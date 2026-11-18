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


class Logs(BaseModel):
    lines: list[LogLine]
    truncated: bool


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


class IntegrationStatus(BaseModel):
    """Whether one source of health is present. `status` is never a health state: an absent source is not green."""

    name: Literal["dagster", "services", "quality", "quality-sync", "metrics"]
    status: Literal["available", "disabled", "unavailable", "not_provided"]
    reason: str


class HealthSignal(BaseModel):
    kind: Literal["execution", "latest_success", "expected_run", "freshness", "quality", "service"]
    state: HealthStateName
    reason: str
    run: str | None
    observed_at: datetime | None
    expires_at: datetime | None


class HealthThresholds(BaseModel):
    """The `max_age` and `grace` values the signals were evaluated with, in seconds. Provisional.

    `run_max_age_seconds` bounds only a pipeline with no running schedule; a scheduled one has its own cadence.
    """

    run_max_age_seconds: int
    schedule_max_age_seconds: int
    schedule_grace_seconds: int
    run_window: int


class StagePipelineState(BaseModel):
    job: str
    state: HealthStateName


class ServiceHealth(BaseModel):
    """One service probe's answer (ADR 0021, decision 7); `component` is e.g. `dagster.workloads`."""

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


class StageHealth(BaseModel):
    stage: str
    state: HealthStateName
    evaluated_at: datetime
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
    state: HealthStateName
    latest_run: Run | None
    last_success: Run | None
    # Failed runs among the `run_window` most recent; a missed run is the `expected_run` signal.
    failed_runs: int
    schedule: PipelineSchedule
    freshness: PipelineFreshness
    signals: list[HealthSignal]


class PipelinesHealth(BaseModel):
    stage: str
    evaluated_at: datetime
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
    domain: str | None
    product: str
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


# The IntegrationStatus vocabulary: a metric's state is never a health state, and an absent series is not a zero one.
MetricState = Literal["available", "disabled", "unavailable", "not_provided"]
MetricRangeName = Literal["1h", "6h", "24h", "7d"]


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
