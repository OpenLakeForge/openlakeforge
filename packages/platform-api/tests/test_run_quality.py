"""The run quality route (#1325) and its report download, against a loopback Dagster and fake object stores."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import pytest
from _alerting_fakes import FakeBucket
from _files_fakes import FakeS3
from conftest import Mint
from fastapi.testclient import TestClient
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult, manifest_key
from test_reads import _as
from test_reads import _settings as _api_settings
from test_run_failure import FAILED, RUN_ID, Dagster

from openlakeforge_platform_api import run_failure
from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.quality_health import QualityReader
from openlakeforge_platform_api.settings import Settings

OPS = "ops"
BASES = {stage: f"s3://{OPS}/activations/{stage}/" for stage in ("dev", "prod")}
PATH = f"/api/v1/stages/dev/runs/{RUN_ID}/quality"
WHEN = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
FLOE_REPORT = f"activations/dev/floe/reports/{RUN_ID}/orders/run.json"
DBT_REPORT = f"activations/dev/run-artifacts/dbt/sales/orders/{RUN_ID}/target/run_results.json"


def _result(
    engine: str, check: str, outcome: QualityOutcome, pipeline: PipelineOutcome, **fields: Any
) -> QualityResult:
    return QualityResult(
        engine=engine,
        stage=fields.pop("stage", "dev"),
        domain="sales",
        product="orders",
        check_id=check,
        engine_severity=fields.pop("severity", "reject"),
        outcome=outcome,
        pipeline_outcome=pipeline,
        observed_at=WHEN,
        dagster_run_id=RUN_ID,
        **fields,
    )


def _floe(check: str, outcome: QualityOutcome, pipeline: PipelineOutcome, **metrics: int) -> QualityResult:
    return _result(
        "floe", check, outcome, pipeline, table="orders", artifact_uri=f"s3://{OPS}/{FLOE_REPORT}", metrics=metrics
    )


def _dbt(check: str, outcome: QualityOutcome, failures: int, column: str | None = "order_id") -> QualityResult:
    return _result(
        "dbt",
        check,
        outcome,
        PipelineOutcome.BLOCKED if outcome is QualityOutcome.ERROR else PipelineOutcome.CONTINUED,
        severity="error",
        table="gold_sales.order_revenue",
        column=column,
        artifact_uri=f"s3://{OPS}/{DBT_REPORT}",
        metrics={"failures": failures, "execution_time_seconds": 0.4},
    )


MIXED = (
    _floe("floe_not_null", QualityOutcome.WARN, PipelineOutcome.CONTINUED, rows_total=100, violations_total=3),
    _floe(
        "floe_cast_error",
        QualityOutcome.ERROR,
        PipelineOutcome.QUARANTINED,
        rows_total=100,
        accepted_rows=97,
        rejected_rows=3,
        violations_total=3,
    ),
    _dbt("unique_order_revenue_order_id", QualityOutcome.ERROR, 2),
    _dbt("not_null_order_revenue_amount", QualityOutcome.PASSED, 0, column="amount"),
)


def _store(bucket: FakeBucket, *results: QualityResult, stage: str = "dev", complete: bool = True) -> None:
    manifest = QualityManifest(stage, "sales", "orders", RUN_ID, results, complete=complete)
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
def s3() -> FakeS3:
    store = FakeS3()
    store.put(OPS, FLOE_REPORT, b'{"entity": "orders"}', "text/html")
    store.put(OPS, DBT_REPORT, b'{"results": []}')
    return store


def _app(
    settings: Settings,
    dagster: Dagster,
    bucket: FakeBucket,
    s3: FakeS3 | None,
    permissions: PermissionMatrix | None = None,
) -> TestClient:
    graphql = f"{dagster.url}/graphql"
    configured = _api_settings(
        settings, dagster_graphql_urls={"dev": graphql, "prod": graphql}, quality_manifests=BASES
    )
    readers = {stage: QualityReader(bucket, base) for stage, base in BASES.items()}
    return TestClient(create_app(configured, quality_readers=readers, files_store=s3, permissions=permissions))


@pytest.fixture
def api(settings: Settings, dagster: Dagster, bucket: FakeBucket, s3: FakeS3) -> TestClient:
    return _app(settings, dagster, bucket, s3)


def _get(api: TestClient, mint: Mint, role: str = "data-engineer", path: str = PATH) -> dict[str, Any]:
    response = api.get(path, headers=_as(mint, role))
    assert response.status_code == 200, response.text
    return response.json()


def _quality(body: dict[str, Any]) -> dict[str, str]:
    (source,) = body["sources"]
    assert source["name"] == "quality"
    return source


@pytest.mark.parametrize(
    ("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403), ("viewer", 403)]
)
def test_the_route_admits_data_engineer_and_above(api: TestClient, mint: Mint, role: str, status: int) -> None:
    assert api.get(PATH, headers=_as(mint, role)).status_code == status


def test_a_request_without_a_token_is_denied(api: TestClient) -> None:
    assert api.get(PATH).status_code == 401


def test_a_role_limited_to_another_stage_is_refused(
    settings: Settings, dagster: Dagster, bucket: FakeBucket, s3: FakeS3, mint: Mint
) -> None:
    packaged = tomllib.loads(
        resources.files("openlakeforge_platform_api").joinpath("permissions.toml").read_text(encoding="utf-8")
    )
    packaged["operations"]["get_run_quality"]["role_stages"] = {"data-engineer": ["dev"]}
    matrix = PermissionMatrix.from_mapping(packaged, settings.precedence)
    api = _app(settings, dagster, bucket, s3, permissions=matrix)
    _store(bucket, *MIXED)

    refused = api.get(PATH.replace("/dev/", "/prod/"), headers=_as(mint, "data-engineer"))

    assert refused.status_code == 403
    assert "only on stages: dev" in refused.json()["detail"]
    assert api.get(PATH, headers=_as(mint, "data-engineer")).status_code == 200


def test_an_unknown_run_is_not_found(api: TestClient, mint: Mint) -> None:
    response = api.get("/api/v1/stages/dev/runs/missing/quality", headers=_as(mint, "data-engineer"))

    assert response.status_code == 404


def test_a_run_with_floe_and_dbt_outcomes_lists_every_check_with_its_counts(
    api: TestClient, mint: Mint, bucket: FakeBucket
) -> None:
    _store(bucket, *MIXED)

    body = _get(api, mint)

    assert (body["run_id"], body["job"], body["status"], body["complete"]) == (
        RUN_ID,
        "orders_pipeline",
        "FAILURE",
        True,
    )
    assert body["counts"] == {"passed": 1, "warn": 1, "error": 2, "not_run": 0, "unknown": 0, "blocking": 1}
    floe_report, dbt_report = (
        next(report["id"] for report in body["reports"] if report["engine"] == engine) for engine in ("floe", "dbt")
    )
    checks = [{key: check[key] for key in check if key != "observed_at"} for check in body["checks"]]
    assert checks == [
        {
            "engine": "dbt",
            "check_id": "not_null_order_revenue_amount",
            "table": "gold_sales.order_revenue",
            "column": "amount",
            "engine_severity": "error",
            "outcome": "passed",
            "pipeline_outcome": "continued",
            "rows": None,
            "accepted_rows": None,
            "rejected_rows": None,
            "violations": None,
            "failures": 0,
            "report_id": dbt_report,
        },
        {
            "engine": "dbt",
            "check_id": "unique_order_revenue_order_id",
            "table": "gold_sales.order_revenue",
            "column": "order_id",
            "engine_severity": "error",
            "outcome": "error",
            "pipeline_outcome": "blocked",
            "rows": None,
            "accepted_rows": None,
            "rejected_rows": None,
            "violations": None,
            "failures": 2,
            "report_id": dbt_report,
        },
        {
            "engine": "floe",
            "check_id": "floe_cast_error",
            "table": "orders",
            "column": None,
            "engine_severity": "reject",
            "outcome": "error",
            "pipeline_outcome": "quarantined",
            "rows": 100,
            "accepted_rows": 97,
            "rejected_rows": 3,
            "violations": 3,
            "failures": None,
            "report_id": floe_report,
        },
        {
            "engine": "floe",
            "check_id": "floe_not_null",
            "table": "orders",
            "column": None,
            "engine_severity": "reject",
            "outcome": "warn",
            "pipeline_outcome": "continued",
            "rows": 100,
            "accepted_rows": None,
            "rejected_rows": None,
            "violations": 3,
            "failures": None,
            "report_id": floe_report,
        },
    ]
    assert body["runbook"] is None
    assert _quality(body)["status"] == "available"


def test_each_report_is_listed_once_with_a_link_through_the_api(
    api: TestClient, mint: Mint, bucket: FakeBucket
) -> None:
    _store(bucket, *MIXED)

    reports = _get(api, mint)["reports"]

    base = f"/api/v1/stages/dev/runs/{RUN_ID}/quality/reports/"
    assert [(report["engine"], report["table"]) for report in reports] == [("dbt", None), ("floe", "orders")]
    for report in reports:
        assert report["href"] == base + report["id"]
    assert OPS not in json.dumps(reports)


def test_a_run_without_a_manifest_has_no_checks_never_invented_passes(api: TestClient, mint: Mint) -> None:
    body = _get(api, mint)

    assert (body["checks"], body["counts"], body["complete"], body["reports"]) == (None, None, None, [])
    assert _quality(body) == {
        "name": "quality",
        "status": "not_provided",
        "reason": "the run wrote no quality manifest",
    }


def test_an_unreadable_manifest_is_unavailable(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    bucket.objects["activations/dev/" + manifest_key("sales", "orders", RUN_ID)] = b"{not json"

    body = _get(api, mint)

    assert (body["checks"], body["counts"]) == (None, None)
    assert _quality(body)["status"] == "unavailable"


def test_a_job_that_is_not_a_product_pipeline_has_no_checks(api: TestClient, mint: Mint, dagster: Dagster) -> None:
    dagster.runs = [{**FAILED, "jobName": "gold_maintenance"}]

    body = _get(api, mint)

    assert body["checks"] is None
    assert _quality(body)["status"] == "not_provided"


def test_an_incomplete_manifest_keeps_its_checks_and_says_so(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    _store(bucket, MIXED[0], complete=False)

    body = _get(api, mint)

    assert body["complete"] is False
    assert [check["check_id"] for check in body["checks"]] == ["floe_not_null"]


def test_a_dagster_past_the_deadline_is_a_502(
    api: TestClient, mint: Mint, dagster: Dagster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_failure, "READ_DEADLINE_SECONDS", 0.0)

    response = api.get(PATH, headers=_as(mint, "data-engineer"))

    assert response.status_code == 502
    assert "did not answer" in response.json()["detail"]


def _report_path(body: dict[str, Any], engine: str) -> str:
    report = next(report for report in body["reports"] if report["engine"] == engine)
    return f"/api/v1/stages/dev/runs/{RUN_ID}/quality/reports/{report['id']}"


def test_a_report_streams_through_the_api_as_json(api: TestClient, mint: Mint, bucket: FakeBucket, s3: FakeS3) -> None:
    _store(bucket, *MIXED)
    body = _get(api, mint)

    response = api.get(_report_path(body, "floe"), headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert response.content == b'{"entity": "orders"}'
    # The stored type is not trusted: a report is served as JSON whatever it was written with.
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith('attachment; filename="run.json"')
    assert all(read.closed for read in s3.bodies)


@pytest.mark.parametrize(
    ("role", "stage", "status"),
    [
        ("platform-admin", "dev", 200),
        ("platform-admin", "prod", 200),
        ("data-engineer", "dev", 200),
        ("data-engineer", "prod", 403),
        ("analyst", "dev", 403),
        ("viewer", "dev", 403),
    ],
)
def test_report_downloads_follow_the_log_floor(
    api: TestClient, mint: Mint, bucket: FakeBucket, s3: FakeS3, role: str, stage: str, status: int
) -> None:
    report = f"activations/{stage}/floe/reports/{RUN_ID}/orders/run.json"
    s3.put(OPS, report, b"{}")
    _store(
        bucket,
        _result(
            "floe",
            "floe_unique",
            QualityOutcome.PASSED,
            PipelineOutcome.CONTINUED,
            stage=stage,
            table="orders",
            artifact_uri=f"s3://{OPS}/{report}",
        ),
        stage=stage,
    )
    body = _get(api, mint, "platform-admin", PATH.replace("/dev/", f"/{stage}/"))
    path = _report_path(body, "floe").replace("/dev/", f"/{stage}/")

    assert api.get(path, headers=_as(mint, role)).status_code == status


def test_a_role_that_may_not_download_gets_no_link(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    manifest = QualityManifest(
        "prod",
        "sales",
        "orders",
        RUN_ID,
        (
            _result(
                "floe",
                "floe_unique",
                QualityOutcome.WARN,
                PipelineOutcome.CONTINUED,
                stage="prod",
                table="orders",
                artifact_uri=f"s3://{OPS}/activations/prod/floe/reports/r/run.json",
            ),
        ),
    )
    bucket.objects[manifest.ops_key] = manifest.to_json().encode()

    engineer = _get(api, mint, "data-engineer", PATH.replace("/dev/", "/prod/"))
    admin = _get(api, mint, "platform-admin", PATH.replace("/dev/", "/prod/"))

    assert [report["href"] for report in engineer["reports"]] == [None]
    assert admin["reports"][0]["href"] is not None
    assert engineer["checks"][0]["report_id"] == admin["reports"][0]["id"]


def test_a_report_outside_the_stages_prefix_is_never_served(
    api: TestClient, mint: Mint, bucket: FakeBucket, s3: FakeS3
) -> None:
    foreign = f"activations/prod/floe/reports/{RUN_ID}/orders/run.json"
    s3.put(OPS, foreign, b'{"stage": "prod"}')
    _store(
        bucket,
        _result(
            "floe",
            "floe_unique",
            QualityOutcome.ERROR,
            PipelineOutcome.BLOCKED,
            table="orders",
            artifact_uri=f"s3://{OPS}/{foreign}",
        ),
    )
    body = _get(api, mint, "platform-admin")

    (report,) = body["reports"]
    response = api.get(
        f"/api/v1/stages/dev/runs/{RUN_ID}/quality/reports/{report['id']}", headers=_as(mint, "platform-admin")
    )

    assert report["href"] is None
    assert response.status_code == 404


def test_a_report_id_the_manifest_does_not_name_is_not_found(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    _store(bucket, *MIXED)

    response = api.get(
        f"/api/v1/stages/dev/runs/{RUN_ID}/quality/reports/0000000000000000", headers=_as(mint, "data-engineer")
    )

    assert response.status_code == 404


def test_a_report_of_a_run_without_a_manifest_is_not_found(api: TestClient, mint: Mint) -> None:
    response = api.get(
        f"/api/v1/stages/dev/runs/{RUN_ID}/quality/reports/0000000000000000", headers=_as(mint, "data-engineer")
    )

    assert response.status_code == 404


def test_without_the_files_key_reports_have_no_link_and_do_not_download(
    settings: Settings, dagster: Dagster, bucket: FakeBucket, mint: Mint
) -> None:
    api = _app(settings, dagster, bucket, None)
    _store(bucket, *MIXED)
    body = _get(api, mint)

    response = api.get(_report_path(body, "floe"), headers=_as(mint, "data-engineer"))

    assert {report["href"] for report in body["reports"]} == {None}
    assert response.status_code == 503


def test_a_retained_report_that_is_gone_is_not_found(
    api: TestClient, mint: Mint, bucket: FakeBucket, s3: FakeS3
) -> None:
    _store(bucket, *MIXED)
    body = _get(api, mint)
    del s3.objects[(OPS, FLOE_REPORT)]

    response = api.get(_report_path(body, "floe"), headers=_as(mint, "data-engineer"))

    assert response.status_code == 404
