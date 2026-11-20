"""The JSON each route answers with, so the committed OpenAPI document (ADR 0021, decision 5) describes it.

Field order is the order the keys were written in before these models existed:
a client sees the same bytes, and a reordering here is a visible change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Health(BaseModel):
    status: str


class WhoAmI(BaseModel):
    subject: str
    role: str | None
    email: str | None
    name: str | None
    audience: str


class Job(BaseModel):
    location: str
    repository: str
    name: str
    description: str
    # The domain and data product the job loads, from its definition tags (#1272): plain names, the same as
    # `/data-products` names, never a display name. Null for a job of no product, such as a maintenance job, and for a
    # tag that is not a plain name.
    domain: str | None
    product: str | None


class Jobs(BaseModel):
    jobs: list[Job]


class Run(BaseModel):
    run_id: str
    job_name: str
    status: str
    # Unix seconds as Dagster reports them. `int | float` keeps a whole-number time from becoming `1.0` on the wire.
    created_at: float
    started_at: int | float | None
    ended_at: int | float | None
    tags: dict[str, str]


class Runs(BaseModel):
    runs: list[Run]


class LaunchedRun(BaseModel):
    run_id: str
    job: str
    stage: str
    request_id: str


class LogLine(BaseModel):
    archived_at: str
    namespace: str
    pod: str
    container: str
    line: str | None
    error: str | None
    # v2 records only; `source` says which archive layout the line came from.
    stage: str | None = None
    component: str | None = None
    domain: str | None = None
    product: str | None = None
    run_id: str | None = None
    stream: str | None = None
    missing: list[str] | None = None
    source: str | None = None
    # When the line was written, as the source recorded it (v2's CRI time, the kubelet timestamp a v1 line starts
    # with); null when it recorded none. `level` is one of debug, info, warning, error, critical when the message
    # states it in a recognised shape (`openlakeforge_services.logs.level_of`), else null. Neither is ever guessed.
    timestamp: str | None = None
    level: Literal["debug", "info", "warning", "error", "critical"] | None = None


class Logs(BaseModel):
    lines: list[LogLine]
    truncated: bool


class RunLogs(BaseModel):
    lines: list[LogLine]
    # More lines lie in the run's window than were answered: past `limit`, or past the days one read covers. Ask
    # again with `since` set to the last line's timestamp.
    truncated: bool
    # The window that was read, ISO 8601 UTC: the run's start to its end (now, for a run still going), narrowed by
    # the request's `since` and `until`, and cut after MAX_RUN_LOG_DAYS UTC days. Empty when `since` is not before
    # `until`, as for a `since` past the run's end.
    since: str
    until: str


class ImportedReports(BaseModel):
    dashboards: list[str]
    stage: str
    request_id: str


class QueryStageCapability(BaseModel):
    engine: str
    available: bool
    export_formats: list[str]


class QueryStage(BaseModel):
    stage: str
    query: QueryStageCapability


class QueryCapabilities(BaseModel):
    query_available: bool
    stages: list[QueryStage]


class Schemas(BaseModel):
    schemas: list[str]
    next_cursor: str | None


class Table(BaseModel):
    name: str
    type: Literal["table", "view"]


class Tables(BaseModel):
    tables: list[Table]
    next_cursor: str | None


class QueryColumn(BaseModel):
    name: str
    type: str


class QueryResult(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "cancelled"]
    columns: list[QueryColumn]
    rows: list[list[Any]]
    truncated: bool
    row_count: int
    elapsed_ms: int
    next_cursor: str | None


class Domain(BaseModel):
    name: str
    display_name: str


class Domains(BaseModel):
    domains: list[Domain]


class DataProduct(BaseModel):
    name: str
    display_name: str
    domains: list[str]


class DataProducts(BaseModel):
    data_products: list[DataProduct]


HealthStateName = Literal["healthy", "warning", "critical", "unknown", "disabled"]


IntegrationState = Literal["available", "disabled", "unavailable", "not_provided"]


class IntegrationStatus(BaseModel):
    """Whether one source of health is present. `status` is never a health state: an absent source is not green."""

    name: Literal["dagster", "services", "quality", "quality-sync", "metrics"]
    status: IntegrationState
    reason: str


class MonitoringCapability(BaseModel):
    """Whether a stage is configured with one observability source. Configuration only: `available` says the source is
    there, not that it answers; liveness is the health routes'."""

    name: Literal["dagster", "services", "quality", "quality-sync", "logs", "metrics", "alerts"]
    status: IntegrationState
    reason: str


