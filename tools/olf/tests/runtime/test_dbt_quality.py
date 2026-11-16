"""dbt test results to `quality_result` records.

The fixtures follow the artifacts dbt-core 1.11.12 writes (run-results schema v6, manifest schema v12): the keys
a test node and a result carry, and the `artifacts_incomplete.json` marker `product_dagster` uploads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult
from openlakeforge_runtime.dbt_quality import (
    DbtRunEvidence,
    dbt_quality_manifest,
    read_dbt_evidence,
)

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
PREFIX = "s3://ops/run-artifacts/dbt/sales/order_revenue/run-1"
ORDERS = "model.shop.orders"
CUSTOMERS = "model.shop.customers"


def model(name: str) -> dict[str, Any]:
    return {
        "resource_type": "model",
        "package_name": "shop",
        "unique_id": f"model.shop.{name}",
        "name": name,
        "alias": name,
        "schema": "gold_order_revenue",
        "database": "lakehouse",
        "relation_name": f'"lakehouse"."gold_order_revenue"."{name}"',
    }


def dbt_test(
    name: str,
    attached: str | None = ORDERS,
    *,
    column: str | None = "id",
    severity: str = "ERROR",
    depends: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    parents = depends if depends is not None else ((attached,) if attached else ())
    return {
        "resource_type": "test",
        "package_name": "shop",
        "unique_id": f"test.shop.{name}.abc123",
        "name": name,
        "fqn": ["shop", "order_revenue", name],
        "attached_node": attached,
        "column_name": column,
        "test_metadata": {"name": "not_null", "kwargs": {"column_name": "id"}, "namespace": None},
        "config": {"severity": severity, "warn_if": "!= 0", "error_if": "!= 0", "enabled": True},
        "depends_on": {"macros": ["macro.dbt.test_not_null"], "nodes": list(parents)},
        "relation_name": None,
    }


def manifest(*tests: dict[str, Any]) -> dict[str, Any]:
    nodes = {n["unique_id"]: n for n in (model("orders"), model("customers"), *tests)}
    return {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json", "project_name": "shop"},
        "nodes": nodes,
        "sources": {},
    }


def result(node: dict[str, Any], status: str, failures: int | None = 0, seconds: float = 0.42) -> dict[str, Any]:
    return {
        "status": status,
        "timing": [{"name": "execute", "started_at": "2026-11-16T09:29:00Z", "completed_at": "2026-11-16T09:29:01Z"}],
        "thread_id": "Thread-1 (worker)",
        "execution_time": seconds,
        "adapter_response": {"_message": "OK"},
        "message": "Got 3 results, configured to fail if != 0: customer jane@example.com has no order"
        if status == "fail"
        else None,
        "failures": failures,
        "unique_id": node["unique_id"],
        "compiled": True,
        "compiled_code": "select 1",
        "relation_name": None,
    }


def run_results(*results: dict[str, Any], invocation: str = "inv-1") -> dict[str, Any]:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json",
            "invocation_id": invocation,
        },
        "results": list(results),
        "elapsed_time": 1.5,
        "args": {"which": "build"},
    }


def build(
    evidence: DbtRunEvidence, *, stage: str = "dev", run: str = "run-1", domain: str = "sales"
) -> QualityManifest:
    return dbt_quality_manifest(
        stage=stage,
        domain=domain,
        product="order_revenue",
        dagster_run_id=run,
        observed_at=NOW,
        evidence=evidence,
    )


def by_check(result_set: QualityManifest) -> dict[str, QualityResult]:
    return {r.check_id: r for r in result_set.results}


NOT_NULL = dbt_test("not_null_orders_id")
UNIQUE = dbt_test("unique_orders_id")


def test_each_status_maps_to_outcome_and_pipeline_outcome() -> None:
    warn = dbt_test("warn_orders_total", severity="warn", column="total")
    fail = dbt_test("fail_orders_status", column="status")
    error = dbt_test("error_orders_region", column="region")
    skipped = dbt_test("skipped_orders_note", column="note")
    tests = [NOT_NULL, warn, fail, error, skipped]
    statuses = ["pass", "warn", "fail", "error", "skipped"]
    evidence = DbtRunEvidence(
        manifest(*tests),
        run_results(*(result(t, s, 3 if s == "fail" else 0) for t, s in zip(tests, statuses, strict=True))),
    )

    records = by_check(build(evidence))

    assert {k: (v.outcome, v.pipeline_outcome) for k, v in records.items()} == {
        "not_null_orders_id": (QualityOutcome.PASSED, PipelineOutcome.CONTINUED),
        "warn_orders_total": (QualityOutcome.WARN, PipelineOutcome.CONTINUED),
        "fail_orders_status": (QualityOutcome.ERROR, PipelineOutcome.BLOCKED),
        "error_orders_region": (QualityOutcome.ERROR, PipelineOutcome.UNKNOWN),
        "skipped_orders_note": (QualityOutcome.NOT_RUN, PipelineOutcome.UNKNOWN),
    }
    assert records["warn_orders_total"].engine_severity == "warn"
    assert records["not_null_orders_id"].engine_severity == "ERROR"


def test_a_record_names_the_table_and_column_from_the_manifest_and_carries_only_numbers() -> None:
    fail = dbt_test("fail_orders_status", column="status")
    record = by_check(build(DbtRunEvidence(manifest(fail), run_results(result(fail, "fail", 3, 2.5)))))[
        "fail_orders_status"
    ]

    assert (record.engine, record.table, record.column) == ("dbt", "gold_order_revenue.orders", "status")
    assert (record.stage, record.domain, record.product, record.dagster_run_id) == (
        "dev",
        "sales",
        "order_revenue",
        "run-1",
    )
    assert record.engine_run_id == "inv-1"
    assert dict(record.metrics) == {"failures": 3, "execution_time_seconds": 2.5}
    assert (
        "jane@example.com" not in build(DbtRunEvidence(manifest(fail), run_results(result(fail, "fail", 3)))).to_json()
    )


def test_a_failed_test_of_severity_warn_is_not_claimed_to_block() -> None:
    odd = dbt_test("odd", severity="warn")

    record = by_check(build(DbtRunEvidence(manifest(odd), run_results(result(odd, "fail")))))["odd"]

    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.UNKNOWN)


def test_relationship_test_belongs_to_the_attached_model_only() -> None:
    rel = dbt_test("relationships_orders_customer_id", ORDERS, column="customer_id", depends=(CUSTOMERS, ORDERS))

    record = by_check(build(DbtRunEvidence(manifest(rel), run_results(result(rel, "pass")))))[
        "relationships_orders_customer_id"
    ]

    assert (record.table, record.column) == ("gold_order_revenue.orders", "customer_id")


def test_a_singular_test_over_several_models_has_no_table_to_claim() -> None:
    singular = dbt_test("orders_match_customers", None, column=None, depends=(ORDERS, CUSTOMERS))
    solo = dbt_test("orders_not_negative", None, column=None, depends=(ORDERS,))
    evidence = DbtRunEvidence(manifest(singular, solo), run_results(result(singular, "pass"), result(solo, "pass")))

    records = by_check(build(evidence))

    assert records["orders_match_customers"].table is None
    assert records["orders_not_negative"].table == "gold_order_revenue.orders"


def test_selected_tests_without_a_result_are_not_run_and_the_rest_are_left_out() -> None:
    other = dbt_test("not_null_customers_id", CUSTOMERS)
    evidence = DbtRunEvidence(
        manifest(NOT_NULL, UNIQUE, other),
        run_results(result(NOT_NULL, "pass")),
        selected_tests=frozenset({NOT_NULL["unique_id"], UNIQUE["unique_id"]}),
    )

    records = by_check(build(evidence))

    assert set(records) == {"not_null_orders_id", "unique_orders_id"}
    assert records["unique_orders_id"].outcome is QualityOutcome.NOT_RUN


def test_without_a_selection_a_test_with_no_result_is_not_invented() -> None:
    evidence = DbtRunEvidence(manifest(NOT_NULL, UNIQUE), run_results(result(NOT_NULL, "pass")))

    assert set(by_check(build(evidence))) == {"not_null_orders_id"}


def test_a_failed_build_leaves_selected_tests_unknown_and_blocks_on_a_test_error() -> None:
    marker = {"status": "artifacts_incomplete", "dbt_invocations": 2, "generations_without_results": [2]}
    errored = dbt_test("error_orders_region", column="region")
    evidence = DbtRunEvidence(
        manifest(NOT_NULL, UNIQUE, errored),
        run_results(result(NOT_NULL, "pass"), result(errored, "error", None)),
        incomplete_marker=marker,
        selected_tests=frozenset(t["unique_id"] for t in (NOT_NULL, UNIQUE, errored)),
    )

    records = by_check(build(evidence))

    assert records["not_null_orders_id"].outcome is QualityOutcome.PASSED
    assert (records["unique_orders_id"].outcome, records["unique_orders_id"].pipeline_outcome) == (
        QualityOutcome.UNKNOWN,
        PipelineOutcome.UNKNOWN,
    )
    assert (records["error_orders_region"].outcome, records["error_orders_region"].pipeline_outcome) == (
        QualityOutcome.ERROR,
        PipelineOutcome.BLOCKED,
    )
    assert "failures" not in records["error_orders_region"].metrics


def test_a_build_that_wrote_no_results_is_unknown_not_not_run() -> None:
    evidence = DbtRunEvidence(
        manifest(NOT_NULL),
        None,
        incomplete_marker={"status": "artifacts_incomplete", "dbt_invocations": 0, "generations_without_results": []},
        selected_tests=frozenset({NOT_NULL["unique_id"]}),
    )

    assert by_check(build(evidence))["not_null_orders_id"].outcome is QualityOutcome.UNKNOWN


def test_two_stages_and_two_runs_never_cross_associate() -> None:
    dev = DbtRunEvidence(manifest(NOT_NULL), run_results(result(NOT_NULL, "pass"), invocation="inv-dev"))
    prod = DbtRunEvidence(manifest(NOT_NULL), run_results(result(NOT_NULL, "fail", 2), invocation="inv-prod"))

    dev_manifest = build(dev, stage="dev", run="run-1")
    prod_manifest = build(prod, stage="prod", run="run-2")
    prod_again = build(prod, stage="prod", run="run-3")

    assert [r.outcome for r in dev_manifest.results] == [QualityOutcome.PASSED]
    assert [r.outcome for r in prod_manifest.results] == [QualityOutcome.ERROR]
    identities = {m.results[0].identity for m in (dev_manifest, prod_manifest, prod_again)}
    assert len(identities) == 3
    assert {m.stage for m in (dev_manifest, prod_manifest)} == {"dev", "prod"}
    assert prod_manifest.key != prod_again.key


def test_results_of_nodes_the_manifest_does_not_know_are_ignored() -> None:
    stray = dbt_test("stray")
    evidence = DbtRunEvidence(manifest(NOT_NULL), run_results(result(NOT_NULL, "pass"), result(stray, "fail")))

    assert set(by_check(build(evidence))) == {"not_null_orders_id"}


def test_the_manifest_round_trips() -> None:
    evidence = DbtRunEvidence(manifest(NOT_NULL), run_results(result(NOT_NULL, "pass")), artifact_uri=PREFIX)

    built = build(evidence)

    assert QualityManifest.from_json(built.to_json()) == built


def test_read_dbt_evidence_reads_the_run_prefix_and_returns_none_when_it_cannot() -> None:
    files = {
        f"{PREFIX}/target/manifest.json": manifest(NOT_NULL),
        f"{PREFIX}/target/run_results.json": run_results(result(NOT_NULL, "pass")),
    }

    def read(uri: str) -> dict[str, Any]:
        return files[uri]

    found = read_dbt_evidence(PREFIX + "/", selected_tests=[NOT_NULL["unique_id"]], read=read)
    assert found is not None
    assert found.incomplete_marker is None
    assert found.selected_tests == frozenset({NOT_NULL["unique_id"]})
    assert found.artifact_uri == f"{PREFIX}/target/run_results.json"

    assert read_dbt_evidence(None, read=read) is None
    assert read_dbt_evidence(PREFIX, read=lambda _uri: (_ for _ in ()).throw(OSError("denied"))) is None
    del files[f"{PREFIX}/target/run_results.json"]
    assert read_dbt_evidence(PREFIX, read=read) is None
    marker = {"status": "artifacts_incomplete", "generations_without_results": [1]}
    files[f"{PREFIX}/artifacts_incomplete.json"] = marker
    failed = read_dbt_evidence(PREFIX, read=read)
    assert failed is not None and failed.run_results is None and failed.incomplete_marker == marker
