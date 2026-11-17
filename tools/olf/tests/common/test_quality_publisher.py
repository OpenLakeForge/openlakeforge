"""The quality publisher against an in-memory OpenMetadata and ops bucket (#797)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import pytest
from openlakeforge_core.quality import (
    PipelineOutcome,
    QualityManifest,
    QualityOutcome,
    QualityResult,
)
from openlakeforge_core.quality_sync import MAX_ATTEMPTS, OmStatus, ResultState, RunSync, SyncSummary, om_status
from openlakeforge_services.openmetadata import OpenMetadataError, OpenMetadataTransientError
from openlakeforge_services.quality_publisher import (
    QualityCatalog,
    QualityPublisher,
    StoreUnavailableError,
)

T0 = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
BASE = "s3://ops/activations/dev"
DB = "polaris.lakehouse_dev"
SILVER = f"{DB}.sales_silver.orders"
GOLD = f"{DB}.sales_gold.revenue"
STATE_KEY = "activations/dev/quality-sync/state/sales/revenue/run-1.json"
MANIFEST_KEY = "activations/dev/run-artifacts/quality/sales/revenue/run-1/manifest.json"


class FakeOpenMetadata:
    """Just enough of OpenMetadata 1.13.6's data quality routes: unknown tables are 404, results are keyed by
    timestamp, and a repeated timestamp is rejected the way the time-series table would."""

    def __init__(self, tables: dict[str, list[str]]) -> None:
        self.tables = tables
        self.definitions: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self.results: dict[str, dict[int, dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.refuse: int | None = None

    def request(self, method: str, path: str, *, json_body: Any = None, params: Any = None, ok_statuses=(200,)) -> Any:
        self.calls.append((method, path))
        if self.down:
            raise OpenMetadataTransientError("connection refused")
        if self.refuse:
            raise OpenMetadataError("refused", status=self.refuse)
        if path.startswith("/api/v1/tables/name/"):
            fqn = unquote(path.rsplit("/", 1)[1])
            if fqn not in self.tables:
                raise OpenMetadataError("not found", status=404)
            return {"fullyQualifiedName": fqn, "columns": [{"name": c} for c in self.tables[fqn]]}
        if path == "/api/v1/dataQuality/testDefinitions":
            self.definitions[json_body["name"]] = json_body
            return {}
        if path == "/api/v1/dataQuality/testCases":
            link = json_body["entityLink"]
            assert link.startswith("<#E::table::") and link.endswith(">")
            table, _, column = link[len("<#E::table::") : -1].partition("::columns::")
            fqn = f"{table}.{column + '.' if column else ''}{json_body['name']}"
            assert json_body["testDefinition"] in self.definitions
            self.cases[fqn] = json_body
            return {}
        if path.startswith("/api/v1/dataQuality/testCases/testCaseResults/"):
            fqn = unquote(path.rsplit("/", 1)[1])
            assert fqn in self.cases, "a result for a case that does not exist"
            history = self.results.setdefault(fqn, {})
            if method == "GET":
                ts = params["startTs"]
                return {"data": [history[ts]] if ts in history else []}
            assert json_body["timestamp"] not in history, "duplicate result timestamp"
            history[json_body["timestamp"]] = json_body
            return {}
        raise AssertionError(f"unexpected {method} {path}")


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime, str]] = {}
        self.down = False

    def put(self, key: str, body: str, modified: datetime) -> None:
        self.objects[key] = (body.encode(), modified, f'"{abs(hash(body))}"')

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if self.down:
            raise RuntimeError("bucket down")
        if Key not in self.objects:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}  # type: ignore[attr-defined]
            raise error
        return {"Body": _Body(self.objects[Key][0])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        if self.down:
            raise RuntimeError("bucket down")
        self.put(Key, Body.decode(), datetime.now(UTC))

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", ContinuationToken: str = "") -> dict:  # noqa: N803
        if self.down:
            raise RuntimeError("bucket down")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {
            "Contents": [{"Key": k, "ETag": self.objects[k][2], "LastModified": self.objects[k][1]} for k in keys],
            "IsTruncated": False,
        }


def result(
    check: str,
    outcome: QualityOutcome = QualityOutcome.PASSED,
    *,
    engine: str = "floe",
    table: str | None = "orders",
    column: str | None = None,
    run: str = "run-1",
    stage: str = "dev",
    pipeline: PipelineOutcome = PipelineOutcome.CONTINUED,
    severity: str = "warn",
    at: datetime = T0,
) -> QualityResult:
    return QualityResult(
        engine=engine,
        stage=stage,
        domain="sales",
        product="revenue",
        table=table,
        column=column,
        check_id=check,
        engine_severity=severity,
        outcome=outcome,
        pipeline_outcome=pipeline,
        observed_at=at,
        dagster_run_id=run,
        artifact_uri="s3://ops/activations/dev/floe/reports/sales/run_1/orders/run.json",
        metrics={"rejected_rows": 3},
    )


def put_manifest(
    bucket: FakeBucket,
    results: list[QualityResult],
    *,
    run: str = "run-1",
    stage: str = "dev",
    key_stage: str = "dev",
    modified: datetime = T0,
) -> None:
    manifest = QualityManifest(
        stage=stage, domain="sales", product="revenue", dagster_run_id=run, results=tuple(results), complete=True
    )
    bucket.put(f"activations/{key_stage}/{manifest.key}", manifest.to_json(), modified)


def publisher(bucket: FakeBucket, om: FakeOpenMetadata, **kwargs: Any) -> QualityPublisher:
    return QualityPublisher(
        store=bucket,  # type: ignore[arg-type]
        base_uri=BASE,
        stage="dev",
        catalog=QualityCatalog(om),  # type: ignore[arg-type]
        database_fqn=kwargs.pop("database_fqn", DB),
        **kwargs,
    )


@pytest.fixture
def om() -> FakeOpenMetadata:
    return FakeOpenMetadata({SILVER: ["id"], GOLD: ["id", "amount"]})


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


def summary(bucket: FakeBucket) -> SyncSummary:
    return SyncSummary.from_json(bucket.objects["activations/dev/quality-sync/summary.json"][0].decode())


def test_publishes_floe_and_dbt_results_under_existing_tables(bucket, om):
    put_manifest(
        bucket,
        [
            result("floe_not_null", QualityOutcome.WARN, severity="warn"),
            result("floe_unique", QualityOutcome.ERROR, severity="abort", pipeline=PipelineOutcome.BLOCKED),
            result(
                "not_null_revenue_amount", engine="dbt", table="sales_gold.revenue", column="amount", severity="error"
            ),
        ],
    )

    report = publisher(bucket, om).reconcile(now=T0 + timedelta(minutes=1))

    assert report.published == 3
    ts = int(T0.timestamp() * 1000)
    warn = om.results[f"{SILVER}.olf.floe.floe_not_null"][ts]
    assert warn["testCaseStatus"] == "Aborted"
    assert {"name": "olf_outcome", "value": "warn"} in warn["testResultValue"]
    assert {"name": "engine_severity", "value": "warn"} in warn["testResultValue"]
    assert om.results[f"{SILVER}.olf.floe.floe_unique"][ts]["testCaseStatus"] == "Failed"
    assert om.results[f"{GOLD}.amount.olf.dbt.not_null_revenue_amount"][ts]["testCaseStatus"] == "Success"
    assert om.cases[f"{GOLD}.amount.olf.dbt.not_null_revenue_amount"]["entityLink"].endswith("::columns::amount>")
    assert summary(bucket).pending == 0
    assert summary(bucket).lag_seconds is None


def test_warn_status_can_be_failed_by_the_operator(bucket, om):
    put_manifest(bucket, [result("floe_not_null", QualityOutcome.WARN)])
    publisher(bucket, om, warn_status=OmStatus.FAILED).reconcile(now=T0)
    assert next(iter(om.results[f"{SILVER}.olf.floe.floe_not_null"].values()))["testCaseStatus"] == "Failed"


def test_warn_is_never_published_as_success():
    assert om_status(QualityOutcome.WARN) is OmStatus.ABORTED
    assert om_status(QualityOutcome.WARN, warn_status=OmStatus.FAILED) is OmStatus.FAILED
    assert om_status(QualityOutcome.NOT_RUN) is None
    assert om_status(QualityOutcome.UNKNOWN) is None


def test_unexecuted_checks_publish_nothing(bucket, om):
    put_manifest(bucket, [result("floe_unique", QualityOutcome.NOT_RUN, pipeline=PipelineOutcome.UNKNOWN)])
    publisher(bucket, om).reconcile(now=T0)
    assert om.results == {}
    assert om.cases == {}


def test_a_second_run_adds_history_to_the_same_case(bucket, om):
    put_manifest(bucket, [result("floe_not_null")])
    put_manifest(bucket, [result("floe_not_null", run="run-2", at=T0 + timedelta(hours=1))], run="run-2")
    publisher(bucket, om).reconcile(now=T0 + timedelta(hours=2))
    assert list(om.cases) == [f"{SILVER}.olf.floe.floe_not_null"]
    assert len(om.results[f"{SILVER}.olf.floe.floe_not_null"]) == 2


def test_replaying_the_same_run_is_idempotent(bucket, om):
    put_manifest(bucket, [result("floe_not_null")])
    pub = publisher(bucket, om)
    pub.reconcile(now=T0)
    pub.reconcile(now=T0 + timedelta(minutes=5), run_id="run-1")
    # Even with the state gone, the catalog's own timestamp is what stops a second result.
    del bucket.objects[STATE_KEY]
    report = pub.reconcile(now=T0 + timedelta(minutes=10), run_id="run-1")
    assert report.published == 1
    assert len(om.results[f"{SILVER}.olf.floe.floe_not_null"]) == 1


def test_outage_leaves_a_retriable_pending_state_with_lag(bucket, om):
    put_manifest(bucket, [result("floe_not_null"), result("floe_unique")])
    om.down = True
    report = publisher(bucket, om).reconcile(now=T0 + timedelta(minutes=10))

    assert report.interrupted
    assert report.published == 0
    s = summary(bucket)
    assert s.pending == 2
    assert s.lag_seconds == 600
    assert s.last_failure_at is not None
    assert s.last_success_at is None
    assert "unavailable" in (s.last_error or "")

    # The pass stopped at the first failure, so one result was never tried and goes out at once; the one that
    # failed waits out its backoff.
    om.down = False
    assert publisher(bucket, om).reconcile(now=T0 + timedelta(minutes=10, seconds=5)).published == 1
    assert summary(bucket).pending == 1

    assert publisher(bucket, om).reconcile(now=T0 + timedelta(minutes=30)).published == 1
    assert summary(bucket).pending == 0
    assert summary(bucket).last_success_at is not None


def test_unregistered_table_stays_pending_and_publishes_once_registered(bucket, om):
    put_manifest(bucket, [result("floe_not_null")])
    del om.tables[SILVER]
    pub = publisher(bucket, om)
    assert pub.reconcile(now=T0).deferred == 1
    assert om.cases == {}
    assert "not registered" in (summary(bucket).last_error or "")
    om.tables[SILVER] = ["id"]
    assert pub.reconcile(now=T0 + timedelta(hours=1)).published == 1


def test_results_that_never_find_their_table_end_failed_and_replay_resets_them(bucket, om):
    put_manifest(bucket, [result("floe_not_null")])
    del om.tables[SILVER]
    pub = publisher(bucket, om)
    now = T0
    for _ in range(MAX_ATTEMPTS):
        now += timedelta(hours=2)
        pub.reconcile(now=now)
    assert summary(bucket).failed == 1
    assert summary(bucket).pending == 0
    state = RunSync.from_json(bucket.objects[STATE_KEY][0].decode())
    assert state.count(ResultState.FAILED) == 1

    om.tables[SILVER] = ["id"]
    assert pub.reconcile(now=now + timedelta(hours=2)).published == 0
    assert pub.reconcile(now=now + timedelta(hours=3), retry_failed=True).published == 1


def test_a_refused_credential_is_retried_not_dropped(bucket, om):
    put_manifest(bucket, [result("floe_not_null")])
    om.refuse = 401
    pub = publisher(bucket, om)
    assert pub.reconcile(now=T0).failed == 1
    assert "HTTP 401" in (summary(bucket).last_error or "")
    om.refuse = None
    assert pub.reconcile(now=T0 + timedelta(hours=1)).published == 1


def test_a_manifest_of_another_stage_under_this_stages_prefix_is_not_published(bucket, om):
    om.tables["polaris.lakehouse_prod.sales_silver.orders"] = ["id"]
    put_manifest(bucket, [result("floe_not_null", stage="prod")], stage="prod", key_stage="dev")
    assert publisher(bucket, om).reconcile(now=T0).published == 0
    assert not any("lakehouse_prod" in path for _, path in om.calls)
    assert om.cases == {}


def test_a_publisher_refuses_another_stages_database(bucket, om):
    with pytest.raises(ValueError, match="lakehouse_dev"):
        publisher(bucket, om, database_fqn="polaris.lakehouse_prod")


def test_batches_are_bounded_oldest_first(bucket, om):
    for i in range(3):
        run = f"run-{i}"
        at = T0 + timedelta(hours=i)
        put_manifest(bucket, [result("floe_not_null", run=run, at=at)], run=run, modified=at)
    pub = publisher(bucket, om)
    now = T0 + timedelta(days=1)
    assert pub.reconcile(now=now, limit=2).runs_examined == 2
    assert len(om.results[f"{SILVER}.olf.floe.floe_not_null"]) == 2
    assert summary(bucket).pending == 0
    assert pub.reconcile(now=now, limit=2).runs_examined == 1
    assert len(om.results[f"{SILVER}.olf.floe.floe_not_null"]) == 3


def test_a_dbt_result_without_table_identity_is_not_guessed(bucket, om):
    put_manifest(bucket, [result("t", engine="dbt", table=None)])
    publisher(bucket, om).reconcile(now=T0)
    assert om.cases == {}
    assert summary(bucket).pending == 0


def test_requests_carry_no_sample_data(bucket, om):
    put_manifest(bucket, [result("floe_cast_error", QualityOutcome.ERROR, pipeline=PipelineOutcome.QUARANTINED)])
    publisher(bucket, om).reconcile(now=T0)
    body = json.dumps(om.results[f"{SILVER}.olf.floe.floe_cast_error"])
    assert "sampleData" not in body
    assert "secret" not in body.lower()


def test_an_unreadable_bucket_is_an_error_not_a_silent_pass(bucket, om):
    bucket.down = True
    with pytest.raises(StoreUnavailableError):
        publisher(bucket, om).reconcile(now=T0)
    assert om.calls == []
