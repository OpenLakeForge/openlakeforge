"""Run, page, cancel and export a person's SQL through Trino, as that person (ADR 0019, ADR 0021).

Nothing is stored. The `id` and `cursor` a client holds are HMAC-signed records of where the query is in Trino
(its id and continuation path), the stage, the default schema if one was named and the person's token `sub`, so
any replica can resume a query and none can be made to resume another person's or another stage's: a value that
does not verify for this person on this stage is a 404, as if it did not exist. They are signed, not encrypted. The
continuation URI is kept as a path and re-attached to the stage's configured origin, so a request can never steer
where the person's token is sent.

Read-only for now, enforced by Trino. Trino reports a statement's kind only after running it, and the grammar is its
own, so the API asks Trino whether the text is a query: only a query (SELECT, WITH, VALUES, TABLE) can stand as a
table expression in `SELECT * FROM (<sql>)`, and `EXPLAIN (TYPE VALIDATE)` checks that without running anything.
Anything Trino accepts as a statement but not as that is refused as a write. No audit record is written for the
same reason decision 3 exempts reads: the API acts on nothing it could not undo.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import logging
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from openlakeforge_services.trino import (
    TrinoAuthError,
    TrinoCancelledError,
    TrinoError,
    TrinoForbiddenError,
    TrinoNotFoundError,
    TrinoPage,
    TrinoQueryError,
    TrinoStatementClient,
    TrinoTimeoutError,
)
from pydantic import BaseModel, ConfigDict, Field

from openlakeforge_platform_api.discovery import SOURCE
from openlakeforge_platform_api.query_identity import TrinoIdentity, trino_identity
from openlakeforge_platform_api.settings import Settings

log = logging.getLogger(__name__)

# A leading one of these makes a spreadsheet read a cell as a formula (OWASP, CSV injection).
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
# The id travels in a URL path; past this a proxy may refuse it.
MAX_ID_CHARS = 7000
MAX_SCHEMA_CHARS = 128
_READ_ONLY_REFUSAL = (
    "only read queries (SELECT, WITH, VALUES, TABLE) are offered in the portal yet; writes and other statements are not"
)


class QuerySubmit(BaseModel):
    # The stage in the path is the only catalog selection: a body naming a catalog, URL or user is refused.
    model_config = ConfigDict(extra="forbid")

    sql: str
    # The query's default schema, sent as `X-Trino-Schema`; None keeps names qualified. Checked in `_schema`.
    # Aliased because `schema` is a BaseModel attribute.
    default_schema: str | None = Field(None, alias="schema")


def _schema(value: str | None) -> str | None:
    """The schema travels as an HTTP header, so only printable ASCII: a newline or a non-Latin-1 byte cannot get in."""
    if value is None:
        return None
    if not value or len(value) > MAX_SCHEMA_CHARS or not (value.isascii() and value.isprintable()):
        raise HTTPException(
            status_code=400, detail=f"schema must be 1 to {MAX_SCHEMA_CHARS} printable ASCII characters"
        )
    return value


@dataclass(frozen=True)
class _Ctx:
    settings: Settings
    stage: str
    subject: str
    key: bytes
    base: str
    client: TrinoStatementClient
    identity: TrinoIdentity

    def credentials(self, token: str) -> dict[str, str]:
        return {"token": token}

    def url(self, path: str) -> str:
        return self.base + path


def _context(request: Request, stage: str, identity: TrinoIdentity) -> _Ctx:
    settings: Settings = request.app.state.settings
    endpoint = settings.query_endpoints.get(stage)
    if endpoint is None:
        raise HTTPException(status_code=404, detail=f"stage {stage!r} offers no query")
    key: bytes | None = request.app.state.query_signing_key
    if key is None:
        raise HTTPException(status_code=503, detail="query execution is unavailable: no signing key is configured")
    parts = urlsplit(endpoint.statement_url)
    client = TrinoStatementClient(
        endpoint.statement_url,
        endpoint.catalog,
        identity.user,
        SOURCE,
        settings.query_timeout_seconds,
        send=request.app.state.trino_send,
    )
    return _Ctx(
        settings, stage, request.state.principal.subject, key, f"{parts.scheme}://{parts.netloc}", client, identity
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(ctx: _Ctx, kind: str, **fields: Any) -> str:
    payload = {"k": kind, "s": ctx.stage, "u": ctx.subject, **fields}
    body = zlib.compress(json.dumps(payload, separators=(",", ":")).encode())
    return f"{_b64(body)}.{_b64(hmac.new(ctx.key, body, hashlib.sha256).digest())}"


def _open(ctx: _Ctx, kind: str, token: str) -> dict[str, Any]:
    """The signed fields, or 404: a forged, foreign, cross-stage or mistyped value is not distinguished."""
    gone = HTTPException(status_code=404, detail="no such query")
    try:
        body_text, _, mac_text = token.partition(".")
        body, mac = _unb64(body_text), _unb64(mac_text)
        if not hmac.compare_digest(mac, hmac.new(ctx.key, body, hashlib.sha256).digest()):
            raise gone
        payload = json.loads(zlib.decompress(body))
    except (ValueError, zlib.error, binascii.Error) as exc:
        raise gone from exc
    if not isinstance(payload, dict) or (payload.get("k"), payload.get("s"), payload.get("u")) != (
        kind,
        ctx.stage,
        ctx.subject,
    ):
        raise gone
    return payload


def _path(uri: str) -> str:
    parts = urlsplit(uri)
    return parts.path + (f"?{parts.query}" if parts.query else "")


@contextmanager
def _trino_errors(stage: str, *, gone: bool = False) -> Iterator[None]:
    """Answer Trino's failures as the contract's statuses. Logs name the failure, never the SQL or a token."""
    try:
        yield
    except TrinoAuthError as exc:
        raise HTTPException(
            status_code=401,
            detail="Trino refused the token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except TrinoForbiddenError as exc:
        raise HTTPException(status_code=403, detail="access to this stage's data was refused") from exc
    except TrinoNotFoundError as exc:
        if not gone:
            raise HTTPException(status_code=502, detail=f"Trino for stage {stage!r} is unreachable") from exc
        raise HTTPException(status_code=404, detail="no such query") from exc
    except TrinoQueryError as exc:
        # The message is about the person's own SQL or grants, which is the feedback they ran the query for.
        if exc.denied:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if exc.error.get("errorType") == "USER_ERROR":
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.warning("Trino failed a query in stage %s: %s", stage, exc.error.get("errorName"))
        raise HTTPException(status_code=502, detail=f"Trino for stage {stage!r} answered with an error") from exc
    except TrinoTimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"Trino for stage {stage!r} timed out") from exc
    except TrinoError as exc:
        log.warning("Trino for stage %s failed: %s", stage, type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"Trino for stage {stage!r} is unreachable") from exc


def _require_read_only(ctx: _Ctx, sql: str) -> None:
    token = ctx.identity.token

    def validate(statement: str) -> None:
        ctx.client.run(
            f"EXPLAIN (TYPE VALIDATE) {statement}", bearer=lambda: token, extra_credentials=ctx.credentials, max_rows=1
        )

    try:
        # The newline ends a trailing `--` comment, which would otherwise swallow the closing parenthesis.
        validate(f"SELECT * FROM (\n{sql}\n)")
        return
    except TrinoQueryError as exc:
        if exc.denied:
            raise
    # Not a query. Valid as a statement, it is a write or another statement kind; invalid, it is the person's typo,
    # and this raises Trino's own message about the text they sent.
    validate(sql)
    raise HTTPException(status_code=403, detail=_READ_ONLY_REFUSAL)


def _response(
    ctx: _Ctx,
    query_id: str,
    started: float,
    *,
    status: str,
    page: _Drained | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    drained = page or _Drained([], [], [], False)
    return {
        "id": query_id,
        "status": status,
        "columns": [{"name": n, "type": t} for n, t in zip(drained.columns, drained.types, strict=False)],
        "rows": drained.rows,
        "truncated": drained.truncated,
        "row_count": len(drained.rows),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "next_cursor": cursor,
    }


@dataclass(frozen=True)
class _Drained:
    columns: list[str]
    types: list[str]
    rows: list[list[Any]]
    truncated: bool
    next_uri: str | None = None
    state: str | None = None


def _drain(ctx: _Ctx, page: TrinoPage) -> _Drained:
    """Follow `page` onward until the row cap, the end of the result, or the wait budget; never past the cap."""
    cap = ctx.settings.query_page_rows
    token = ctx.identity.token
    deadline = time.monotonic() + ctx.settings.query_wait_seconds
    columns, types = page.columns, page.types
    rows: list[list[Any]] = []
    while True:
        room = cap - len(rows)
        if len(page.rows) > room:
            # shortcut: one Trino page larger than the cap is cut and the rest abandoned, since resuming mid-page
            # needs server state; the export route reads whole results. Upgrade with a store of its own.
            rows.extend(page.rows[:room])
            if page.next_uri:
                try:
                    ctx.client.cancel(page.next_uri, token)
                except TrinoError:
                    pass
            return _Drained(columns, types, rows, True)
        rows.extend(page.rows)
        if page.next_uri is None or len(rows) >= cap or time.monotonic() >= deadline:
            return _Drained(columns, types, rows, False, page.next_uri, page.state)
        page = ctx.client.follow(page.next_uri, token)
        columns, types = columns or page.columns, types or page.types


def _answer(ctx: _Ctx, query_id: str, trino_id: str, started: float, drained: _Drained) -> dict[str, Any]:
    if drained.next_uri is None:
        return _response(ctx, query_id, started, status="succeeded", page=drained)
    cursor = _sign(ctx, "cursor", q=trino_id, p=_path(drained.next_uri))
    status = "queued" if drained.state == "QUEUED" and not drained.rows else "running"
    return _response(ctx, query_id, started, status=status, page=drained, cursor=cursor)


def submit_query(
    request: Request,
    stage: str,
    body: QuerySubmit,
    identity: TrinoIdentity = Depends(trino_identity),  # noqa: B008 - FastAPI's dependency API
) -> dict[str, Any]:
    ctx = _context(request, stage, identity)
    started = time.monotonic()
    # Trino rejects a trailing semicolon, which every SQL editor writes.
    sql = body.sql.strip().rstrip(";").strip()
    if not sql:
        raise HTTPException(status_code=400, detail="the statement is empty")
    schema = _schema(body.default_schema)
    ctx = replace(ctx, client=replace(ctx.client, schema=schema))
    with _trino_errors(stage):
        _require_read_only(ctx, sql)
        page = ctx.client.submit(sql, identity.token, ctx.credentials(identity.token))
        extra = {"sc": schema} if schema else {}
        next_path = _path(page.next_uri) if page.next_uri else None
        query_id = _sign(ctx, "id", q=page.query_id or "", p=next_path, sql=sql, **extra)
        if len(query_id) > MAX_ID_CHARS:
            if page.next_uri:
                ctx.client.cancel(page.next_uri, identity.token)
            raise HTTPException(status_code=400, detail="the statement is too long for the portal")
        return _answer(ctx, query_id, page.query_id or "", started, _drain(ctx, page))


def get_query(
    request: Request,
    stage: str,
    query_id: str,
    cursor: str | None = None,
    identity: TrinoIdentity = Depends(trino_identity),  # noqa: B008
) -> dict[str, Any]:
    ctx = _context(request, stage, identity)
    started = time.monotonic()
    record = _open(ctx, "id", query_id)
    path = record.get("p")
    if cursor:
        position = _open(ctx, "cursor", cursor)
        if position.get("q") != record.get("q"):
            raise HTTPException(status_code=404, detail="no such query")
        path = position.get("p")
    if not isinstance(path, str):
        # Delivered whole by the submit response, which is all Trino keeps of a result it has finished sending.
        return _response(ctx, query_id, started, status="succeeded")
    with _trino_errors(stage, gone=True):
        try:
            drained = _drain(ctx, ctx.client.follow(ctx.url(path), identity.token))
        except TrinoCancelledError:
            return _response(ctx, query_id, started, status="cancelled")
    return _answer(ctx, query_id, str(record.get("q")), started, drained)


def cancel_query(
    request: Request,
    stage: str,
    query_id: str,
    identity: TrinoIdentity = Depends(trino_identity),  # noqa: B008
) -> dict[str, Any]:
    ctx = _context(request, stage, identity)
    started = time.monotonic()
    record = _open(ctx, "id", query_id)
    # Trino cancels by the query's id and slug, which every continuation URI of it carries, so the first one serves.
    if isinstance(record.get("p"), str):
        with _trino_errors(stage):
            ctx.client.cancel(ctx.url(record["p"]), identity.token)
    return _response(ctx, query_id, started, status="cancelled")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return "'" + value if value.startswith(_FORMULA_STARTS) else value
    if isinstance(value, (list, dict)):
        return _cell(json.dumps(value, default=str))
    return value


def _csv(columns: list[str], rows: list[list[Any]]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([_cell(name) for name in columns])
    for start in range(0, len(rows), 500):
        for row in rows[start : start + 500]:
            writer.writerow([_cell(value) for value in row])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
    if not rows:
        yield buffer.getvalue()


def _json(columns: list[str], rows: list[list[Any]]) -> Iterator[str]:
    # JSON is data, not something a spreadsheet opens as formulas, so values pass through unchanged.
    yield '{"columns":' + json.dumps(columns) + ',"rows":['
    for index, row in enumerate(rows):
        yield ("," if index else "") + json.dumps(row, default=str)
    yield "]}"


def export_query(
    request: Request,
    stage: str,
    query_id: str,
    format: str = Query(pattern="^(csv|json)$"),  # noqa: A002, B008 - the contract's parameter name
    identity: TrinoIdentity = Depends(trino_identity),  # noqa: B008
) -> StreamingResponse:
    ctx = _context(request, stage, identity)
    record = _open(ctx, "id", query_id)
    limit = ctx.settings.query_export_max_rows
    sql = record.get("sql")
    if not isinstance(sql, str):
        raise HTTPException(status_code=404, detail="no such query")
    # Re-run under the default schema the id was signed with, so unqualified names resolve as they did.
    schema = record.get("sc")
    if schema is not None and not isinstance(schema, str):
        raise HTTPException(status_code=404, detail="no such query")
    ctx = replace(ctx, client=replace(ctx.client, schema=schema))
    # Trino gives a result out once, so an export runs the statement again, as the person. The statement needs no
    # read-only probe: it is the text this API signed after the probe passed.
    with _trino_errors(stage):
        result = ctx.client.run(sql, bearer=lambda: identity.token, extra_credentials=ctx.credentials, max_rows=limit)
    if result.truncated:
        raise HTTPException(status_code=413, detail=f"the result has more than {limit} rows; narrow the query")
    # shortcut: up to the export cap is held in memory before the first byte so an oversize result can still be a
    # 413; upgrade to spooling when the cap needs to exceed what one replica can hold.
    render = _csv if format == "csv" else _json
    media = "text/csv; charset=utf-8" if format == "csv" else "application/json"
    return StreamingResponse(
        render(result.columns, result.rows),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="query-results.{format}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