class MonitoringStage(BaseModel):
    stage: str
    operations: list[str]
    capabilities: list[MonitoringCapability]


class MonitoringCapabilities(BaseModel):
    stages: list[MonitoringStage]


class HealthSignal(BaseModel):
    kind: Literal["execution", "latest_success", "expected_run", "freshness", "quality", "service"]
    state: HealthStateName
    reason: str
    run: str | None
    observed_at: datetime | None
    expires_at: datetime | None
    # The occurrence the signal judged: set by the core signal on `expected_run` when a run was actually expected,
    # null on every other kind. Never `schedule.last_expected_at` or `observed_at` standing in for it.
    due: datetime | None


# One range vocabulary for the metrics routes and the health window (ADR 0024, decision 4): the keys of
# `openlakeforge_core.metrics.RANGES`, which a test holds this to.
MetricRangeName = Literal["1h", "6h", "24h", "7d"]


class HealthWindow(BaseModel):
    """The span the health routes' counts cover: runs created after `from`, up to `to`, the evaluation time.

    `covered_from` is set when the bounded Dagster reads stopped short of `from`: the counts cover runs created from
    then on only, and are never served as the whole window. A state and its signals are never windowed.
    """

    range: MetricRangeName
    from_: datetime = Field(serialization_alias="from")
    to: datetime
    covered_from: datetime | None


class HealthThresholds(BaseModel):
    """The `max_age` and `grace` values the signals were evaluated with, in seconds. Provisional.

    `run_max_age_seconds` bounds only a pipeline with no running schedule; a scheduled one has its own cadence.
    """

    run_max_age_seconds: int
    schedule_max_age_seconds: int
    schedule_grace_seconds: int
    # Runs read per pipeline for its signals, whatever the window.
    run_window: int


class StagePipelineState(BaseModel):
    job: str
    # As on `Job`.
    domain: str | None
    product: str | None
    state: HealthStateName
    # Failed runs created in the window; None when the read the count needed failed, never 0.
    failed_runs: int | None


# The services the probes belong to, so a client can group rows (worst state per `service`) without parsing ids.
# An enum in the committed OpenAPI document: adding one is a reviewed change (ADR 0021, decision 7).
ServiceName = Literal["dagster", "trino", "polaris", "object-store", "log-archive", "quality-sync", "platform-api"]


class ServiceHealth(BaseModel):
    """One service probe's answer (ADR 0021, decision 7); `component` is e.g. `dagster.workloads`.

    `component` is the stable machine key. `display_name` and `service_display_name` are for people and name no host,
    namespace or container; renaming one is a reviewed change, but a client keys on `component` and `service`.
    """

    component: str
    # What was asked, such as `kubernetes`, `dagster-graphql` or `s3`; never an address or a credential.
    source: str
    state: HealthStateName
    reason: str
    observed_at: datetime | None
    expires_at: datetime | None
    # How long the answer stays usable, `expires_at` minus `observed_at`. Probes run on request and are reused for a
    # few seconds, so this is a validity bound and not a polling interval.
    max_age_seconds: int | None
    display_name: str
    service: ServiceName
    service_display_name: str
    # When the component last did its job by its own evidence: the newest archive object's write time for
    # `log-archive.newest-object`, the last published result for `quality-sync.publication`. Null for every other
    # probe, and whenever the state is `unknown` or `disabled`; never the probe time and never a default.
    last_success_at: datetime | None


class StageHealth(BaseModel):
    stage: str
    state: HealthStateName
    evaluated_at: datetime
    window: HealthWindow
    # Failed runs over every pipeline, created from `window.covered_from` when that is set and from `window.from`
    # otherwise; None when Dagster is not configured or cannot be read, or any pipeline's count is None, never 0.
    failed_runs: int | None
    pipelines: list[StagePipelineState]
    services: list[ServiceHealth]
    integrations: list[IntegrationStatus]


