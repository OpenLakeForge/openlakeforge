"""The failure summary of one run (#1271): its failed steps, its own failure message and the rows Floe rejected.

The health routes stay free of error text; this sub-resource of `get_run` is where a client reads it, for the runs it
shows. Error text is bounded and redacted on the way out: only the first line of a step's innermost cause, at most
MESSAGE_MAX_CHARS after redaction, and the Dagster read never selects `stack`. Row counts are aggregates from the run's
quality manifest (ADR 0023), which this route reads and never changes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from fastapi import HTTPException, Request
from openlakeforge_core.quality import QualityManifest
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterFailure, DagsterRun
from openlakeforge_services.redact import redact

from openlakeforge_platform_api.models import FailedStep, IntegrationStatus, RunFailure, RunRows
from openlakeforge_platform_api.quality_health import DOMAIN_TAG, PIPELINE_JOB_SUFFIX, QualityReadError
from openlakeforge_platform_api.reads import _dagster, _dagster_errors

log = logging.getLogger(__name__)

MAX_STEPS = 5
MESSAGE_MAX_CHARS = 300
# The whole answer, the run, its event log and its manifest, is held to this, as in quality_routes: each call has its
# own timeout, but a hung Dagster or store would otherwise hold the request for their sum.
READ_DEADLINE_SECONDS = 20.0
# A read abandoned at the deadline keeps its thread until its own timeouts end it; a small pool caps how many.
_READS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="run-failure-read")
_ENGINE = "floe"


class _Deadline(Exception):
    pass


def _within[T](expires: float, read: Callable[[], T]) -> T:
    def run() -> T:
        if time.monotonic() >= expires:
            raise _Deadline
        return read()

    answer = _READS.submit(run)
    try:
        return answer.result(timeout=max(expires - time.monotonic(), 0))
    except FutureTimeout as exc:
        answer.cancel()
        raise _Deadline from exc


def _bounded(text: str) -> tuple[str, bool]:
    """The first line of `text`, redacted and then cut, and whether anything of `text` was left out."""
    lines = text.strip().splitlines()
    first = redact(lines[0].strip()) if lines else ""
    return first[:MESSAGE_MAX_CHARS], len(first) > MESSAGE_MAX_CHARS or len(lines) > 1


def _step(failure: DagsterFailure) -> FailedStep:
    innermost = failure.errors[-1] if failure.errors else None
    message, truncated = _bounded(innermost.message if innermost else "")
    return FailedStep(
        step=failure.step_key or "",
        error_class=innermost.class_name if innermost else None,
        message=message,
        message_truncated=truncated,
    )


def _failures(
    client: DagsterClient, run: DagsterRun, expires: float
) -> tuple[list[FailedStep] | None, str | None, bool, IntegrationStatus]:
    if run.status == "SUCCESS":
        # A step retried to success leaves a failure event behind; the run did not fail, so none is reported.
        return [], None, False, IntegrationStatus(name="dagster", status="available", reason="the run succeeded")
    try:
        failures = _within(expires, lambda: client.run_failures(run.run_id))
    except (ServiceClientError, _Deadline) as exc:
        # Dagster's own message can carry a stack trace: the log has it, the answer does not.
        log.warning("the event log of run %s could not be read: %r", run.run_id, exc)
        unavailable = IntegrationStatus(
            name="dagster", status="unavailable", reason="the run's event log could not be read"
        )
        return None, None, False, unavailable
    steps = [failure for failure in failures if failure.step_key is not None]
    killed = next((failure for failure in failures if failure.step_key is None), None)
    run_message = _bounded(killed.errors[0].message)[0] if killed and not steps else None
    available = IntegrationStatus(name="dagster", status="available", reason="read from the run's event log")
    return [_step(failure) for failure in steps[:MAX_STEPS]], run_message, len(steps) > MAX_STEPS, available


def _sum_rows(manifest: QualityManifest) -> RunRows | None:
    # Floe reports an entity's row counts on each of its checks, so they are taken once per table, then summed.
    tables: dict[str | None, tuple[int, int]] = {}
    for result in manifest.results:
        rejected, accepted = result.metrics.get("rejected_rows"), result.metrics.get("accepted_rows")
        if result.engine != _ENGINE or rejected is None or accepted is None:
            continue
        seen = tables.get(result.table, (0, 0))
        tables[result.table] = (max(seen[0], int(rejected)), max(seen[1], int(accepted)))
    if not tables:
        return None
    return RunRows(
        rejected=sum(rejected for rejected, _ in tables.values()),
        accepted=sum(accepted for _, accepted in tables.values()),
        complete=manifest.complete,
    )


def _rows(request: Request, stage: str, run: DagsterRun, expires: float) -> tuple[RunRows | None, IntegrationStatus]:
    reader = request.app.state.quality_readers.get(stage)
    if reader is None:
        return None, IntegrationStatus(
            name="quality", status="not_provided", reason="no quality manifest reader is configured for this stage"
        )
    domain = run.tags.get(DOMAIN_TAG)
    if domain is None or not run.job_name.endswith(PIPELINE_JOB_SUFFIX):
        return None, IntegrationStatus(
            name="quality", status="not_provided", reason="only a product pipeline's run writes a quality manifest"
        )
    product = run.job_name.removesuffix(PIPELINE_JOB_SUFFIX)
    try:
        manifest = _within(expires, lambda: reader.read(stage, domain, product, run.run_id))
    except (QualityReadError, _Deadline) as exc:
        # The store's message can name keys and endpoints: the log has it, the answer does not.
        log.warning("the quality manifest of run %s in stage %s was not usable: %r", run.run_id, stage, exc)
        return None, IntegrationStatus(
            name="quality", status="unavailable", reason="the run's quality manifest could not be read"
        )
    if manifest is None:
        return None, IntegrationStatus(
            name="quality", status="not_provided", reason="the run wrote no quality manifest"
        )
    return _sum_rows(manifest), IntegrationStatus(
        name="quality", status="available", reason="read from the run's quality manifest"
    )


def get_run_failure(request: Request, stage: str, run_id: str) -> RunFailure:
    """The run's failed steps, its own failure message and its rejected rows, each bounded and redacted."""
    client = _dagster(request, stage)
    expires = time.monotonic() + READ_DEADLINE_SECONDS
    with _dagster_errors(stage):
        try:
            run = _within(expires, lambda: client.get_run(run_id))
        except _Deadline as exc:
            raise HTTPException(
                status_code=502, detail=f"Dagster for stage {stage!r} did not answer in {READ_DEADLINE_SECONDS:g} s"
            ) from exc
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} in stage {stage!r}")
    steps, run_message, steps_truncated, dagster = _failures(client, run, expires)
    rows, quality = _rows(request, stage, run, expires)
    return RunFailure(
        run_id=run.run_id,
        job=run.job_name,
        status=run.status,
        steps=steps,
        run_message=run_message,
        steps_truncated=steps_truncated,
        rows=rows,
        sources=[dagster, quality],
    )
