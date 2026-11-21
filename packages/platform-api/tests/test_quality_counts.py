"""The per-stage quality counts route (#1257) against a loopback Dagster and a fake manifest store."""

from __future__ import annotations

import time
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

import pytest
from _alerting_fakes import FakeBucket
from conftest import Mint
from fastapi.testclient import TestClient
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult, manifest_key
from test_health import ScheduledDagster
from test_reads import _as
from test_reads import _settings as _api_settings

from openlakeforge_platform_api import quality_routes
from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.pipeline_health import DOMAIN_TAG, PRODUCT_TAG
from openlakeforge_platform_api.quality_health import QualityReader
from openlakeforge_platform_api.settings import Settings

BASE = "s3://ops/activations/dev/"
PREFIX = "activations/dev/"
JOB = "orders_pipeline"
QUALITY = "/api/v1/stages/dev/health/quality"
PIPELINES = "/api/v1/stages/dev/health/pipelines"


class Dagster(ScheduledDagster):
    """Jobs whose definitions name their domain and product, and their runs newest first per job."""

    def __init__(self) -> None:
        super().__init__()
        self.tagged: dict[str, dict[str, str]] = {JOB: _product("orders")}
        self.runs = []

    def answer(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "query Jobs" in query:
            jobs = [
                {"name": job, "description": "", "tags": [{"key": k, "value": v} for k, v in tags.items()]}
                for job, tags in self.tagged.items()
            ]
            repository = {"name": "__repository__", "location": {"name": "loc"}, "jobs": jobs}
            return {"repositoriesOrError": {"__typename": "RepositoryConnection", "nodes": [repository]}}
        return super().answer(query, variables)


def _product(product: str, domain: str = "sales") -> dict[str, str]:
    return {DOMAIN_TAG: domain, PRODUCT_TAG: product}


def _runs(*statuses: str, job: str = JOB) -> list[dict[str, Any]]:
    """`run-1` is the oldest; Dagster answers newest first."""
    stamp = time.time() - 1800
    # The product is read from the job's definition, never a run's tags, so these name none.
    tags = [{"key": "dagster/schedule_name", "value": "daily"}]
    runs = [
        {
            "runId": f"{job}-run-{n}" if job != JOB else f"run-{n}",
            "jobName": job,
            "status": status,
            "creationTime": stamp + n,
            "startTime": stamp + n + 1,
            "endTime": None if status == "STARTED" else stamp + n + 60,
            "tags": tags,
        }
        for n, status in enumerate(statuses, 1)
    ]
    return list(reversed(runs))


def _result(
    outcome: QualityOutcome,
    pipeline_outcome: PipelineOutcome = PipelineOutcome.CONTINUED,
    *,
    check: str = "not_null_customer_secret",
    run_id: str = "run-1",
    product: str = "orders",
) -> QualityResult:
    return QualityResult(
        engine="floe",
        stage="dev",
        domain="sales",
        product=product,
        table="fct_secret_table",
        check_id=check,
        engine_severity="error",
        outcome=outcome,
        pipeline_outcome=pipeline_outcome,
        observed_at=datetime.now(UTC) - timedelta(minutes=20),
        dagster_run_id=run_id,
        artifact_uri="s3://ops/evidence/run-1/floe.json",
    )


def _store(bucket: FakeBucket, *results: QualityResult, complete: bool = True) -> FakeBucket:
    first = results[0]
    assert first.product is not None
    manifest = QualityManifest("dev", "sales", first.product, first.dagster_run_id, results, complete=complete)
    bucket.objects[manifest.ops_key] = manifest.to_json().encode()
    return bucket


@pytest.fixture
def dagster() -> Iterator[Dagster]:
    fake = Dagster()
    yield fake
    fake.close()


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


def _app(settings: Settings, dagster: Dagster, readers: dict[str, QualityReader], **kwargs: Any) -> TestClient:
    configured = _api_settings(settings, dagster_graphql_urls={"dev": f"{dagster.url}/graphql"})
    return TestClient(create_app(configured, quality_readers=readers, **kwargs))


@pytest.fixture
def api(settings: Settings, dagster: Dagster, bucket: FakeBucket) -> TestClient:
    return _app(settings, dagster, {"dev": QualityReader(bucket, BASE)})


def _get(api: TestClient, mint: Mint, path: str = QUALITY) -> dict[str, Any]:
    response = api.get(path, headers=_as(mint, "data-engineer"))
    assert response.status_code == 200, response.text
    return response.json()


def _only(api: TestClient, mint: Mint) -> dict[str, Any]:
    (pipeline,) = _get(api, mint)["pipelines"]
    return pipeline


def _counts(**found: int) -> dict[str, int]:
    return {"passed": 0, "warn": 0, "error": 0, "not_run": 0, "unknown": 0, "blocking": 0, **found}


@pytest.mark.parametrize(
    ("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403), ("viewer", 403)]
)
def test_the_route_admits_data_engineer_and_above(api: TestClient, mint: Mint, role: str, status: int) -> None:
    assert api.get(QUALITY, headers=_as(mint, role)).status_code == status


def test_a_request_without_a_token_is_denied(api: TestClient) -> None:
    assert api.get(QUALITY).status_code == 401


def test_a_stage_that_is_not_enabled_is_refused(api: TestClient, mint: Mint) -> None:
    assert api.get(QUALITY.replace("/dev/", "/staging/"), headers=_as(mint, "platform-admin")).status_code == 404


def test_a_stage_the_role_is_not_granted_is_refused_as_the_pipeline_health_route_refuses_it(
    settings: Settings, dagster: Dagster, bucket: FakeBucket, mint: Mint
) -> None:
    shipped = tomllib.loads(
        resources.files("openlakeforge_platform_api").joinpath("permissions.toml").read_text(encoding="utf-8")
    )
    for name in ("get_stage_quality_health", "list_pipeline_health"):
        shipped["operations"][name]["role_stages"] = {"data-engineer": ["dev"]}
    matrix = PermissionMatrix.from_mapping(shipped, settings.precedence)
    api = _app(settings, dagster, {"dev": QualityReader(bucket, BASE)}, permissions=matrix)

    quality = api.get(QUALITY.replace("/dev/", "/prod/"), headers=_as(mint, "data-engineer"))
    pipelines = api.get(PIPELINES.replace("/dev/", "/prod/"), headers=_as(mint, "data-engineer"))

    assert quality.status_code == pipelines.status_code == 403
    assert quality.json()["detail"] == pipelines.json()["detail"].replace(
        "list_pipeline_health", "get_stage_quality_health"
    )
    assert api.get(QUALITY, headers=_as(mint, "data-engineer")).status_code == 200


def test_a_stage_without_dagster_is_not_found(api: TestClient, mint: Mint) -> None:
    assert api.get(QUALITY.replace("/dev/", "/prod/"), headers=_as(mint, "platform-admin")).status_code == 404


def test_a_warn_is_counted_and_reads_warning(api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket) -> None:
    dagster.runs = _runs("SUCCESS")
    _store(bucket, _result(QualityOutcome.PASSED, check="a"), _result(QualityOutcome.WARN, check="b"))

    body = _get(api, mint)

    (pipeline,) = body["pipelines"]
    assert {key: pipeline[key] for key in ("job", "domain", "product", "run_id", "complete", "state")} == {
        "job": JOB,
        "domain": "sales",
        "product": "orders",
        "run_id": "run-1",
        "complete": True,
        "state": "warning",
    }
    assert pipeline["counts"] == _counts(passed=1, warn=1)
    assert body["totals"] == pipeline["counts"]
    assert {item["name"]: item["status"] for item in body["integrations"]} == {
        "dagster": "available",
        "quality": "available",
    }


def test_a_blocking_error_is_counted_as_an_error_and_as_blocking(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("FAILURE")
    _store(
        bucket,
        _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, check="a"),
        _result(QualityOutcome.ERROR, PipelineOutcome.QUARANTINED, check="b"),
        _result(QualityOutcome.PASSED, check="c"),
    )

    pipeline = _only(api, mint)

    assert pipeline["state"] == "critical"
    assert pipeline["counts"] == _counts(passed=1, error=2, blocking=1)


def test_a_check_that_did_not_run_is_counted_and_the_pass_beside_it_is_not_healthy(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("SUCCESS")
    _store(bucket, _result(QualityOutcome.PASSED, check="a"), _result(QualityOutcome.NOT_RUN, check="b"))

    pipeline = _only(api, mint)

    assert pipeline["state"] == "unknown"
    assert pipeline["counts"] == _counts(passed=1, not_run=1)


def test_an_incomplete_manifest_keeps_its_counts_and_is_never_healthy(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("SUCCESS")
    _store(bucket, _result(QualityOutcome.PASSED), complete=False)

    pipeline = _only(api, mint)

    assert (pipeline["state"], pipeline["complete"]) == ("unknown", False)
    assert pipeline["counts"] == _counts(passed=1)


def test_an_absent_manifest_has_no_counts_never_zeros(api: TestClient, mint: Mint, dagster: Dagster) -> None:
    dagster.runs = _runs("SUCCESS")

    body = _get(api, mint)

    (pipeline,) = body["pipelines"]
    assert (pipeline["state"], pipeline["counts"], pipeline["complete"]) == ("unknown", None, None)
    assert pipeline["run_id"] == "run-1"
    assert body["totals"] is None


@pytest.mark.parametrize("damage", ["corrupt", "foreign", "store-down"])
def test_an_unusable_manifest_has_no_counts(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket, damage: str
) -> None:
    dagster.runs = _runs("SUCCESS")
    key = PREFIX + manifest_key("sales", "orders", "run-1")
    if damage == "corrupt":
        bucket.objects[key] = b"{not json"
    elif damage == "foreign":
        other = QualityManifest("dev", "sales", "orders", "run-9", (_result(QualityOutcome.PASSED, run_id="run-9"),))
        bucket.objects[key] = other.to_json().encode()
    else:
        _store(bucket, _result(QualityOutcome.PASSED))
        bucket.down = True

    pipeline = _only(api, mint)

    assert (pipeline["state"], pipeline["counts"]) == ("unknown", None)


def test_a_success_after_an_unreadable_manifest_names_the_unreadable_run(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("SUCCESS", "FAILURE")
    _store(bucket, _result(QualityOutcome.PASSED))
    bucket.objects[PREFIX + manifest_key("sales", "orders", "run-2")] = b"{not json"

    pipeline = _only(api, mint)

    assert (pipeline["state"], pipeline["run_id"], pipeline["counts"]) == ("unknown", "run-2", None)


def test_a_pipeline_with_no_finished_run_is_listed_as_unknown(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("STARTED")
    _store(bucket, _result(QualityOutcome.PASSED))

    body = _get(api, mint)

    (pipeline,) = body["pipelines"]
    assert (pipeline["state"], pipeline["run_id"], pipeline["counts"]) == ("unknown", None, None)
    assert body["totals"] is None


def test_a_latest_complete_success_reads_its_own_counts_and_run(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("SUCCESS", "SUCCESS")
    _store(bucket, _result(QualityOutcome.PASSED))
    _store(
        bucket, _result(QualityOutcome.PASSED, run_id="run-2"), _result(QualityOutcome.WARN, check="b", run_id="run-2")
    )

    pipeline = _only(api, mint)

    assert (pipeline["state"], pipeline["run_id"], pipeline["complete"]) == ("warning", "run-2", True)
    assert pipeline["counts"] == _counts(passed=1, warn=1)


@pytest.mark.parametrize("status", ["FAILURE", "CANCELED"])
def test_a_latest_run_that_ended_with_an_incomplete_manifest_is_unknown_with_its_partial_counts(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket, status: str
) -> None:
    dagster.runs = _runs("SUCCESS", status)
    _store(bucket, *(_result(QualityOutcome.PASSED, check=f"c{n}") for n in range(3)))
    _store(bucket, _result(QualityOutcome.PASSED, run_id="run-2"), complete=False)

    body = _get(api, mint)

    (pipeline,) = body["pipelines"]
    assert (pipeline["state"], pipeline["run_id"], pipeline["complete"]) == ("unknown", "run-2", False)
    assert pipeline["reason"].endswith("before its quality checks finished")
    # The older run's three passes are not the current quality, nor part of the stage's sum.
    assert pipeline["counts"] == _counts(passed=1)
    assert body["totals"] == _counts(passed=1)


def test_a_latest_failed_run_with_no_manifest_is_unknown_with_no_counts(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("SUCCESS", "FAILURE")
    _store(bucket, _result(QualityOutcome.PASSED))

    body = _get(api, mint)

    (pipeline,) = body["pipelines"]
    assert (pipeline["state"], pipeline["run_id"], pipeline["complete"], pipeline["counts"]) == (
        "unknown",
        "run-2",
        None,
        None,
    )
    assert pipeline["reason"] == "the run failed before its quality checks finished"
    assert body["totals"] is None


def test_a_latest_run_in_progress_leaves_the_last_complete_success_answering(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("SUCCESS", "STARTED")
    _store(bucket, _result(QualityOutcome.PASSED))
    _store(bucket, _result(QualityOutcome.PASSED, run_id="run-2"), complete=False)

    pipeline = _only(api, mint)

    assert (pipeline["state"], pipeline["run_id"], pipeline["complete"]) == ("healthy", "run-1", True)


def test_a_pipeline_with_no_runs_is_not_listed(api: TestClient, mint: Mint, dagster: Dagster) -> None:
    body = _get(api, mint)

    assert (body["pipelines"], body["totals"]) == ([], None)


@pytest.mark.parametrize(
    ("statuses", "manifests"),
    [
        (("SUCCESS",), [("run-1", QualityOutcome.PASSED, PipelineOutcome.CONTINUED)]),
        (("SUCCESS",), [("run-1", QualityOutcome.WARN, PipelineOutcome.CONTINUED)]),
        (("SUCCESS", "FAILURE"), [("run-2", QualityOutcome.ERROR, PipelineOutcome.BLOCKED)]),
        (
            ("SUCCESS", "FAILURE", "STARTED"),
            [
                ("run-1", QualityOutcome.PASSED, PipelineOutcome.CONTINUED),
                ("run-2", QualityOutcome.ERROR, PipelineOutcome.BLOCKED),
                ("run-3", QualityOutcome.PASSED, PipelineOutcome.CONTINUED),
            ],
        ),
        (("SUCCESS",), []),
        (("SUCCESS", "FAILURE"), [("run-1", QualityOutcome.PASSED, PipelineOutcome.CONTINUED)]),
    ],
    ids=["passed", "warn", "blocked-after-success", "failure-before-a-retry", "absent", "ended-before-its-checks"],
)
def test_the_state_is_the_quality_signal_of_the_pipeline_health_route(
    api: TestClient,
    mint: Mint,
    dagster: Dagster,
    bucket: FakeBucket,
    statuses: tuple[str, ...],
    manifests: list[tuple[str, QualityOutcome, PipelineOutcome]],
) -> None:
    dagster.runs = _runs(*statuses)
    for run_id, outcome, pipeline_outcome in manifests:
        _store(bucket, _result(outcome, pipeline_outcome, run_id=run_id))

    quality = _only(api, mint)
    (health,) = _get(api, mint, PIPELINES)["pipelines"]
    signal = next(signal for signal in health["signals"] if signal["kind"] == "quality")

    assert (quality["state"], quality["reason"]) == (signal["state"], signal["reason"])
    # A signal read from no manifest names no run; the counts route still names the run whose manifest it looked for.
    assert signal["run"] in (None, quality["run_id"])


def test_totals_sum_the_pipelines_whose_counts_are_known(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.tagged = {
        JOB: _product("orders"),
        "invoices_pipeline": _product("invoices"),
        "refunds_pipeline": _product("refunds"),
        "gold_maintenance": {},
    }
    dagster.runs = (
        _runs("SUCCESS")
        + _runs("FAILURE", job="invoices_pipeline")
        + _runs("SUCCESS", job="refunds_pipeline")
        + _runs("SUCCESS", job="gold_maintenance")
    )
    _store(bucket, _result(QualityOutcome.PASSED, check="a"), _result(QualityOutcome.WARN, check="b"))
    _store(
        bucket,
        _result(
            QualityOutcome.ERROR,
            PipelineOutcome.BLOCKED,
            run_id="invoices_pipeline-run-1",
            product="invoices",
        ),
    )

    body = _get(api, mint)

    assert {pipeline["job"]: pipeline["counts"] is not None for pipeline in body["pipelines"]} == {
        JOB: True,
        "invoices_pipeline": True,
        "refunds_pipeline": False,
    }
    assert body["totals"] == _counts(passed=1, warn=1, error=1, blocking=1)


def test_without_a_reader_quality_is_not_provided_and_nothing_is_counted(
    settings: Settings, mint: Mint, dagster: Dagster
) -> None:
    dagster.runs = _runs("SUCCESS")

    body = _get(_app(settings, dagster, {}), mint)

    assert {item["name"]: item["status"] for item in body["integrations"]}["quality"] == "not_provided"
    assert (body["totals"], body["pipelines"]) == (None, [])


def test_an_unreadable_dagster_is_unavailable_and_nothing_is_counted(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("SUCCESS")
    _store(bucket, _result(QualityOutcome.PASSED))
    dagster.fail_status = 503

    body = _get(api, mint)

    assert {item["name"]: item["status"] for item in body["integrations"]}["dagster"] == "unavailable"
    assert (body["totals"], body["pipelines"]) == (None, [])


def test_a_read_past_the_deadline_is_unavailable_and_nothing_is_counted(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, mint: Mint, dagster: Dagster
) -> None:
    class Hung(FakeBucket):
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            time.sleep(1)
            return super().get_object(Bucket=Bucket, Key=Key)

    monkeypatch.setattr(quality_routes, "READ_DEADLINE_SECONDS", 0.2)
    dagster.runs = _runs("SUCCESS")
    api = _app(settings, dagster, {"dev": QualityReader(Hung(), BASE)})
    headers = _as(mint, "data-engineer")
    started = time.monotonic()

    body = api.get(QUALITY, headers=headers).json()

    assert time.monotonic() - started < 1
    dagster_status = next(item for item in body["integrations"] if item["name"] == "dagster")
    assert dagster_status["status"] == "unavailable"
    assert (body["totals"], body["pipelines"]) == (None, [])


def test_the_answer_carries_no_check_table_or_evidence_pointer(
    api: TestClient, mint: Mint, dagster: Dagster, bucket: FakeBucket
) -> None:
    dagster.runs = _runs("FAILURE")
    _store(bucket, _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED))

    text = api.get(QUALITY, headers=_as(mint, "data-engineer")).text

    for secret in ("not_null_customer_secret", "fct_secret_table", "s3://", "evidence"):
        assert secret not in text
