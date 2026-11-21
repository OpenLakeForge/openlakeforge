"""Per-stage pipeline health from Dagster reads, classified by `openlakeforge_core.health`.

Takes a Dagster client rather than a request so a caller outside the HTTP app, such as a CronJob, can reuse it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from openlakeforge_core.alert_policy import (
    DEFAULT_GRACE,
    Freshness,
    MissedRun,
    Quality,
    ResolvedCondition,
    ResolvedPolicy,
)
from openlakeforge_core.health import (
    HealthState,
    RunObservation,
    RunStatus,
    ScheduleObservation,
    ScheduleState,
    Scope,
    Signal,
    evaluate_execution,
    evaluate_expected_run,
    evaluate_freshness,
    evaluate_freshness_age,
    evaluate_latest_success,
    evaluate_quality,
    rollup,
)
from openlakeforge_core.quality import QualityManifest, is_plain_name
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterRun, DagsterSchedule

from openlakeforge_platform_api.models import IntegrationStatus
from openlakeforge_platform_api.quality_health import QualityReader, quality_signal
from openlakeforge_platform_api.schedules import latest_due, next_due

log = logging.getLogger(__name__)

# Provisional (#1127), and only for a pipeline with no running schedule: a manual pipeline untouched for longer reads
# `unknown`, which is the honest answer about it. A scheduled pipeline's bound is its own cadence (`_run_max_age`).
RUN_MAX_AGE = timedelta(days=7)
# The schedule is read as it is evaluated, so its max_age only matters to the evaluator's contract.
SCHEDULE_MAX_AGE = timedelta(minutes=5)
# Dagster's scheduler ticks once a minute and a run is created after the tick; this leaves room for a slow queue.
SCHEDULE_GRACE = DEFAULT_GRACE
# Runs read per pipeline for its signals: enough to find a recent success without paging. A `range` never changes it,
# so the same runs give the same state whatever window a caller asks for.
RUN_WINDOW = 25
# The bound of every stage-wide read of runs created since a time (ADR 0024, decision 2): the pipeline series, and a
# health window that a job's own RUN_WINDOW runs do not reach back to.
RUN_CAP = 500

RUN_STATUS = {
    "SUCCESS": RunStatus.SUCCEEDED,
    "FAILURE": RunStatus.FAILED,
    "CANCELED": RunStatus.CANCELED,
    "CANCELING": RunStatus.CANCELED,
}

# The definition tags of a product job, `SILVER_DOMAIN_TAG` and `PRODUCT_TAG` in
# packages/runtime/openlakeforge_runtime/product_dagster.py. A job carrying neither belongs to no product.
DOMAIN_TAG = "openlakeforge/silver-domain"
PRODUCT_TAG = "openlakeforge/product"


@dataclass(frozen=True)
class JobScope:
    """The domain and data product a Dagster job loads, as plain names; both None for a job of no product."""

    domain: str | None = None
    product: str | None = None


def job_scope(tags: Mapping[str, str]) -> JobScope:
    """The domain and product a job's definition tags name. A value that is not a plain name (the `manifest_key`
    segment rule) reads None rather than being passed on. A job deployed before the product tag existed has its domain
    and no product: the name is never parsed for one."""

    def plain(key: str) -> str | None:
        value = tags.get(key)
        return value if value is not None and is_plain_name(value) else None

    return JobScope(domain=plain(DOMAIN_TAG), product=plain(PRODUCT_TAG))


@dataclass(frozen=True)
class QualityEvidence:
    """The run a product pipeline's quality signal was read from, and that run's manifest.

    The quality counts route reads the manifest from here rather than again, so its counts and the signal always
    describe the same run. `signal` is None while no run has answered (`_quality`); `manifest` is None when the
    run's manifest is absent or unusable.
    """

    signal: Signal | None
    run_id: str | None
    manifest: QualityManifest | None


@dataclass(frozen=True)
class RunWindow:
    """A job's failed runs created after `since`. `covered_from` is set when the bounded reads stopped short of
    `since`: only runs created from then on were seen, so the count is over that shorter span. `failed` is None when
    the read the count needed failed: an unknown count, never 0."""

    # Creation times, so a caller can count again over a shorter span shared by several jobs.
    failed: tuple[datetime, ...] | None
    covered_from: datetime | None

    @property
    def failed_runs(self) -> int | None:
        return None if self.failed is None else len(self.failed)

    def failed_since(self, start: datetime) -> int | None:
        return None if self.failed is None else sum(created >= start for created in self.failed)


@dataclass(frozen=True)
class PipelineEvaluation:
    job: str
    runs: list[DagsterRun]
    schedule: DagsterSchedule | None
    # When the schedule's cron last came due; None without a schedule or when its cron cannot be read.
    due: datetime | None
    signals: list[Signal]
    # Set for a product pipeline with a run when the stage has a quality reader; None otherwise.
    quality: QualityEvidence | None = None
    # From the job definition's tags (`job_scope`); not part of the signals' scope, which stays keyed by job.
    domain: str | None = None
    product: str | None = None
    # Set only when the caller asked for a window (`read_stage_pipelines(since=...)`); it never feeds the signals.
    window: RunWindow | None = None

    @property
    def state(self) -> HealthState:
        return rollup(self.signals)


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _observation(run: DagsterRun) -> RunObservation:
    return RunObservation(
        status=RUN_STATUS.get(run.status, RunStatus.RUNNING),
        observed_at=at(run.ended_at or run.started_at or run.created_at),
        run=run.run_id,
    )


def _expected(
    schedule: DagsterSchedule | None,
    runs: list[DagsterRun],
    due: datetime | None,
    now: datetime,
    grace: timedelta = SCHEDULE_GRACE,
) -> ScheduleObservation | None:
    """The schedule as the expected-run rule reads it; None, which reads `unknown`, when nothing can be said.

    The due time comes from the cron, never from a tick: a stopped scheduler daemon leaves no tick behind, and a
    due time taken from the last tick would stay on the last day it ran.
    """
    if schedule is None:
        return ScheduleObservation(ScheduleState.ABSENT, now)
    if schedule.status != "RUNNING":
        return ScheduleObservation(ScheduleState.STOPPED, now, declared_running=schedule.default_status == "RUNNING")
    if due is None:
        return None
    # Any run counts, a manual one included: Dagster does not say which run a tick requested.
    last_run = at(runs[0].created_at) if runs else None
    if schedule.started_at is not None:
        # An occurrence before the schedule was switched on was never expected.
        if due < at(schedule.started_at):
            return ScheduleObservation(ScheduleState.RUNNING, now, last_run_started_at=last_run)
    elif schedule.last_tick_at is None and now >= due + grace and not (last_run and last_run >= due):
        # Dagster stores no start time for a schedule that is on by default in code. With no tick either, nothing
        # shows the schedule was on at its due time (it may have been deployed after it), so a missing run is not
        # evidence: `unknown`, never `healthy`.
        return None
    return ScheduleObservation(ScheduleState.RUNNING, now, last_expected_at=due, last_run_started_at=last_run)


def _owed_due(schedule: DagsterSchedule | None, now: datetime, grace: timedelta) -> datetime | None:
    """The latest due time at or before `now - grace`, from the cron alone: the run that should have succeeded.

    Taken straight from the cron rather than stepped back from the latest due time, so a schedule that fires more
    often than the grace (every minute, every five) still owes a run: stepping back one occurrence lands inside the
    grace again. None where the schedule is not running or its cron cannot be read.
    """
    if schedule is None or schedule.status != "RUNNING":
        return None
    return latest_due(schedule.cron_schedule, schedule.timezone, now - grace)


def _evidence_bound(schedule: DagsterSchedule | None, owed: datetime | None, grace: timedelta) -> timedelta:
    """How long a run stays evidence: the gap from the owed due time to the next one, plus the grace.

    A success stays valid until the next due time after it, so a weekday cron's Friday success is still evidence on
    Saturday and Sunday. Without a running schedule, or when the cron cannot be read, the provisional seven days.
    """
    if schedule is None or owed is None:
        return RUN_MAX_AGE
    following = next_due(schedule.cron_schedule, schedule.timezone, owed)
    return following - owed + grace if following else RUN_MAX_AGE


def _owed(schedule: DagsterSchedule | None, runs: list[DagsterRun], owed: datetime | None) -> datetime | None:
    """`owed` when the schedule can be shown to have been on at that time, for the reason given in `_expected`.

    None, which reads `unknown`, otherwise.
    """
    if owed is None or schedule is None:
        return None
    if schedule.started_at is not None:
        return owed if owed >= at(schedule.started_at) else None
    if schedule.last_tick_at is None and not (runs and at(runs[0].created_at) >= owed):
        return None
    return owed


# Dagster's terminal statuses for a run that did not succeed; a run in one of them will publish no further manifest.
_ENDED_UNSUCCESSFUL = {"FAILURE": "failed", "CANCELED": "was canceled"}


def _quality(
    quality: QualityReader,
    scope: Scope,
    job: str,
    tags: Mapping[str, str],
    runs: list[DagsterRun],
    now: datetime,
    max_age: timedelta,
    level: str = "warn",
) -> QualityEvidence | None:
    """The quality signal of the latest run's manifest, where a failed run's blocking errors are, or None for a job
    that is not a product pipeline: one whose definition does not name both its domain and its product.

    The tag values go to the reader as they are, not through `job_scope`: a tag that is not a plain name is refused
    by `manifest_key` there and reads `unknown`, where `job_scope` would make it a job of no product and drop the
    signal.

    The manifest is published after every step and omits dbt's results until dbt's evidence is read, so one from a
    run that did not succeed (in progress, canceled, or failed before dbt reported) may lack the checks still to
    come, and a pass in it proves nothing: a retry that has cleared Silver would resolve the incident the previous
    run's dbt failure opened. Only a successful run's complete manifest can read `healthy`. Runs are walked newest
    first: one that did not succeed answers only with a warning or critical, and is otherwise passed over, so a
    failure between an old success and a Silver-only retry still speaks. The first success answers with its own
    manifest (`unknown` while that is incomplete) and ends the walk.

    A newer run can stop that success from answering `healthy` (#1346): one whose manifest exists but cannot be read
    may be hiding a failure, and one that ended unsuccessfully without completing its manifest never finished its
    checks, so the older pass is not the current quality. Either answers `unknown` in its place, naming the newest
    such run, with its partial manifest where it has one. A run still in progress hides nothing: it may yet finish.
    With no success reached, that run answers the same way; with neither there is no signal, which leaves an open
    incident open, as `unknown` does.
    """
    domain, product = tags.get(DOMAIN_TAG), tags.get(PRODUCT_TAG)
    if domain is None or product is None:
        return None
    hiding: QualityEvidence | None = None
    for run in runs:
        read = quality_signal(quality, scope, job, run.run_id, domain, product, now=now, max_age=max_age, level=level)
        found = read.signal
        if run.status == "SUCCESS":
            if hiding is not None and found.state is HealthState.HEALTHY:
                return hiding
            return QualityEvidence(found, run.run_id, read.manifest)
        if found.state in (HealthState.WARNING, HealthState.CRITICAL):
            return QualityEvidence(found, run.run_id, read.manifest)
        if hiding is not None:
            continue
        if read.unreadable:
            hiding = QualityEvidence(evaluate_quality(scope, None, now=now, max_age=max_age), run.run_id, None)
        elif run.status in _ENDED_UNSUCCESSFUL and not (read.manifest and read.manifest.complete):
            unfinished = replace(
                evaluate_quality(scope, None, now=now, max_age=max_age),
                reason=f"the run {_ENDED_UNSUCCESSFUL[run.status]} before its quality checks finished",
                run=run.run_id,
            )
            hiding = QualityEvidence(unfinished, run.run_id, read.manifest)
    return hiding or QualityEvidence(None, None, None)


def _freshness(
    scope: Scope,
    schedule: DagsterSchedule | None,
    runs: list[DagsterRun],
    success: RunObservation | None,
    state: ScheduleState,
    now: datetime,
    options: Freshness | None,
    grace: timedelta,
) -> Signal:
    """Age bounds from the alert policy when it declares them, in any schedule state; otherwise the cadence."""
    if options and (options.warn_after is not None or options.critical_after is not None):
        return evaluate_freshness_age(
            scope, success, now=now, warn_after=options.warn_after, critical_after=options.critical_after
        )
    owed = _owed(schedule, runs, _owed_due(schedule, now, grace))
    return evaluate_freshness(scope, success, owed, now=now, grace=grace, schedule=state)


def _evaluate(
    stage: str,
    job: str,
    runs: list[DagsterRun],
    schedule: DagsterSchedule | None,
    now: datetime,
    quality: QualityReader | None = None,
    conditions: Mapping[str, ResolvedCondition] | None = None,
    tags: Mapping[str, str] | None = None,
) -> PipelineEvaluation:
    """`tags` are the job definition's. The signals stay scoped by job name: the alert policy matches rules on it and
    incident fingerprints include it (ADR 0023), so the job's domain and product are carried beside the scope."""
    tags = tags or {}
    scope = Scope(stage=stage, product=job)
    missed_options = (conditions or {}).get("missed_run")
    fresh_options = (conditions or {}).get("freshness")
    missed = missed_options.options if missed_options and isinstance(missed_options.options, MissedRun) else None
    freshness = fresh_options.options if fresh_options and isinstance(fresh_options.options, Freshness) else None
    quality_options = (conditions or {}).get("quality")
    level = (
        quality_options.options.level if quality_options and isinstance(quality_options.options, Quality) else "warn"
    )
    grace = missed.grace if missed else SCHEDULE_GRACE
    fresh_grace = freshness.grace if freshness else SCHEDULE_GRACE
    latest = _observation(runs[0]) if runs else None
    # The latest completion, not the newest-created: overlapping runs can finish out of creation order.
    success = max(
        (_observation(run) for run in runs if run.status == "SUCCESS"),
        key=lambda observation: observation.observed_at,
        default=None,
    )
    due = latest_due(schedule.cron_schedule, schedule.timezone, now) if schedule else None
    expected = _expected(schedule, runs, due, now, grace)
    owed = _owed_due(schedule, now, grace)
    max_age = _evidence_bound(schedule, owed, grace)
    if schedule is None:
        state = ScheduleState.ABSENT
    else:
        state = ScheduleState.RUNNING if schedule.status == "RUNNING" else ScheduleState.STOPPED
    signals = [
        evaluate_execution(scope, latest, now=now, max_age=max_age),
        evaluate_latest_success(scope, success, now=now, max_age=max_age),
        evaluate_expected_run(scope, expected, now=now, max_age=SCHEDULE_MAX_AGE, grace=grace),
        _freshness(scope, schedule, runs, success, state, now, freshness, fresh_grace),
    ]
    evidence = _quality(quality, scope, job, tags, runs, now, max_age, level) if quality is not None and runs else None
    if evidence is not None and evidence.signal is not None:
        signals.append(evidence.signal)
    mapped = job_scope(tags)
    return PipelineEvaluation(job, runs, schedule, due, signals, evidence, domain=mapped.domain, product=mapped.product)


def _reaches(runs: list[DagsterRun], since: datetime) -> bool:
    """Whether a job's RUN_WINDOW read holds every run of it created after `since`."""
    return len(runs) < RUN_WINDOW or min(run.created_at for run in runs) <= since.timestamp()


def _window(job: str, runs: list[DagsterRun], stage_runs: list[DagsterRun] | None, since: datetime) -> RunWindow:
    """The job's runs created after `since`, from its own read and, where that stops short, the stage-wide one.

    Each read is complete from its oldest run on, the job's for that job and the stage's for every job, so together
    they cover the job from the older of the two. Creation time places a run, as in the pipeline series.
    """
    seen = {run.run_id: run for run in runs}
    covered_from: datetime | None = None
    if not _reaches(runs, since):
        # `read_stage_pipelines` reads the stage's runs whenever a job's own read stops short, so None here is that
        # read having failed.
        if stage_runs is None:
            return RunWindow(None, None)
        seen.update((run.run_id, run) for run in stage_runs if run.job_name == job)
        if len(stage_runs) >= RUN_CAP:
            oldest = at(min(run.created_at for run in [*runs, *stage_runs]))
            covered_from = oldest if oldest > since else None
    failed = tuple(
        at(run.created_at) for run in seen.values() if run.status == "FAILURE" and run.created_at > since.timestamp()
    )
    return RunWindow(failed, covered_from)


def _stage_runs(client: DagsterClient, stage: str, since: datetime) -> list[DagsterRun] | None:
    """The stage's runs created after `since`, or None when they cannot be read. Only counts depend on this read, so
    its failure must not discard the jobs, schedules and runs already read: a wider range would hide a known signal."""
    try:
        return client.list_runs_created_after(since.timestamp(), limit=RUN_CAP)
    except ServiceClientError as exc:
        log.warning("Dagster for stage %s could not be read for the health window: %s", stage, exc)
        return None


def read_stage_pipelines(
    client: DagsterClient | None,
    stage: str,
    now: datetime,
    quality: QualityReader | None = None,
    policy: ResolvedPolicy | None = None,
    since: datetime | None = None,
) -> tuple[IntegrationStatus, list[PipelineEvaluation]]:
    """The stage's pipelines, and whether Dagster could be read; an unreadable Dagster yields no pipelines.

    With a `quality` reader each product pipeline with a run also carries a quality signal; without one, none.
    With a `policy` each job takes its grace and freshness bounds from the rule that wins for it (ADR 0023, decision
    8); the health routes pass none and keep the cadence-derived signals.

    With `since` each pipeline also carries its `window`, counted from the same reads plus, only when some job's own
    read does not reach back to `since`, one stage-wide read of the runs created after it, capped at RUN_CAP. The
    signals are evaluated from the job's RUN_WINDOW runs either way, so the window never changes a state, and a failed
    stage-wide read leaves the pipelines and their signals as read, with no count where it was needed.
    """
    if client is None:
        absent = IntegrationStatus(name="dagster", status="disabled", reason="no Dagster is configured for this stage")
        return absent, []
    try:
        listed = client.list_jobs()
        jobs = [job.name for job in listed]
        tags = {job.name: job.tags for job in listed}
        schedules = client.list_schedules()
        runs = {job: client.list_runs(job_name=job, limit=RUN_WINDOW) for job in jobs}
    except ServiceClientError as exc:
        # The service's own message goes to the log, not the caller: a Dagster PythonError can carry a stack trace.
        log.warning("Dagster for stage %s could not be read for health: %s", stage, exc)
        return IntegrationStatus(name="dagster", status="unavailable", reason="Dagster could not be read"), []
    # A running schedule wins over a stopped one aimed at the same job: it is written last.
    by_job: dict[str, DagsterSchedule] = {}
    for schedule in sorted(schedules, key=lambda item: item.status == "RUNNING"):
        by_job[schedule.job_name] = schedule
    pipelines = [
        _evaluate(
            stage, job, runs[job], by_job.get(job), now, quality, policy.for_job(job) if policy else None, tags[job]
        )
        for job in jobs
    ]
    if since is not None:
        stage_runs = _stage_runs(client, stage, since) if not all(_reaches(runs[job], since) for job in jobs) else None
        pipelines = [
            replace(pipeline, window=_window(pipeline.job, runs[pipeline.job], stage_runs, since))
            for pipeline in pipelines
        ]
    return IntegrationStatus(name="dagster", status="available", reason="read from Dagster"), pipelines
