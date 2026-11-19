"""The run failure summary route (#1271) against a loopback Dagster and a fake manifest store."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from _alerting_fakes import FakeBucket
from conftest import Mint
from fastapi.testclient import TestClient
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult, manifest_key
from test_reads import RUN, FakeDagster, _as
from test_reads import _settings as _api_settings

from openlakeforge_platform_api import run_failure
from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.quality_health import DOMAIN_TAG, QualityReader
from openlakeforge_platform_api.settings import Settings

BASE = "s3://ops/activations/dev/"
RUN_ID = "run-1"
PATH = f"/api/v1/stages/dev/runs/{RUN_ID}/failure"
FAILED = {
    **RUN,
    "runId": RUN_ID,
    "jobName": "orders_pipeline",
    "status": "FAILURE",
    "tags": [{"key": DOMAIN_TAG, "value": "sales"}],
}


class Dagster(FakeDagster):
    """Also answers the event-log read, and keeps every query it was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.runs = [FAILED]
        self.events: list[dict[str, Any]] = []
        self.event_log: dict[str, Any] | None = None
        self.event_log_delay = 0.0
        self.queries: list[str] = []

    def answer(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        if "logsForRun" in query:
            time.sleep(self.event_log_delay)
            return {"logsForRun": self.event_log or {"__typename": "EventConnection", "events": self.events}}
        return super().answer(query, variables)


def _step(key: str, *chain: tuple[str, str]) -> dict[str, Any]:
    """A step failure whose error is `chain[0]`, caused by `chain[1]`, and so on; each is (className, message)."""
    error: dict[str, Any] | None = None
    for class_name, message in reversed(chain):
        error = {"className": class_name, "message": message, "cause": error}
    return {"stepKey": key, "error": error}


def _floe(table: str, check: str, **metrics: int) -> QualityResult:
    return QualityResult(
        engine="floe",
        stage="dev",
        domain="sales",
        product="orders",
        table=table,
        check_id=check,
        engine_severity="reject",
        outcome=QualityOutcome.ERROR,
        pipeline_outcome=PipelineOutcome.QUARANTINED,
        observed_at=datetime.now(UTC),
        dagster_run_id=RUN_ID,
        metrics=metrics,
    )


def _store(bucket: FakeBucket, *results: QualityResult, complete: bool = True) -> None:
    manifest = QualityManifest("dev", "sales", "orders", RUN_ID, results, complete=complete)
    bucket.objects[manifest.ops_key] = manifest.to_json().encode()


@pytest.fixture
def dagster() -> Iterator[Dagster]:
    fake = Dagster()
    yield fake
    fake.close()


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


@pytest.fixture
def api(settings: Settings, dagster: Dagster, bucket: FakeBucket) -> TestClient:
    configured = _api_settings(settings, dagster_graphql_urls={"dev": f"{dagster.url}/graphql"})
    return TestClient(create_app(configured, quality_readers={"dev": QualityReader(bucket, BASE)}))


def _get(api: TestClient, mint: Mint, path: str = PATH) -> dict[str, Any]:
    response = api.get(path, headers=_as(mint, "data-engineer"))
    assert response.status_code == 200, response.text
    return response.json()


def _sources(body: dict[str, Any]) -> dict[str, str]:
    return {source["name"]: source["status"] for source in body["sources"]}


@pytest.mark.parametrize(
    ("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403), ("viewer", 403)]
)
def test_the_route_admits_data_engineer_and_above(api: TestClient, mint: Mint, role: str, status: int) -> None:
    assert api.get(PATH, headers=_as(mint, role)).status_code == status


def test_a_request_without_a_token_is_denied(api: TestClient) -> None:
    assert api.get(PATH).status_code == 401


def test_an_unknown_run_is_not_found(api: TestClient, mint: Mint) -> None:
    response = api.get("/api/v1/stages/dev/runs/missing/failure", headers=_as(mint, "data-engineer"))

    assert response.status_code == 404


def test_a_stage_without_dagster_is_not_found(api: TestClient, mint: Mint) -> None:
    response = api.get(PATH.replace("/dev/", "/prod/"), headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert "no Dagster" in response.json()["detail"]


def test_a_failed_step_names_its_innermost_cause(api: TestClient, mint: Mint, dagster: Dagster) -> None:
    dagster.events = [
        {},
        _step(
            "silver_sales",
            ("DagsterExecutionStepExecutionError", 'Error occurred while executing op "silver_sales":'),
            ("FloeJobError", "Floe job failed"),
            ("FloeSchemaMismatch", "orders.amount expected decimal, found string"),
        ),
    ]

    body = _get(api, mint)

    assert {key: body[key] for key in ("run_id", "job", "status", "run_message", "steps_truncated")} == {
        "run_id": RUN_ID,
        "job": "orders_pipeline",
        "status": "FAILURE",
        "run_message": None,
        "steps_truncated": False,
    }
    assert body["steps"] == [
        {
            "step": "silver_sales",
            "error_class": "FloeSchemaMismatch",
            "message": "orders.amount expected decimal, found string",
            "message_truncated": False,
        }
    ]
    assert _sources(body)["dagster"] == "available"


def test_a_run_killed_with_no_failed_step_carries_the_runs_own_message(
    api: TestClient, mint: Mint, dagster: Dagster
) -> None:
    dagster.events = [{"message": "Run worker pod was terminated: OOMKilled (exit code 137)\nmore detail"}]

    body = _get(api, mint)

    assert (body["steps"], body["run_message"]) == ([], "Run worker pod was terminated: OOMKilled (exit code 137)")


def test_more_than_five_failed_steps_keep_the_first_five_in_event_order(
    api: TestClient, mint: Mint, dagster: Dagster
) -> None:
    dagster.events = [_step(f"step_{n}", ("ValueError", f"bad {n}")) for n in range(7)]
    dagster.events.append({"message": 'Execution of run for "orders_pipeline" failed. Steps failed: [...]'})

    body = _get(api, mint)

    assert [step["step"] for step in body["steps"]] == [f"step_{n}" for n in range(5)]
    assert body["steps_truncated"] is True
    # The run's own failure only restates the steps, so it is not repeated.
    assert body["run_message"] is None


def test_a_message_is_its_first_line_cut_to_the_bound(api: TestClient, mint: Mint, dagster: Dagster) -> None:
    dagster.events = [
        _step("long", ("ValueError", "x" * 1000)),
        _step("multiline", ("ValueError", "first line\nsecond line")),
        _step("exact", ("ValueError", "y" * run_failure.MESSAGE_MAX_CHARS)),
    ]

    long, multiline, exact = _get(api, mint)["steps"]

    assert (long["message"], long["message_truncated"]) == ("x" * run_failure.MESSAGE_MAX_CHARS, True)
    assert (multiline["message"], multiline["message_truncated"]) == ("first line", True)
    assert exact["message_truncated"] is False


def test_secrets_are_redacted_and_a_stack_is_never_fetched(api: TestClient, mint: Mint, dagster: Dagster) -> None:
    secrets = ("hunter2", "alice:s3cr3t", "X-Amz-Signature", "deadbeefcafe", "eyJhbGciOi", "AKIAIOSFODNN7EXAMPLE")
    message = (
        "upload to https://alice:s3cr3t@store.example/bucket/key?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE"
        "&X-Amz-Signature=deadbeefcafe failed with password=hunter2 and Authorization: Bearer "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    )
    dagster.events = [_step("upload", ("OSError", message))]

    body = _get(api, mint)

    served = body["steps"][0]["message"]
    for secret in secrets:
        assert secret not in served
    assert served.startswith("upload to https://<redacted>@store.example/bucket/key?<redacted> failed")
    (failure_read,) = [query for query in dagster.queries if "logsForRun" in query]
    assert "className" in failure_read
    assert "stack" not in failure_read


def test_a_successful_run_has_no_steps_and_no_message(api: TestClient, mint: Mint, dagster: Dagster) -> None:
    dagster.runs = [{**FAILED, "status": "SUCCESS"}]
    # A step that failed once and was retried to success leaves its failure in the event log.
    dagster.events = [_step("flaky", ("TimeoutError", "timed out"))]

    body = _get(api, mint)

    assert (body["steps"], body["run_message"], body["steps_truncated"]) == ([], None, False)
    assert _sources(body)["dagster"] == "available"


@pytest.mark.parametrize(
    "event_log",
    [
        {"__typename": "PythonError", "message": 'Traceback (most recent call last):\n  File "secret.py"'},
        {"__typename": "RunNotFoundError", "message": "gone"},
    ],
)
def test_an_unreadable_event_log_has_no_steps_never_an_empty_list(
    api: TestClient, mint: Mint, dagster: Dagster, event_log: dict[str, Any]
) -> None:
    dagster.event_log = event_log

    body = _get(api, mint)

    assert (body["steps"], body["run_message"]) == (None, None)
    assert _sources(body)["dagster"] == "unavailable"
    assert "secret.py" not in str(body)


def test_an_event_log_past_the_deadline_has_no_steps(
    api: TestClient, mint: Mint, dagster: Dagster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_failure, "READ_DEADLINE_SECONDS", 0.3)
    dagster.event_log_delay = 1.0

    body = _get(api, mint)

    assert body["steps"] is None
    assert _sources(body)["dagster"] == "unavailable"


def test_rows_are_counted_once_per_table_and_summed(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    # Floe repeats an entity's counts on each of its checks.
    _store(
        bucket,
        *(_floe("orders", check, rejected_rows=120, accepted_rows=9880) for check in ("cast", "not_null", "unique")),
        *(_floe("customers", check, rejected_rows=3, accepted_rows=97) for check in ("cast", "not_null")),
        _floe("returns", "cast", violations_total=4),
        complete=False,
    )

    body = _get(api, mint)

    assert body["rows"] == {"rejected": 123, "accepted": 9977, "complete": False}
    assert _sources(body)["quality"] == "available"


def test_a_manifest_with_no_row_counts_has_no_rows_never_zeros(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    _store(bucket, _floe("orders", "cast", violations_total=4))

    body = _get(api, mint)

    assert body["rows"] is None
    assert _sources(body)["quality"] == "available"


def test_an_absent_manifest_has_no_rows(api: TestClient, mint: Mint) -> None:
    body = _get(api, mint)

    assert body["rows"] is None
    assert _sources(body)["quality"] == "not_provided"


@pytest.mark.parametrize("damage", ["corrupt", "store-down"])
def test_an_unreadable_manifest_has_no_rows(api: TestClient, mint: Mint, bucket: FakeBucket, damage: str) -> None:
    if damage == "corrupt":
        bucket.objects["activations/dev/" + manifest_key("sales", "orders", RUN_ID)] = b"{not json"
    else:
        _store(bucket, _floe("orders", "cast", rejected_rows=1, accepted_rows=2))
        bucket.down = True

    body = _get(api, mint)

    assert body["rows"] is None
    assert _sources(body)["quality"] == "unavailable"


def test_a_job_that_is_not_a_product_pipeline_reads_no_manifest(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = [{**FAILED, "jobName": "maintenance_job"}]

    body = _get(api, mint)

    assert body["rows"] is None
    assert _sources(body)["quality"] == "not_provided"