class StageServiceHealth(BaseModel):
    """The `services` of StageHealth alone, which needs no Dagster read; the alert evaluator's route (ADR 0023)."""

    stage: str
    services: list[ServiceHealth]


class PipelineSchedule(BaseModel):
    name: str | None
    status: Literal["running", "stopped", "absent"]
    last_expected_at: datetime | None


class PipelineFreshness(BaseModel):
    last_success_at: datetime | None
    age_seconds: int | None


class PipelineHealth(BaseModel):
    job: str
    # As on `Job`.
    domain: str | None
    product: str | None
    state: HealthStateName
    latest_run: Run | None
    last_success: Run | None
    # Failed runs created in the window; a missed run is the `expected_run` signal. None when the read the count
    # needed failed, never 0; the state and signals are read either way.
    failed_runs: int | None
    # Set when this job's bounded reads did not reach back to the window's `from`: `failed_runs` counts from here on.
    covered_from: datetime | None
    schedule: PipelineSchedule
    freshness: PipelineFreshness
    signals: list[HealthSignal]


class PipelinesHealth(BaseModel):
    stage: str
    evaluated_at: datetime
    window: HealthWindow
    # As on `StageHealth`.
    failed_runs: int | None
    thresholds: HealthThresholds
    integrations: list[IntegrationStatus]
    pipelines: list[PipelineHealth]


class QualityCounts(BaseModel):
    """Results of one quality manifest, or a sum of them, by `QualityOutcome`."""

    passed: int
    warn: int
    error: int
    not_run: int
    unknown: int
    # The `error` results that blocked the pipeline; already counted in `error`, never added to it.
    blocking: int


class PipelineQuality(BaseModel):
    """The manifest behind one product pipeline's quality signal: counts and a run id, no check, table or row."""

    job: str
    # As on `Job`.
    domain: str | None
    product: str | None
    # The run whose manifest decided `state`; None while no run has answered.
    run_id: str | None
    # When the manifest's newest result was observed.
    observed_at: datetime | None
    # Whether the run finished writing its manifest; None without a usable manifest.
    complete: bool | None
    # The pipeline's `quality` signal on /health/pipelines; `unknown` where that route carries none.
    state: HealthStateName
    # None when the manifest is absent, unreadable or describes another run: never zeros.
    counts: QualityCounts | None
    reason: str


class StageQualityHealth(BaseModel):
    stage: str
    evaluated_at: datetime
    integrations: list[IntegrationStatus]
    # The sum over pipelines whose counts are known; None when none is, Dagster is unreadable or no reader is set.
    totals: QualityCounts | None
    pipelines: list[PipelineQuality]


class FailedStep(BaseModel):
    """One failed step of a run: its Dagster step key and the innermost cause of its error."""

    step: str
    # The innermost cause's Python class name; None when Dagster reports none.
    error_class: str | None
    # The first line of the innermost cause's message, redacted, then cut to its bound.
    message: str
    # Whether `message` is less than the whole innermost message: a cut line or lines after the first.
    message_truncated: bool


class RunRows(BaseModel):
    """Rows Floe rejected and accepted in a run, summed over its tables from the run's quality manifest."""

    rejected: int
    accepted: int
    # The manifest's own flag: false when the run did not finish writing it, so the sums may be partial.
    complete: bool


class RunFailure(BaseModel):
    """What went wrong in one run, bounded and redacted; a run that did not fail has no steps and no message."""

    run_id: str
    job: str
    status: str
    # At most a few failed steps, in event order. None when the event log could not be read, never an empty list.
    steps: list[FailedStep] | None
    # The run's own failure when no step failed, such as a run pod killed for memory; bounded like a step's message.
    run_message: str | None
    steps_truncated: bool
    # None when the run has no quality manifest, it cannot be read, or no Floe result reports the counts: never zeros.
    rows: RunRows | None
    sources: list[IntegrationStatus]


