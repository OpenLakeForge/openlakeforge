from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from openlakeforge_core.quality import (
    SCHEMA_VERSION,
    PipelineOutcome,
    QualityManifest,
    QualityOutcome,
    QualityRecordError,
    QualityResult,
    health_quality_fields,
    manifest_key,
)

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)


def result(**overrides: Any) -> QualityResult:
    fields: dict[str, Any] = {
        "engine": "floe",
        "stage": "dev",
        "domain": "sales",
        "product": "order_revenue",
        "table": "orders",
        "column": "amount",
        "check_id": "floe_not_null",
        "engine_severity": "reject",
        "outcome": QualityOutcome.ERROR,
        "pipeline_outcome": PipelineOutcome.QUARANTINED,
        "observed_at": NOW,
        "dagster_run_id": "run-1",
        "engine_run_id": "floe-9",
        "artifact_uri": "s3://ops/activations/dev/floe/reports/sales/run_9/report.json",
        "metrics": {"rejected_rows": 3, "total_rows": 100},
    }
    fields.update(overrides)
    return QualityResult(**fields)


def manifest(*results: QualityResult, stage: str = "dev", run: str = "run-1") -> QualityManifest:
    return QualityManifest(stage=stage, domain="sales", product="order_revenue", dagster_run_id=run, results=results)


def test_record_round_trips_through_json() -> None:
    original = manifest(result(), result(check_id="floe_unique", outcome=QualityOutcome.PASSED, metrics={}))
    assert QualityManifest.from_json(original.to_json()) == original


def test_a_manifest_is_incomplete_unless_marked_and_the_marker_round_trips() -> None:
    document = json.loads(manifest(result()).to_json())
    del document["complete"]

    assert QualityManifest.from_json(json.dumps(document)).complete is False
    finished = QualityManifest("dev", "sales", "order_revenue", "run-1", (result(),), complete=True)
    assert QualityManifest.from_json(finished.to_json()).complete is True
    with pytest.raises(QualityRecordError):
        QualityManifest.from_json(_manifest_document(complete="yes"))


def test_serialization_is_deterministic_whatever_the_input_order() -> None:
    a, b = result(check_id="floe_unique"), result(check_id="floe_not_null")
    assert manifest(a, b).to_json() == manifest(b, a).to_json()
    assert manifest(a, b).to_json() == manifest(a, b).to_json()


def test_original_severity_and_pipeline_outcome_stay_separate() -> None:
    loaded = QualityResult.from_dict(result().to_dict())
    assert loaded.engine_severity == "reject"
    assert loaded.outcome is QualityOutcome.ERROR
    assert loaded.pipeline_outcome is PipelineOutcome.QUARANTINED


def test_version_is_written_and_checked() -> None:
    document = json.loads(manifest(result()).to_json())
    assert document["version"] == SCHEMA_VERSION == document["results"][0]["version"]
    document["version"] = SCHEMA_VERSION + 1
    with pytest.raises(QualityRecordError, match="unsupported quality record version"):
        QualityManifest.from_json(json.dumps(document))
    record = result().to_dict()
    del record["version"]
    with pytest.raises(QualityRecordError, match="version"):
        QualityResult.from_dict(record)


def test_stages_and_runs_have_distinct_identities_and_paths() -> None:
    dev_1, dev_2, prod_1 = result(), result(dagster_run_id="run-2"), result(stage="prod")
    assert len({dev_1.identity, dev_2.identity, prod_1.identity}) == 3
    paths = {manifest(dev_1).ops_key, manifest(dev_2, run="run-2").ops_key, manifest(prod_1, stage="prod").ops_key}
    assert paths == {
        "activations/dev/run-artifacts/quality/sales/order_revenue/run-1/manifest.json",
        "activations/dev/run-artifacts/quality/sales/order_revenue/run-2/manifest.json",
        "activations/prod/run-artifacts/quality/sales/order_revenue/run-1/manifest.json",
    }


def test_identity_ignores_what_a_replay_may_change() -> None:
    first = result()
    replay = result(outcome=QualityOutcome.PASSED, observed_at=NOW + timedelta(hours=1), metrics={"x": 1})
    assert first.identity == replay.identity


def test_identity_cannot_collide_across_field_boundaries() -> None:
    assert result(table="a|b", column=None).identity != result(table="a", column="b").identity


def test_manifest_rejects_results_from_another_stage_or_run() -> None:
    with pytest.raises(QualityRecordError, match="not this manifest's stage"):
        manifest(result(stage="prod"))
    with pytest.raises(QualityRecordError, match="not this manifest's stage"):
        manifest(result(dagster_run_id="other"))


def test_manifest_rejects_duplicate_check_executions() -> None:
    with pytest.raises(QualityRecordError, match="duplicate"):
        manifest(result(), result(outcome=QualityOutcome.PASSED))


@pytest.mark.parametrize("field", ["rows", "sample_values", "payload", "secret", "password", "token"])
def test_record_with_row_data_or_secret_fields_is_rejected(field: str) -> None:
    record = result().to_dict()
    record[field] = [{"email": "a@example.com"}]
    with pytest.raises(QualityRecordError, match="forbidden fields"):
        QualityResult.from_dict(record)


def test_unknown_fields_are_rejected() -> None:
    record = result().to_dict()
    record["note"] = "hello"
    with pytest.raises(QualityRecordError, match="unknown fields"):
        QualityResult.from_dict(record)


