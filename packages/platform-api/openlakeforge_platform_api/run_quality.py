"""The quality checks of one run (#1325), and the reports it retained, from the run's quality manifest (ADR 0023).

A sub-resource of `get_run` beside `/failure`: the failure answer stays a bounded summary, and a run that did not fail
has checks too. The manifest is the only input, read with the same reader and the same job-tag lookup as the failure
route (`run_failure.run_manifest`), and is never written; nothing is re-run or re-evaluated. Each check carries the
aggregates the adapter recorded, never a row, sample or message.

A report is served only through `get_run_quality_report`, which streams it with the files key. It is named by an id
derived from the manifest's `artifact_uri`, never by a key the client sends, and is served only when that URI lies
under the stage's own activation prefix. The report row's floor is `list_logs`'s: a Floe run report and dbt's run
results can quote values the run read, as its pod logs can.
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import quote, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from openlakeforge_core.alerts import AlertKind, runbook_link
from openlakeforge_core.quality import QualityManifest, QualityOutcome, QualityResult

from openlakeforge_platform_api import files, run_failure
from openlakeforge_platform_api.models import QualityCheck, QualityReport, RunQuality
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.quality_routes import _counts
from openlakeforge_platform_api.reads import _dagster
from openlakeforge_platform_api.settings import Settings

REPORT_OPERATION = "get_run_quality_report"
_REPORT_ID_CHARS = 16


def _count(result: QualityResult, metric: str) -> int | None:
    value = result.metrics.get(metric)
    return None if value is None else int(value)


def _report_id(uri: str) -> str:
    return hashlib.sha256(uri.encode()).hexdigest()[:_REPORT_ID_CHARS]


def _location(settings: Settings, stage: str, uri: str) -> tuple[str, str] | None:
    """The bucket and key of a report under the stage's own activation prefix, or None for anything else.

    The manifest is the stage runtime's, and the files key reaches every stage's ops prefix: this check is what keeps a
    manifest from naming another stage's object.
    """
    base = settings.quality_manifests.get(stage)
    if base is None:
        return None
    prefix, target = urlsplit(base), urlsplit(uri)
    key = target.path.lstrip("/")
    if target.scheme != "s3" or target.netloc != prefix.netloc or not key.startswith(prefix.path.lstrip("/")):
        return None
    try:
        files.check_key(key)
    except HTTPException:
        return None
    if key.endswith("/"):
        return None
    return target.netloc, key


def _check(result: QualityResult) -> QualityCheck:
    return QualityCheck(
        engine=result.engine,
        check_id=result.check_id,
        table=result.table,
        column=result.column,
        engine_severity=result.engine_severity,
        outcome=result.outcome.value,
        pipeline_outcome=result.pipeline_outcome.value,
        observed_at=result.observed_at,
        rows=_count(result, "rows_total"),
        accepted_rows=_count(result, "accepted_rows"),
        rejected_rows=_count(result, "rejected_rows"),
        violations=_count(result, "violations_total"),
        failures=_count(result, "failures"),
        report_id=_report_id(result.artifact_uri) if result.artifact_uri else None,
    )


def _reports(request: Request, stage: str, run_id: str, manifest: QualityManifest) -> list[QualityReport]:
    settings: Settings = request.app.state.settings
    matrix: PermissionMatrix = request.app.state.permissions
    servable = request.app.state.files_store is not None and matrix.allows(
        REPORT_OPERATION, request.state.principal.role, stage
    )
    reports: dict[str, QualityReport] = {}
    for result in manifest.results:
        uri = result.artifact_uri
        if not uri or _report_id(uri) in reports:
            continue
        report_id = _report_id(uri)
        href = None
        if servable and _location(settings, stage, uri) is not None:
            href = f"/api/v1/stages/{stage}/runs/{quote(run_id, safe='')}/quality/reports/{report_id}"
        # One dbt report covers every test of the run, so it names no table.
        table = result.table if result.engine == "floe" else None
        reports[report_id] = QualityReport(id=report_id, engine=result.engine, table=table, href=href)
    return sorted(reports.values(), key=lambda report: (report.engine, report.table or "", report.id))


def _runbook(base_url: str | None, status: str, results: list[QualityResult]) -> str | None:
    """The runbook section of the alert this run would raise: `quality` for an error check, which may be what failed
    the run, else `run_failed` for a failed run. A run that passed, or only warned, links none."""
    if base_url is None:
        return None
    if any(result.outcome is QualityOutcome.ERROR for result in results):
        return runbook_link(base_url, AlertKind.QUALITY)
    if status == "FAILURE":
        return runbook_link(base_url, AlertKind.RUN_FAILED)
    return None


def get_run_quality(request: Request, stage: str, run_id: str) -> RunQuality:
    """Each check of the run's quality manifest, its outcome counts, and the reports the run retained."""
    settings: Settings = request.app.state.settings
    client = _dagster(request, stage)
    expires = time.monotonic() + run_failure.READ_DEADLINE_SECONDS
    run = run_failure.fetch_run(client, stage, run_id, expires)
    manifest, quality = run_failure.run_manifest(request, client, stage, run, expires)
    results = sorted(
        manifest.results if manifest else (),
        key=lambda result: (result.engine, result.table or "", result.column or "", result.check_id),
    )
    return RunQuality(
        run_id=run.run_id,
        job=run.job_name,
        status=run.status,
        complete=manifest.complete if manifest else None,
        counts=_counts(manifest) if manifest else None,
        checks=[_check(result) for result in results] if manifest else None,
        reports=_reports(request, stage, run.run_id, manifest) if manifest else [],
        runbook=_runbook(settings.runbook_base_url, run.status, results),
        sources=[quality],
    )


def get_run_quality_report(request: Request, stage: str, run_id: str, report_id: str) -> StreamingResponse:
    """One report the run's quality manifest names, streamed through the API and never read whole."""
    store = files.files_store(request)
    client = _dagster(request, stage)
    expires = time.monotonic() + run_failure.READ_DEADLINE_SECONDS
    run = run_failure.fetch_run(client, stage, run_id, expires)
    manifest, quality = run_failure.run_manifest(request, client, stage, run, expires)
    if manifest is None:
        status = 502 if quality.status == "unavailable" else 404
        raise HTTPException(status_code=status, detail=f"run {run_id!r}: {quality.reason}")
    uris = (result.artifact_uri for result in manifest.results if result.artifact_uri)
    uri = next((uri for uri in uris if _report_id(uri) == report_id), None)
    location = _location(request.app.state.settings, stage, uri) if uri else None
    if location is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} retained no report {report_id!r}")
    bucket, key = location
    with files.s3_errors():
        try:
            result = run_failure._within(expires, lambda: store.get_object(Bucket=bucket, Key=key))
        except run_failure._Deadline as exc:
            raise HTTPException(status_code=503, detail="the object store did not answer in time") from exc
    headers = {"Content-Disposition": files._content_disposition(key), "X-Content-Type-Options": "nosniff"}
    length = result.get("ContentLength")
    if length is not None:
        headers["Content-Length"] = str(length)
    # Both engines write JSON; the type is fixed so a stored Content-Type cannot make the browser render HTML.
    return StreamingResponse(files._chunks(result["Body"]), media_type="application/json", headers=headers)
