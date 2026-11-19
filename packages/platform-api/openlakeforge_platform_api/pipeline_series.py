"""Pipeline run series from Dagster reads (ADR 0024, decision 2): runs and run durations per step.

Dagster has no Prometheus endpoint at the pinned version, so these series come from the run storage the runs route
already reads, and they exist on Slim, where no Prometheus runs. The routes that serve them and the range and step
vocabulary belong to the metrics read path (ADR 0024, decision 4); this module computes the window it is given.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from openlakeforge_core.health import RunStatus
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterRun

from openlakeforge_platform_api.pipeline_health import RUN_CAP, RUN_STATUS, at

log = logging.getLogger(__name__)

PIPELINE_RUNS = "pipeline_runs"
PIPELINE_RUN_DURATION_SECONDS = "pipeline_run_duration_seconds"
PIPELINE_SERIES = (PIPELINE_RUNS, PIPELINE_RUN_DURATION_SECONDS)
# The read bound of ADR 0024, decision 2: the longest range the metrics routes accept, and RUN_CAP runs per stage.
MAX_RANGE = timedelta(days=7)

SeriesState = Literal["available", "disabled", "unavailable"]
Point = tuple[datetime, float | None]


@dataclass(frozen=True)
class SeriesLine:
    labels: Mapping[str, str]
    # One point per step, oldest first, each the value of the step that ends at its time.
    points: list[Point]


@dataclass(frozen=True)
class PipelineSeries:
    series: str
    stage: str
    state: SeriesState
    reason: str
    lines: list[SeriesLine] = field(default_factory=list)
    # False when the run cap was reached: the read may have dropped older runs, so every step that starts before
    # `covered_from`, the oldest read run's creation time, is None rather than a count that looks complete.
    complete: bool = True
    covered_from: datetime | None = None


def _steps(end: datetime, duration: timedelta, step: timedelta) -> list[datetime]:
    if not timedelta(0) < duration <= MAX_RANGE:
        raise ValueError(f"a pipeline series covers more than nothing and at most {MAX_RANGE}, not {duration}")
    if step <= timedelta(0) or duration % step:
        raise ValueError(f"step {step} does not divide range {duration}")
    count = duration // step
    return [end - step * (count - 1 - index) for index in range(count)]


def _buckets(runs: list[DagsterRun], start: datetime, step: timedelta, count: int) -> dict[int, list[DagsterRun]]:
    """Runs by the step they were created in, the step ending at a point holding the runs created after the previous.

    Creation time, not end time, places a run: the read is capped newest-created first, so only creation time makes
    every step from `covered_from` on exact. A long run that ends in the window but began before it is not counted.
    """
    buckets: dict[int, list[DagsterRun]] = {}
    for run in runs:
        index = math.ceil((run.created_at - start.timestamp()) / step.total_seconds()) - 1
        if 0 <= index < count:
            buckets.setdefault(index, []).append(run)
    return buckets


def _run_counts(stage: str, job: str, buckets: dict[int, list[DagsterRun]], times: list[datetime]) -> list[SeriesLine]:
    # A run still queued or in progress counts as `running`, so a stuck run shows in the step it was created in.
    lines = []
    for status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.RUNNING):
        points: list[Point] = []
        for index, time in enumerate(times):
            matched = [
                run
                for run in buckets.get(index, [])
                if run.job_name == job and RUN_STATUS.get(run.status, RunStatus.RUNNING) is status
            ]
            points.append((time, len(matched)))
        lines.append(SeriesLine({"stage": stage, "job": job, "status": status.value}, points))
    return lines


def _durations(stage: str, job: str, buckets: dict[int, list[DagsterRun]], times: list[datetime]) -> SeriesLine:
    # The longest finished run of the step, not the mean: the chart exists to show a slow run, which a mean of one
    # slow and several quick runs hides. A step without a finished run has no duration, which is None, never 0.
    points: list[Point] = []
    for index, time in enumerate(times):
        durations = [
            run.ended_at - run.started_at
            for run in buckets.get(index, [])
            if run.job_name == job and run.started_at is not None and run.ended_at is not None
        ]
        points.append((time, max(durations, default=None)))
    return SeriesLine({"stage": stage, "job": job}, points)


def _blank_before(lines: list[SeriesLine], covered_from: datetime, step: timedelta) -> list[SeriesLine]:
    return [
        SeriesLine(line.labels, [(time, value if time - step >= covered_from else None) for time, value in line.points])
        for line in lines
    ]


def read_pipeline_series(
    client: DagsterClient | None,
    series: str,
    stage: str,
    *,
    end: datetime,
    duration: timedelta,
    step: timedelta,
) -> PipelineSeries:
    """One pipeline series for the stage over `duration` ending at `end`, a line per job.

    `pipeline_runs` counts the runs created in each step by job and status; `pipeline_run_duration_seconds` is the
    longest finished run's duration. An unreadable Dagster yields no lines: `unavailable`, never an empty chart that
    reads as no runs. Raises ValueError for an unknown series or a window outside the read bound.
    """
    if series not in PIPELINE_SERIES:
        raise ValueError(f"{series!r} is not a pipeline series")
    times = _steps(end, duration, step)
    if client is None:
        return PipelineSeries(series, stage, "disabled", "no Dagster is configured for this stage")
    start = end - duration
    try:
        jobs = {job.name for job in client.list_jobs()}
        runs = client.list_runs_created_after(start.timestamp(), limit=RUN_CAP)
    except ServiceClientError as exc:
        # The service's own message goes to the log, not the caller: a Dagster PythonError can carry a stack trace.
        log.warning("Dagster for stage %s could not be read for pipeline series: %s", stage, exc)
        return PipelineSeries(series, stage, "unavailable", "Dagster could not be read")
    # A job that ran in the window and has since been removed from the code still has a line; Dagster's own
    # `__ASSET_JOB` runs are left out, as `list_jobs` leaves out the job.
    jobs |= {run.job_name for run in runs if not run.job_name.startswith("__")}
    buckets = _buckets(runs, start, step, len(times))
    lines: list[SeriesLine] = []
    for job in sorted(jobs):
        if series == PIPELINE_RUNS:
            lines.extend(_run_counts(stage, job, buckets, times))
        else:
            lines.append(_durations(stage, job, buckets, times))
    # Equal to the cap means the read may have stopped short; one more run cannot be asked for within the bound.
    if len(runs) < RUN_CAP:
        return PipelineSeries(series, stage, "available", "read from Dagster", lines)
    covered_from = at(min(run.created_at for run in runs))
    return PipelineSeries(
        series,
        stage,
        "available",
        f"read from Dagster; the newest {RUN_CAP} runs cover the range from {covered_from.isoformat()} only",
        _blank_before(lines, covered_from, step),
        complete=False,
        covered_from=covered_from,
    )
