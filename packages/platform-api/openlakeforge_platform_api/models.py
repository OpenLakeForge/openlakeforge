"""The JSON each route answers with, so the committed OpenAPI document (ADR 0021, decision 5) describes it.

Field order is the order the keys were written in before these models existed:
a client sees the same bytes, and a reordering here is a visible change.
"""

from __future__ import annotations

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
