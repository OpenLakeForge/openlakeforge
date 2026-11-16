"""Floe check evidence to `quality_result` records.

The fixtures mirror what floe-python 0.10.1 writes to `run.json` and what dagster-floe 0.6.0 puts in each of
its five asset checks, down to the metadata keys, so a Floe upgrade that renames one fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest
from openlakeforge_core import (
    PipelineOutcome,
    QualityManifest,
    QualityOutcome,
    QualityRecordError,
    QualityResult,
    health_quality_fields,
)
from openlakeforge_runtime.floe_quality import (
    FLOE_CHECK_IDS,
    FloeCheckEvidence,
    FloeEntityEvidence,
    floe_quality_manifest,
    read_entity_report,
)

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
REPORT_URI = "s3://ops/activations/dev/floe/reports/sales/run_f1/orders/run.json"


def report(policy: str = "reject", *, files: int = 1) -> dict[str, Any]:
    return {
        "spec_version": "0.3",
        "entity": {"name": "orders"},
        "policy": {"severity": policy},
        "results": {"files_total": files, "rows_total": 100},
        "files": [{"input_file": f"s3://bronze/orders/{i}.csv", "status": "success"} for i in range(files)],
    }


def check(name: str, *, passed: bool = True, severity: str = "ERROR", run_status: str = "success", **meta: Any):
    metadata: dict[str, Any] = {
        "entity": "orders",
        "run_id": "f1",
        "floe_run_status": run_status,
        "entity_report_uri": REPORT_URI,
        "files": 1,
        "rows": 100,
        "accepted": 100,
        "rejected": 0,
        "warnings": 0,
        "errors": 0,
        "entity_status": run_status,
    }
    metadata.update(meta)
    return FloeCheckEvidence(name, passed, severity, metadata)


def all_passing(run_status: str = "success") -> dict[str, FloeCheckEvidence]:
    return {name: check(name, run_status=run_status) for name in FLOE_CHECK_IDS}


def manifest_for(
    *entities: FloeEntityEvidence, stage: str = "dev", run: str = "run-1", domain: str = "sales"
) -> QualityManifest:
    return floe_quality_manifest(
        stage=stage,
        domain=domain,
        product="order_revenue",
        dagster_run_id=run,
        observed_at=NOW,
        entities=entities,
    )


def entity(checks: dict[str, FloeCheckEvidence], policy: str = "reject", name: str = "orders"):
    return FloeEntityEvidence(name, tuple(checks.values()), report(policy))


def by_check(manifest: QualityManifest, table: str = "orders") -> dict[str, QualityResult]:
    return {r.check_id: r for r in manifest.results if r.table == table}


def test_a_clean_run_has_one_passed_result_for_each_of_the_five_checks() -> None:
    results = by_check(manifest_for(entity(all_passing())))
    assert set(results) == set(FLOE_CHECK_IDS)
    assert {r.outcome for r in results.values()} == {QualityOutcome.PASSED}
    assert {r.pipeline_outcome for r in results.values()} == {PipelineOutcome.CONTINUED}


def test_warn_policy_is_a_warning_and_the_pipeline_continues() -> None:
    checks = all_passing("success_with_warnings")
    checks["floe_not_null"] = check(
        "floe_not_null",
        passed=False,
        severity="WARN",
        run_status="success_with_warnings",
        rule="not_null",
        violations_total=2,
        reject_violations=0,
        warn_violations=2,
        files_impacted=1,
        warnings=2,
    )
    result = by_check(manifest_for(entity(checks, "warn")))["floe_not_null"]
    assert (result.outcome, result.pipeline_outcome) == (QualityOutcome.WARN, PipelineOutcome.CONTINUED)
    assert result.engine_severity == "warn"
    assert result.metrics["warn_violations"] == 2
    assert result.artifact_uri == REPORT_URI
    assert result.engine_run_id == "f1"


def test_reject_policy_quarantines_rows_and_is_not_a_blocking_failure() -> None:
    checks = all_passing("rejected")
    checks["floe_cast_error"] = check(
        "floe_cast_error",
        passed=False,
        run_status="rejected",
        violations_total=3,
        reject_violations=3,
        warn_violations=0,
        files_impacted=1,
        rejected=3,
        accepted=97,
        errors=3,
    )
    checks["floe_file_status"] = check(
        "floe_file_status",
        passed=False,
        run_status="rejected",
        file_status_counts={"rejected": 1},
        failed_files=1,
        warn_files=0,
        rejected=3,
        accepted=97,
    )
    results = by_check(manifest_for(entity(checks, "reject")))
    for name in ("floe_cast_error", "floe_file_status"):
        assert (results[name].outcome, results[name].pipeline_outcome) == (
            QualityOutcome.ERROR,
            PipelineOutcome.QUARANTINED,
        )
        assert results[name].engine_severity == "reject"
        assert results[name].metrics["rejected_rows"] == 3
        assert results[name].metrics["rows_total"] == 100
    assert results["floe_file_status"].metrics["files_rejected"] == 1
    assert results["floe_not_null"].outcome is QualityOutcome.PASSED
    fields = health_quality_fields(results["floe_cast_error"])
    assert fields is not None and not fields.blocking


def test_abort_policy_blocks_the_pipeline() -> None:
    checks = {
        name: check(name, passed=False, run_status="aborted", rejected=100, accepted=0, errors=4)
        for name in FLOE_CHECK_IDS
    }
    results = by_check(manifest_for(entity(checks, "abort")))
    assert {(r.outcome, r.pipeline_outcome) for r in results.values()} == {
        (QualityOutcome.ERROR, PipelineOutcome.BLOCKED)
    }
    assert results["floe_unique"].engine_severity == "abort"
    fields = health_quality_fields(results["floe_unique"])
    assert fields is not None and fields.blocking


def test_a_schema_mismatch_that_rejected_a_file_is_quarantined() -> None:
    checks = all_passing("rejected")
    checks["floe_schema_mismatch"] = check(
        "floe_schema_mismatch",
        passed=False,
        run_status="rejected",
        files_impacted=1,
        rejected_files=1,
        mismatch_actions={"rejected_file": 1},
        sample_messages=["column amount holds 'hunter2@example.com'"],
    )
    result = by_check(manifest_for(entity(checks, "reject")))["floe_schema_mismatch"]
    assert (result.outcome, result.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.QUARANTINED)
    assert result.metrics["rejected_files"] == 1


def test_checks_missing_from_a_partial_run_are_not_run_never_passed() -> None:
    checks = all_passing()
    for name in ("floe_unique", "floe_schema_mismatch"):
        del checks[name]
    results = by_check(manifest_for(entity(checks)))
    assert {results[n].outcome for n in ("floe_unique", "floe_schema_mismatch")} == {QualityOutcome.NOT_RUN}
    assert results["floe_not_null"].outcome is QualityOutcome.PASSED
    assert health_quality_fields(results["floe_unique"]) is None


def test_an_entity_with_no_check_results_is_unknown() -> None:
    results = by_check(manifest_for(FloeEntityEvidence("orders")))
    assert set(results) == set(FLOE_CHECK_IDS)
    assert {r.outcome for r in results.values()} == {QualityOutcome.UNKNOWN}
    assert {r.engine_severity for r in results.values()} == {"unknown"}


def test_a_summary_only_fallback_cannot_pass() -> None:
    # dagster-floe reports every check as passed when the entity report was unavailable.
    checks = {name: check(name, fallback="summary_only") for name in FLOE_CHECK_IDS}
    results = by_check(manifest_for(entity(checks)))
    assert {r.outcome for r in results.values()} == {QualityOutcome.UNKNOWN}
    assert {r.pipeline_outcome for r in results.values()} == {PipelineOutcome.UNKNOWN}


@pytest.mark.parametrize("missing", [None, {"policy": {"severity": "reject"}, "files": []}])
def test_an_unreadable_or_empty_report_cannot_pass(missing: dict[str, Any] | None) -> None:
    evidence = FloeEntityEvidence("orders", tuple(all_passing().values()), missing)
    assert {r.outcome for r in manifest_for(evidence).results} == {QualityOutcome.UNKNOWN}


def test_a_failure_survives_a_missing_report() -> None:
    checks = all_passing("aborted")
    checks["floe_file_status"] = check(
        "floe_file_status", passed=False, run_status="aborted", fallback="summary_only"
    )
    result = by_check(manifest_for(FloeEntityEvidence("orders", tuple(checks.values()), None)))["floe_file_status"]
    assert (result.outcome, result.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.BLOCKED)
    assert result.engine_severity == "unknown"


def test_an_error_with_no_sign_of_rejected_rows_does_not_claim_a_quarantine() -> None:
    checks = all_passing()
    checks["floe_not_null"] = check("floe_not_null", passed=False, reject_violations=0)
    evidence = FloeEntityEvidence("orders", tuple(checks.values()), report("warn"))
    result = by_check(manifest_for(evidence))["floe_not_null"]
    assert (result.outcome, result.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.UNKNOWN)


def test_two_entities_of_one_run_do_not_share_results() -> None:
    clean = entity(all_passing(), name="orders")
    failing = all_passing("rejected")
    failing["floe_not_null"] = check("floe_not_null", passed=False, reject_violations=2, run_status="rejected")
    manifest = manifest_for(clean, entity(failing, name="accounts"))
    assert by_check(manifest, "orders")["floe_not_null"].outcome is QualityOutcome.PASSED
    assert by_check(manifest, "accounts")["floe_not_null"].outcome is QualityOutcome.ERROR
    assert len({r.identity for r in manifest.results}) == 2 * len(FLOE_CHECK_IDS)


def test_the_same_evidence_in_two_stages_and_two_runs_never_associates() -> None:
    evidence = entity(all_passing())
    dev = manifest_for(evidence, stage="dev", run="run-1")
    prod = manifest_for(evidence, stage="prod", run="run-1")
    next_run = manifest_for(evidence, stage="dev", run="run-2")
    assert not {r.identity for r in dev.results} & {r.identity for r in prod.results}
    assert not {r.identity for r in dev.results} & {r.identity for r in next_run.results}
    assert len({dev.ops_key, prod.ops_key, next_run.ops_key}) == 3
    assert {r.stage for r in prod.results} == {"prod"}
    assert {r.dagster_run_id for r in next_run.results} == {"run-2"}
    with pytest.raises(QualityRecordError, match="stage"):
        QualityManifest("dev", "sales", "order_revenue", "run-1", prod.results)
    with pytest.raises(QualityRecordError, match="run"):
        QualityManifest("dev", "sales", "order_revenue", "run-1", next_run.results)


def test_no_row_values_or_schema_detail_reach_the_manifest() -> None:
    checks = all_passing("rejected")
    checks["floe_schema_mismatch"] = check(
        "floe_schema_mismatch",
        passed=False,
        run_status="rejected",
        rejected_files=1,
        sample_messages=["amount='4111-1111'"],
        top_columns=[{"column": "card_number", "violations": 3}],
        missing_columns=["card_number"],
        extra_columns=["ssn"],
        rows=100,
    )
    text = manifest_for(entity(checks)).to_json()
    for leaked in ("4111", "card_number", "ssn", "sample_messages"):
        assert leaked not in text


def test_a_presigned_report_uri_is_left_out_rather_than_stored() -> None:
    checks = {
        name: check(name, entity_report_uri=REPORT_URI + "?X-Amz-Signature=abc") for name in FLOE_CHECK_IDS
    }
    assert {r.artifact_uri for r in manifest_for(entity(checks)).results} == {None}


def test_manifest_round_trips_through_json() -> None:
    manifest = manifest_for(entity(all_passing()))
    assert QualityManifest.from_json(manifest.to_json()) == manifest


class _Severity(Enum):
    WARN = "WARN"
    ERROR = "ERROR"


def test_dagster_check_results_are_read_with_their_wrapped_metadata() -> None:
    result = SimpleNamespace(
        check_name="floe_not_null",
        passed=False,
        severity=_Severity.WARN,
        metadata={"warn_violations": SimpleNamespace(value=2), "run_id": SimpleNamespace(value="f1")},
    )
    evidence = FloeCheckEvidence.from_asset_check(result)
    assert evidence.severity == "WARN"
    checks = (*all_passing().values(), evidence)
    deduped = {c.check_name: c for c in checks}
    manifest = manifest_for(FloeEntityEvidence("orders", tuple(deduped.values()), report("warn")))
    assert by_check(manifest)["floe_not_null"].metrics == {"warn_violations": 2}
    assert by_check(manifest)["floe_not_null"].outcome is QualityOutcome.WARN


def test_read_entity_report_returns_none_when_the_report_cannot_be_read() -> None:
    def broken(uri: str) -> dict[str, Any]:
        raise OSError(uri)

    assert read_entity_report(REPORT_URI, read=broken) is None
    assert read_entity_report(None, read=broken) is None
    assert read_entity_report(REPORT_URI, read=lambda _uri: report()) == report()
