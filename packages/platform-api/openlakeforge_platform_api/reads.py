"""Read endpoints: Dagster jobs and runs per stage, OpenMetadata domains and data products.

The two services take different identities (ADR 0021, decision 2). Dagster
checks nothing, so the API reaches it with no credential and the permission
matrix is the only check. OpenMetadata authorizes by the person's own roles, so
the API forwards the caller's token unchanged and holds no OpenMetadata
credential.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from fastapi import HTTPException, Query, Request
from openlakeforge_services.base import ServiceClientError, TransientServiceError
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.openmetadata import OpenMetadataClient

log = logging.getLogger(__name__)

MAX_RUNS = 100


def _dagster(request: Request, stage: str) -> DagsterClient:
    url = request.app.state.settings.dagster_graphql_urls.get(stage)
    if not url:
        raise HTTPException(status_code=404, detail=f"no Dagster is configured for stage {stage!r}")
    return DagsterClient(url)


@contextmanager
def _dagster_errors(stage: str) -> Iterator[None]:
    # The service's own message goes to the log, not the caller: a Dagster
    # PythonError can carry a stack trace.
    try:
        yield
    except TransientServiceError as exc:
        log.warning("Dagster for stage %s is unreachable: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"Dagster for stage {stage!r} is unreachable") from exc
    except ServiceClientError as exc:
        log.warning("Dagster for stage %s failed: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"Dagster for stage {stage!r} answered with an error") from exc


def list_jobs(request: Request, stage: str) -> dict[str, Any]:
    with _dagster_errors(stage):
        jobs = _dagster(request, stage).list_jobs()
    return {"jobs": [asdict(job) for job in jobs]}


def list_runs(
    request: Request,
    stage: str,
    job: str = "",
    limit: int = Query(default=20, ge=1, le=MAX_RUNS),  # noqa: B008 - FastAPI's parameter API
) -> dict[str, Any]:
    with _dagster_errors(stage):
        runs = _dagster(request, stage).list_runs(job_name=job, limit=limit)
    return {"runs": [asdict(run) for run in runs]}


def get_run(request: Request, stage: str, run_id: str) -> dict[str, Any]:
    with _dagster_errors(stage):
        run = _dagster(request, stage).get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} in stage {stage!r}")
    return asdict(run)


def _openmetadata(request: Request) -> OpenMetadataClient:
    url = request.app.state.settings.openmetadata_url
    if not url:
        raise HTTPException(status_code=404, detail="OpenMetadata is not configured on this deployment")
    # The token the API has just validated, unchanged. OpenMetadata checks its
    # signature and expiry but not its audience, so the API's own audience
    # check in front is the only one on this path.
    return OpenMetadataClient(url.rstrip("/"), token=request.state.token)


@contextmanager
def _openmetadata_errors() -> Iterator[None]:
    try:
        yield
    except ServiceClientError as exc:
        log.warning("OpenMetadata failed: %s", exc)
        if exc.status == 401:
            raise HTTPException(
                status_code=401,
                detail="OpenMetadata refused the token",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from exc
        if exc.status == 403:
            raise HTTPException(status_code=403, detail="OpenMetadata denied the request to this user") from exc
        if isinstance(exc, TransientServiceError) and exc.status is None:
            raise HTTPException(status_code=502, detail="OpenMetadata is unreachable") from exc
        raise HTTPException(status_code=502, detail="OpenMetadata answered with an error") from exc


def list_domains(request: Request) -> dict[str, Any]:
    with _openmetadata_errors():
        domains = _openmetadata(request).list_domains()
    return {"domains": [{"name": name, "display_name": display_name} for name, display_name in domains]}


def list_data_products(request: Request) -> dict[str, Any]:
    with _openmetadata_errors():
        products = _openmetadata(request).list_data_products()
    return {"data_products": [asdict(product) for product in products]}
