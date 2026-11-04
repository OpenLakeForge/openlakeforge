"""Schema and table discovery for the Portal's SQL explorer, run through Trino as the signed-in person.

Every statement carries the person's own token (`trino_identity`), so Polaris decides which schemas and tables
come back; nothing is cached, and the API holds no metadata credential. The catalog is the stage's, from settings.
No `X-Trino-Schema` is ever sent: the explorer's Schema filter is the UI's, and SQL names `schema.table` itself.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import Depends, HTTPException, Query, Request
from openlakeforge_services.trino import (
    TrinoAuthError,
    TrinoError,
    TrinoForbiddenError,
    TrinoQueryError,
    TrinoResult,
    TrinoStatementClient,
)

from openlakeforge_platform_api.query_identity import TrinoIdentity, trino_identity

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
EXPORT_FORMATS = ["csv", "json"]
SOURCE = "olf-platform-api"
_TIMEOUT_SECONDS = 30.0


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _contains(column: str, q: str) -> str:
    """A case-insensitive substring match; `\\`, `%` and `_` in `q` match themselves."""
    escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"lower({column}) LIKE {literal('%' + escaped + '%')} ESCAPE '\\'"


def _decode(cursor: str) -> str:
    try:
        return base64.b64decode(cursor, altchars=b"-_", validate=True).decode()
    except ValueError as exc:  # binascii.Error and the unicode errors are all ValueErrors
        raise HTTPException(status_code=400, detail="the cursor is not one this API issued") from exc


def _encode(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode()).decode()


def query_capabilities(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    # Without the signing key every query route answers 503, so no stage is offered.
    ready = settings.person_query and request.app.state.query_signing_key is not None
    stages = [stage for stage in settings.stages if stage in settings.query_endpoints] if ready else []
    return {
        "query_available": bool(stages),
        "stages": [
            {"stage": stage, "query": {"engine": "trino", "available": True, "export_formats": EXPORT_FORMATS}}
            for stage in stages
        ],
    }


def _page(
    request: Request,
    identity: TrinoIdentity,
    stage: str,
    column: str,
    source: str,
    where: list[str],
    q: str,
    cursor: str | None,
    limit: int,
) -> tuple[list[list[Any]], str | None]:
    endpoint = request.app.state.settings.query_endpoints.get(stage)
    if endpoint is None:
        raise HTTPException(status_code=404, detail=f"stage {stage!r} offers no query")
    conditions = list(where)
    if q:
        conditions.append(_contains(column, q))
    if cursor:
        conditions.append(f"{column} > {literal(_decode(cursor))}")
    sql = (
        f"SELECT {column}{', table_type' if column == 'table_name' else ''} "
        f"FROM {identifier(endpoint.catalog)}.information_schema.{source}"
        + (f" WHERE {' AND '.join(conditions)}" if conditions else "")
        + f" ORDER BY {column} LIMIT {limit + 1}"
    )
    client = TrinoStatementClient(
        endpoint.statement_url,
        endpoint.catalog,
        identity.user,
        SOURCE,
        _TIMEOUT_SECONDS,
        send=request.app.state.trino_send,
    )
    try:
        result: TrinoResult = client.run(
            sql, bearer=lambda: identity.token, extra_credentials=lambda token: {"token": token}, max_rows=limit + 1
        )
    except TrinoAuthError as exc:
        raise HTTPException(
            status_code=401,
            detail="Trino refused the token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except TrinoForbiddenError as exc:
        raise HTTPException(status_code=403, detail="access to this stage's data was refused") from exc
    except TrinoQueryError as exc:
        if exc.denied:
            raise HTTPException(status_code=403, detail="access to this stage's data was refused") from exc
        log.warning("Trino discovery failed in stage %s: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"Trino for stage {stage!r} answered with an error") from exc
    except TrinoError as exc:
        # The message names a host and no token, but a caller has no use for it.
        log.warning("Trino for stage %s is unreachable: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"Trino for stage {stage!r} is unreachable") from exc
    rows = result.rows[:limit]
    more = len(result.rows) > limit
    return rows, _encode(str(rows[-1][0])) if more else None


def list_schemas(
    request: Request,
    stage: str,
    q: str = "",
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),  # noqa: B008 - FastAPI's parameter API
    identity: TrinoIdentity = Depends(trino_identity),  # noqa: B008
) -> dict[str, Any]:
    rows, next_cursor = _page(request, identity, stage, "schema_name", "schemata", [], q, cursor, limit)
    return {"schemas": [row[0] for row in rows], "next_cursor": next_cursor}


def list_tables(
    request: Request,
    stage: str,
    schema: str,
    q: str = "",
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),  # noqa: B008
    identity: TrinoIdentity = Depends(trino_identity),  # noqa: B008
) -> dict[str, Any]:
    where = [f"table_schema = {literal(schema)}"]
    rows, next_cursor = _page(request, identity, stage, "table_name", "tables", where, q, cursor, limit)
    tables = [{"name": name, "type": "view" if kind == "VIEW" else "table"} for name, kind in rows]
    return {"tables": tables, "next_cursor": next_cursor}
