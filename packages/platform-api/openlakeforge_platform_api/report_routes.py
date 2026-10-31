"""Superset report import and export (ADR 0021, build step 5), as each stage's platform API service user.

The service user (role `olf_platform_api`, ADR 0021 open item 3) is not an
Admin. It co-owns every report asset the artifacts phase imports, so it may
overwrite them, and holds the report schemas' `schema_access`, so it may
export them. It signs in with Superset's database provider, which
`/api/v1/security/login` honours whatever the AUTH_TYPE (Flask-AppBuilder
5.0.2, security/api.py), so a Superset with OIDC sign-in still admits it.

An upload never brings a database: `reports.prepare_upload` drops them and the
import runs sparse, so a dataset resolves to a connection the stage already has.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import Body, HTTPException, Request, Response
from openlakeforge_services import reports
from openlakeforge_services.base import ServiceClientError, TransientServiceError
from openlakeforge_services.superset import SupersetClient

from openlakeforge_platform_api.audit import audited
from openlakeforge_platform_api.settings import superset_password_env

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _superset(request: Request, stage: str) -> SupersetClient:
    settings = request.app.state.settings
    url = settings.superset_urls.get(stage)
    if not url:
        raise HTTPException(status_code=404, detail=f"no Superset is configured for stage {stage!r}")
    password = os.environ.get(superset_password_env(stage))
    if not settings.superset_username or not password:
        raise HTTPException(
            status_code=503, detail=f"the platform API holds no Superset credential for stage {stage!r}"
        )
    return SupersetClient(url.rstrip("/"), username=settings.superset_username, password=password)


@contextmanager
def _superset_errors(stage: str) -> Iterator[None]:
    # Superset's own message goes to the log: it can carry a stack trace or name the service user.
    try:
        yield
    except TransientServiceError as exc:
        log.warning("Superset for stage %s is unreachable: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"Superset for stage {stage!r} is unreachable") from exc
    except ServiceClientError as exc:
        log.warning("Superset for stage %s failed: %s", stage, exc)
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"stage {stage!r}'s Superset has no such report") from exc
        raise HTTPException(status_code=502, detail=f"Superset for stage {stage!r} answered with an error") from exc


def import_report(
    request: Request,
    stage: str,
    archive: bytes = Body(..., media_type="application/zip"),  # noqa: B008 - FastAPI's parameter API
) -> dict[str, Any]:
    """Import a report directory, zipped from its root, into the stage's Superset, overwriting what it carries."""
    if len(archive) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"a report upload is at most {MAX_UPLOAD_BYTES} bytes")
    prefix = request.app.state.settings.superset_schema_prefixes.get(stage, "")
    with audited(request, stage=stage, target={"report": f"{len(archive)} bytes"}) as outcome:
        client = _superset(request, stage)
        with _superset_errors(stage):
            client.login(attempts=1)
            try:
                prepared = reports.prepare_upload(archive, client.databases(), schema_prefix=prefix)
            except reports.UploadRejected as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            # The artifacts phase grants the report roles a report's schemas; an import here does not, so a
            # report reading schemas nobody was granted would import and show its viewers nothing.
            missing = sorted(prepared.schema_perms - client.my_schema_access())
            if missing:
                raise HTTPException(
                    status_code=409,
                    detail=f"the report reads schemas no report role has been granted: {', '.join(missing)}; "
                    "deploy the report through the artifacts phase first",
                )
            client.import_assets(prepared.bundle, sparse=True)
        outcome.result["dashboards"] = ",".join(prepared.dashboards)
    return {"dashboards": list(prepared.dashboards), "stage": stage, "request_id": request.state.request_id}


def export_report(request: Request, stage: str, dashboard: str) -> Response:
    """One dashboard, by uuid or slug, as a stage-neutral assets bundle."""
    prefix = request.app.state.settings.superset_schema_prefixes.get(stage, "")
    client = _superset(request, stage)
    with _superset_errors(stage):
        client.login(attempts=1)
        raw = client.export_dashboard(dashboard)
    return Response(content=reports.neutral_export(raw, schema_prefix=prefix), media_type="application/zip")
