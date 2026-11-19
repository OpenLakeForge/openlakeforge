"""Per-stage data quality counts (#1257): the outcome counts of the manifest behind each product pipeline's signal.

There is no second reader. The route takes the `read_stage_pipelines` pass `/health/pipelines` makes, and counts the
manifest that pass already read for the quality signal, so a pipeline's `state` here is its `quality` signal there.
The answer carries counts and run ids only, as the health routes do: no check id, table, row or `artifact_uri`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome

from openlakeforge_platform_api import health_routes
from openlakeforge_platform_api.models import IntegrationStatus, PipelineQuality, QualityCounts, StageQualityHealth
from openlakeforge_platform_api.pipeline_health import PipelineEvaluation

# The whole read, Dagster and every manifest, is held to this. Each Dagster call and manifest read has its own
# timeout, but a stage has many of them, so without a deadline a hung Dagster or store would hold the request for
# minutes. Past it the answer is Dagster `unavailable` with no counts, never a partial list.
READ_DEADLINE_SECONDS = 20.0
# A read abandoned at the deadline keeps its thread until its own timeouts end it; a small pool caps how many, and a
# read queued behind them is dropped once its caller has stopped waiting.
_READS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quality-read")


class _Deadline(Exception):
    pass


def _bounded[T](read: Callable[[], T]) -> T:
    expires = time.monotonic() + READ_DEADLINE_SECONDS

    def run() -> T:
        if time.monotonic() >= expires:
            raise _Deadline
        return read()

    answer = _READS.submit(run)
    try:
        return answer.result(timeout=READ_DEADLINE_SECONDS)
    except FutureTimeout as exc:
        answer.cancel()
        raise _Deadline from exc


def _counts(manifest: QualityManifest) -> QualityCounts:
    outcomes = [result.outcome for result in manifest.results]
    return QualityCounts(
        passed=outcomes.count(QualityOutcome.PASSED),
        warn=outcomes.count(QualityOutcome.WARN),
        error=outcomes.count(QualityOutcome.ERROR),
        not_run=outcomes.count(QualityOutcome.NOT_RUN),
        unknown=outcomes.count(QualityOutcome.UNKNOWN),
        blocking=sum(
            result.outcome is QualityOutcome.ERROR and result.pipeline_outcome is PipelineOutcome.BLOCKED
            for result in manifest.results
        ),
    )


def _total(counts: Iterable[QualityCounts]) -> QualityCounts | None:
    known = list(counts)
    if not known:
        return None
    return QualityCounts(
        **{field: sum(getattr(item, field) for item in known) for field in QualityCounts.model_fields},
    )


def _pipeline(pipeline: PipelineEvaluation) -> PipelineQuality | None:
    evidence = pipeline.quality
    if evidence is None:
        return None
    manifest, signal = evidence.manifest, evidence.signal
    return PipelineQuality(
        job=pipeline.job,
        domain=pipeline.domain,
        product=pipeline.product,
        run_id=evidence.run_id,
        observed_at=max((result.observed_at for result in manifest.results), default=None) if manifest else None,
        complete=manifest.complete if manifest else None,
        state=signal.state.value if signal else "unknown",
        counts=_counts(manifest) if manifest else None,
        reason=signal.reason if signal else "no finished run of the pipeline has quality evidence yet",
    )


def get_stage_quality_health(request: Request, stage: str) -> StageQualityHealth:
    """Each product pipeline's quality outcome counts, and their sum over the stage."""
    now = datetime.now(UTC)
    quality = health_routes._quality(request, stage)
    try:
        dagster, evaluations = _bounded(lambda: health_routes._read(request, stage, now))
    except _Deadline:
        dagster = IntegrationStatus(
            name="dagster", status="unavailable", reason=f"the read did not finish within {READ_DEADLINE_SECONDS:g} s"
        )
        evaluations = []
    if dagster.status == "disabled":
        raise HTTPException(status_code=404, detail=f"no Dagster is configured for stage {stage!r}")
    pipelines = [entry for entry in map(_pipeline, evaluations) if entry is not None]
    return StageQualityHealth(
        stage=stage,
        evaluated_at=now,
        integrations=[dagster, quality],
        totals=_total(entry.counts for entry in pipelines if entry.counts is not None),
        pipelines=pipelines,
    )
