"""Silver's Floe outcomes against what Gold really does (#345).

The rule is documented in docs/architecture/floe-validation.md: a `warn` or `reject` check result leaves the
Silver step successful, so Gold runs, and only a Floe run that ends `failed` or `aborted` fails the step and
skips Gold. The checks are built by dagster-floe and judged by `floe_quality`, so a dagster-floe upgrade that made
a check blocking, or a change to how a result is classified, fails here rather than in a cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("dagster")
floe_checks = pytest.importorskip("floe_dagster.asset_checks")

from dagster import AssetKey, AssetOut, Definitions, Failure, Output, asset, define_asset_job, multi_asset  # noqa: E402
from floe_dagster.events import FloeRunFinished  # noqa: E402
from openlakeforge_core.quality import PipelineOutcome, QualityOutcome  # noqa: E402
from openlakeforge_runtime.floe_quality import (  # noqa: E402
    FloeCheckEvidence,
    FloeEntityEvidence,
    floe_quality_manifest,
)

SILVER = AssetKey(["sales", "orders"])


def _report(rule_severity: str, violations: int = 3) -> dict[str, Any]:
    rules = [{"rule": "not_null", "severity": rule_severity, "violations": violations}]
    return {
        "policy": {"severity": rule_severity},
        "files": [{"status": "rejected" if rule_severity == "reject" else "warning", "validation": {"rules": rules}}],
    }


def _run(status: str, report: dict[str, Any]) -> tuple[bool, list[Any], list[str]]:
    """Run Silver then Gold in one job. Returns (job succeeded, Floe's check results, the steps that ran)."""
    ran: list[str] = []
    finished = FloeRunFinished(run_id="floe-1", status=status, exit_code=0, summary_uri=None)
    results = floe_checks.build_asset_check_results(
        asset_key=list(SILVER.path), entity_name="orders", finished=finished, entity_report=report
    )

    @multi_asset(outs={"orders": AssetOut(key=SILVER)}, check_specs=floe_checks.build_asset_check_specs(SILVER.path))
    def silver():
        ran.append("silver")
        yield from results
        # dagster-floe fails the step when `floe run` ends failed or aborted, after emitting the checks.
        if status in ("failed", "aborted"):
            raise Failure(f"floe run {status}")
        yield Output(None, output_name="orders")

    @asset(deps=[SILVER])
    def gold() -> None:
        ran.append("gold")

    job = define_asset_job("product")
    outcome = (
        Definitions(assets=[silver, gold], jobs=[job]).get_job_def("product").execute_in_process(raise_on_error=False)
    )
    return outcome.success, results, ran


def _records(results: list[Any]):
    evidence = FloeEntityEvidence(
        "orders",
        tuple(FloeCheckEvidence.from_asset_check(r) for r in results),
        {"policy": {"severity": "reject"}, "files": [{"name": "orders.csv"}]},
    )
    manifest = floe_quality_manifest(
        stage="dev",
        domain="sales",
        product="order_revenue",
        dagster_run_id="r1",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        entities=[evidence],
    )
    return {r.check_id: r for r in manifest.results}


def test_floes_five_checks_never_block_downstream_assets() -> None:
    assert [spec.name for spec in floe_checks.build_asset_check_specs(SILVER.path)] == [
        "floe_cast_error",
        "floe_not_null",
        "floe_unique",
        "floe_schema_mismatch",
        "floe_file_status",
    ]
    assert not any(spec.blocking for spec in floe_checks.build_asset_check_specs(SILVER.path))


def test_rejected_rows_quarantine_and_gold_still_runs() -> None:
    ok, results, ran = _run("success", _report("reject"))

    assert ok and ran == ["silver", "gold"]
    record = _records(results)["floe_not_null"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.QUARANTINED)


def test_a_warning_degrades_quality_and_gold_still_runs() -> None:
    ok, results, ran = _run("success", _report("warn"))

    assert ok and ran == ["silver", "gold"]
    record = _records(results)["floe_not_null"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.WARN, PipelineOutcome.CONTINUED)


@pytest.mark.parametrize("status", ["aborted", "failed"])
def test_an_aborted_floe_run_fails_silver_and_gold_never_runs(status: str) -> None:
    ok, results, ran = _run(status, _report("abort"))

    assert not ok and ran == ["silver"]
    record = _records(results)["floe_not_null"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.BLOCKED)


def test_a_clean_run_passes_every_check_and_gold_runs() -> None:
    ok, results, ran = _run("success", {"policy": {"severity": "reject"}, "files": [{"status": "success"}]})

    assert ok and ran == ["silver", "gold"]
    assert {r.outcome for r in _records(results).values()} == {QualityOutcome.PASSED}