class AlertStateStatus(BaseModel):
    """Whether the evaluator's state could be read. `stale` is None unless it was, and an old state is never current."""

    status: Literal["available", "unavailable", "not_provided"]
    reason: str
    evaluated_at: datetime | None
    stale: bool | None


class AlertDelivery(BaseModel):
    """The incident's latest notification: queued (`pending`) or how it left the outbox. Counts only, never a receiver.

    `unknown` is a notification settled by an evaluator that did not keep outcomes yet; its counts are None.
    """

    status: Literal["pending", "delivered", "undeliverable", "unrouted", "unknown"]
    event: Literal["opened", "escalated", "resolved"] | None
    attempts: int | None
    receivers: int | None
    delivered: int | None


class AlertIncident(BaseModel):
    """An open incident, from the fields of the ADR 0023 notification payload only, and its job's domain and product."""

    id: str
    condition: str
    severity: Literal["warning", "critical"]
    # The Dagster job the incident is scoped to, which the payload names `product` (ADR 0023); null for a service one.
    job: str | None
    # The job's domain and data product as on `Job`, read from Dagster's job definitions when the route answers; null
    # when the job is of no product, no longer listed, or Dagster could not be read.
    domain: str | None
    product: str | None
    table: str | None
    component: str | None
    opened_at: datetime
    last_seen_at: datetime
    summary: str
    run_ids: list[str]
    delivery: AlertDelivery


class StageAlertIncidents(BaseModel):
    stage: str
    state: AlertStateStatus
    # None whenever the state is not `available`: an unreadable state is not an empty one.
    incidents: list[AlertIncident] | None


# The IntegrationStatus vocabulary: a metric's state is never a health state, and an absent series is not a zero one.
MetricState = Literal["available", "disabled", "unavailable", "not_provided"]


class MetricSeriesInfo(BaseModel):
    """One catalog entry (ADR 0024, decision 4) and whether its source answers now."""

    name: str
    unit: str
    source: Literal["prometheus", "dagster"]
    labels: list[str]
    # The ranges this deployment serves for the series; one past the metrics retention is left out.
    ranges: list[MetricRangeName]
    state: MetricState
    reason: str


class MetricCatalog(BaseModel):
    stage: str
    observed_at: datetime
    series: list[MetricSeriesInfo]


class MetricLine(BaseModel):
    labels: dict[str, str]
    # One [time, value] pair per step, oldest first; a step without a sample is null, never 0.
    points: list[tuple[datetime, float | None]]


class MetricSeriesData(BaseModel):
    series: str
    # None for a platform series: nodes and olf-system components serve every stage.
    stage: str | None
    source: Literal["prometheus", "dagster"]
    state: MetricState
    reason: str
    range: MetricRangeName
    step_seconds: int
    unit: str
    observed_at: datetime
    # False only for a Dagster series whose read reached its run cap: every step before `covered_from` is null.
    complete: bool
    covered_from: datetime | None
    lines: list[MetricLine]


class FileStageCapability(BaseModel):
    stage: str
    operations: list[str]


class FileCapabilities(BaseModel):
    stages: list[FileStageCapability]
    max_upload_bytes: int


class Bucket(BaseModel):
    name: str
    created_at: str | None


class Buckets(BaseModel):
    buckets: list[Bucket]


class Folder(BaseModel):
    prefix: str


class FileObject(BaseModel):
    key: str
    size: int
    last_modified: str


class Objects(BaseModel):
    prefix: str
    folders: list[Folder]
    files: list[FileObject]
    next_cursor: str | None


class UploadedObject(BaseModel):
    key: str
    size: int


class CreateFolderRequest(BaseModel):
    prefix: str


class CreatedFolder(BaseModel):
    prefix: str


class RenameRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # `from` is a Python keyword; the wire name is the contract.
    source: str = Field(alias="from")
    target: str = Field(alias="to")


class Renamed(BaseModel):
    moved: int


class DeleteRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=1000)


class DeleteError(BaseModel):
    key: str
    detail: str


class Deleted(BaseModel):
    deleted: list[str]
    errors: list[DeleteError]
