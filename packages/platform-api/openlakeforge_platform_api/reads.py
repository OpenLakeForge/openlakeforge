"""Read endpoints: Dagster jobs and runs per stage.

Dagster checks nothing (ADR 0021, decision 2), so the API reaches it with no
credential and the permission matrix is the only check.
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