@pytest.mark.parametrize(
    "metrics",
    [{"sample_values": 1}, {"rejected_rows": "alice@example.com"}, {"ok": True}, {"Rows": 3}],
)
def test_metrics_may_only_be_aggregate_numbers(metrics: dict[str, Any]) -> None:
    with pytest.raises(QualityRecordError):
        result(metrics=metrics)


@pytest.mark.parametrize(
    "uri", ["https://ops.example/report.json?X-Amz-Signature=abc", "s3://AKIA:secret@ops/report.json"]
)
def test_artifact_uri_cannot_carry_credentials(uri: str) -> None:
    with pytest.raises(QualityRecordError, match="artifact_uri"):
        result(artifact_uri=uri)


def test_tampered_identity_is_rejected() -> None:
    record = result().to_dict()
    record["table"] = "customers"
    with pytest.raises(QualityRecordError, match="identity"):
        QualityResult.from_dict(record)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(QualityRecordError, match="timezone-aware"):
        result(observed_at=datetime(2026, 11, 16, 9, 30))


def test_timestamps_serialize_as_utc() -> None:
    local = result(observed_at=datetime(2026, 11, 16, 11, 30, tzinfo=timezone(timedelta(hours=2))))
    assert local.to_dict()["observed_at"] == "2026-11-16T09:30:00Z"
    assert QualityResult.from_dict(local.to_dict()).observed_at == NOW


@pytest.mark.parametrize("text", ["not json", "[]", "{}"])
def test_malformed_manifests_raise_a_record_error(text: str) -> None:
    with pytest.raises(QualityRecordError):
        QualityManifest.from_json(text)


def _manifest_document(**changes: object) -> str:
    document = json.loads(manifest(result()).to_json())
    document.update(changes)
    return json.dumps(document)


def _result_document(**changes: object) -> dict[str, object]:
    record = result().to_dict()
    record.update(changes)
    return record


@pytest.mark.parametrize(
    "results",
    [
        [None],
        [[]],
        ["text"],
        [1],
        {},
        "text",
        None,
        1,
        [_result_document(metrics="x")],
        [_result_document(metrics=[1])],
        [_result_document(metrics={"rows": "many"})],
        [_result_document(outcome=[])],
        [_result_document(outcome=1)],
        [_result_document(observed_at=1)],
        [_result_document(engine=1)],
        [_result_document(check_id=None)],
        [_result_document(domain=1)],
    ],
    ids=repr,
)
def test_structurally_malformed_results_raise_a_record_error(results: object) -> None:
    with pytest.raises(QualityRecordError):
        QualityManifest.from_json(_manifest_document(results=results))


def test_a_manifest_without_results_raises_a_record_error() -> None:
    document = json.loads(_manifest_document())
    del document["results"]

    with pytest.raises(QualityRecordError, match="results"):
        QualityManifest.from_json(json.dumps(document))


def test_manifest_key_refuses_path_traversal() -> None:
    with pytest.raises(QualityRecordError, match="safe path segment"):
        manifest_key("sales", "../other", "run-1")


def test_manifest_key_refuses_a_trailing_newline() -> None:
    with pytest.raises(QualityRecordError, match="safe path segment"):
        manifest_key("sales\n", "orders", "run-1")


def test_reject_that_quarantined_rows_is_a_non_blocking_health_error() -> None:
    fields = health_quality_fields(result())
    assert fields is not None
    assert (fields.outcome, fields.blocking, fields.run) == (QualityOutcome.ERROR, False, "run-1")
    assert fields.evidence_uri == result().artifact_uri


def test_blocked_error_is_a_blocking_health_error() -> None:
    fields = health_quality_fields(result(engine_severity="abort", pipeline_outcome=PipelineOutcome.BLOCKED))
    assert fields is not None and fields.blocking


@pytest.mark.parametrize("outcome", [QualityOutcome.NOT_RUN, QualityOutcome.UNKNOWN])
def test_unexecuted_checks_have_no_health_observation(outcome: QualityOutcome) -> None:
    assert health_quality_fields(result(outcome=outcome)) is None


@pytest.mark.parametrize("field", ["engine_severity", "check_id"])
def test_required_string_fields_must_be_strings(field: str) -> None:
    with pytest.raises(QualityRecordError, match=field):
        result(**{field: {"rows": [{"email": "a@example.com"}], "password": "hunter2"}})


@pytest.mark.parametrize("field", ["domain", "artifact_uri"])
def test_optional_string_fields_must_be_strings_or_null(field: str) -> None:
    with pytest.raises(QualityRecordError, match=field):
        result(**{field: 7})


def test_payload_in_engine_severity_is_rejected_on_read() -> None:
    record = result().to_dict()
    record["engine_severity"] = {"rows": [{"email": "a@example.com"}], "password": "hunter2"}
    with pytest.raises(QualityRecordError, match="engine_severity"):
        QualityResult.from_dict(record)


@pytest.mark.parametrize("field", ["domain", "product"])
def test_manifest_rejects_results_owned_by_another_domain_or_product(field: str) -> None:
    with pytest.raises(QualityRecordError, match=f"has {field} 'other'"):
        manifest(result(**{field: "other"}))


def test_manifest_accepts_results_without_domain_or_product() -> None:
    assert len(manifest(result(domain=None, product=None)).results) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_metrics_are_rejected(value: float) -> None:
    with pytest.raises(QualityRecordError, match="finite"):
        result(metrics={"duration_seconds": value})
