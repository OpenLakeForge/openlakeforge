"""The e2e reading of a real run's quality evidence (#345, #346): Dagster checks, manifest, OpenMetadata."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from openlakeforge_core import Product
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult
from openlakeforge_core.quality_sync import OmStatus

from olf.infra.e2e import _quality_evidence as evidence
from olf.infra.e2e._quality_evidence import CaseTarget, CheckEvaluation
from olf.infra.e2e._shell import E2EError

PRODUCT = Product(domain_name="sales", id="order_revenue", display_name="Order Revenue")
RUN = "1f2e3d4c-run"
STAGE = "dev"
AT = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)
REPORT = "s3://ops/activations/dev/floe/reports/sales/orders/run.json"
DBT_TABLE_TEST = "openlakeforge_dbt_unique_combination_of_columns_mart_order_revenue_by_day_order_date__region"
DBT_COLUMN_TEST = "not_null_mart_order_revenue_by_day_order_date"
FLOE = ("floe_cast_error", "floe_not_null", "floe_unique", "floe_schema_mismatch", "floe_file_status")


def floe_metadata(check: str, **extra: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {"rows": 100, "accepted": 98, "rejected": 2, "entity_report_uri": REPORT, "run_id": "floe-1"}
    if check in ("floe_cast_error", "floe_not_null", "floe_unique"):
        meta["violations_total"] = 2 if check == "floe_not_null" else 0
    return {**meta, **extra}


def run_checks(tables: tuple[str, ...] = ("orders", "accounts")) -> list[CheckEvaluation]:
    found = [CheckEvaluation(("sales", t), c, True, "ERROR", floe_metadata(c)) for t in tables for c in FLOE]
    found.append(CheckEvaluation(("order_revenue", "mart_order_revenue_by_day"), DBT_TABLE_TEST, True, "ERROR"))
    found.append(CheckEvaluation(("order_revenue", "mart_order_revenue_by_day"), DBT_COLUMN_TEST, False, "WARN"))
    return found


def record(engine: str, table: str, check: str, outcome: QualityOutcome, **extra: Any) -> QualityResult:
    return QualityResult(
        engine=engine,
        stage=STAGE,
        domain="sales",
        product="order_revenue",
        table=table,
        check_id=check,
        engine_severity="reject" if engine == "floe" else "error",
        outcome=outcome,
        pipeline_outcome=PipelineOutcome.CONTINUED,
        observed_at=AT,
        dagster_run_id=RUN,
        **extra,
    )


def manifest(
    *, complete: bool = True, drop: str | None = None, dbt_warn: QualityOutcome = QualityOutcome.WARN
) -> QualityManifest:
    gold = "order_revenue_gold.mart_order_revenue_by_day"
    results = [
        record("floe", t, c, QualityOutcome.PASSED, artifact_uri=REPORT, metrics={"rows_total": 100})
        for t in ("orders", "accounts")
        for c in FLOE
        if (t, c) != ("orders", drop)
    ]
    results.append(record("dbt", gold, DBT_TABLE_TEST, QualityOutcome.PASSED, metrics={"failures": 0}))
    results.append(record("dbt", gold, DBT_COLUMN_TEST, dbt_warn, column="order_date", metrics={"failures": 3}))
    return QualityManifest(STAGE, "sales", "order_revenue", RUN, tuple(results), complete=complete)


def test_check_evaluations_are_read_from_the_run_event_log_with_plain_metadata() -> None:
    page = {
        "__typename": "EventConnection",
        "events": [
            {"__typename": "ExecutionStepStartEvent"},
            {
                "__typename": "AssetCheckEvaluationEvent",
                "evaluation": {
                    "checkName": "floe_not_null",
                    "success": False,
                    "severity": "WARN",
                    "assetKey": {"path": ["sales", "orders"]},
                    "metadataEntries": [
                        {"label": "rows", "intRepr": "4294967296"},
                        {"label": "entity_report_uri", "text": REPORT},
                        {"label": "file_status_counts", "jsonString": json.dumps({"success": 1})},
                        {"label": "floe/rejection_rate", "floatValue": 0.02},
                    ],
                },
            },
        ],
    }

    [found] = evidence.parse_check_evaluations([page])

    assert found == CheckEvaluation(
        ("sales", "orders"),
        "floe_not_null",
        False,
        "WARN",
        {
            "rows": 4294967296,
            "entity_report_uri": REPORT,
            "file_status_counts": {"success": 1},
            "floe/rejection_rate": 0.02,
        },
    )
    assert found.outcome is QualityOutcome.WARN


def test_the_latest_run_that_selected_silver_is_taken_over_a_newer_gold_only_run() -> None:
    runs = [
        {"runId": "gold-only", "status": "SUCCESS", "assetSelection": [{"path": ["order_revenue", "mart_a"]}]},
        {"runId": "failed", "status": "FAILURE", "assetSelection": None},
        {"runId": "selected", "status": "SUCCESS", "assetSelection": [{"path": ["sales", "orders"]}]},
        {"runId": "whole-job", "status": "SUCCESS", "assetSelection": None},
    ]

    assert evidence.pick_full_run(runs, "sales") == "selected"
    assert evidence.pick_full_run(runs[3:], "sales") == "whole-job"
    assert evidence.pick_full_run(runs[:2], "sales") is None


def test_a_full_run_has_five_floe_checks_per_silver_table_and_dbt_checks_on_gold() -> None:
    checks = evidence.assert_run_checks(run_checks(), PRODUCT)

    assert sorted(checks.silver) == ["accounts", "orders"]
    assert all(sorted(found) == sorted(FLOE) for found in checks.silver.values())
    assert sorted(checks.dbt) == sorted([DBT_TABLE_TEST, DBT_COLUMN_TEST])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: [e for e in c if (e.asset, e.name) != (("sales", "orders"), "floe_unique")], "not the five"),
        (lambda c: [*c, c[0]], "more than once"),
        (lambda c: [CheckEvaluation(("sales", "orders"), "not_null_orders_id", True, "ERROR"), *c], "not the five"),
        (lambda c: [e for e in c if e.asset[0] == "sales"], "no order_revenue Gold table has a dbt check"),
        (lambda c: [e for e in c if e.asset[0] != "sales"], "no sales Silver table has a Floe check"),
        (
            lambda c: [
                CheckEvaluation(e.asset, e.name, e.passed, e.severity, floe_metadata(e.name, rows=None)) for e in c
            ],
            "no rows count",
        ),
        (
            lambda c: [
                CheckEvaluation(
                    e.asset,
                    e.name,
                    e.passed,
                    e.severity,
                    {k: v for k, v in e.metadata.items() if k != "violations_total"},
                )
                for e in c
            ],
            "no violations_total count",
        ),
        (
            lambda c: [
                CheckEvaluation(e.asset, e.name, e.passed, e.severity, floe_metadata(e.name, fallback="summary_only"))
                for e in c
            ],
            "summary only",
        ),
        (
            lambda c: [
                CheckEvaluation(e.asset, e.name, e.passed, e.severity, floe_metadata(e.name, entity_report_uri=""))
                for e in c
            ],
            "no entity report reference",
        ),
        (lambda c: [*c, CheckEvaluation(("crm", "orders"), "floe_unique", True, "ERROR")], "not a sales Silver table"),
    ],
)
def test_checks_that_are_not_the_expected_evidence_fail(change: Any, message: str) -> None:
    with pytest.raises(E2EError, match=message):
        evidence.assert_run_checks(change(run_checks()), PRODUCT)


def test_a_complete_manifest_that_agrees_with_the_run_passes() -> None:
    evidence.assert_manifest(manifest(), evidence.assert_run_checks(run_checks(), PRODUCT), stage=STAGE, run_id=RUN)


@pytest.mark.parametrize(
    ("built", "kwargs", "message"),
    [
        (lambda: manifest(complete=False), {}, "not complete"),
        (lambda: manifest(drop="floe_unique"), {}, "not one per Floe check"),
        (lambda: manifest(dbt_warn=QualityOutcome.PASSED), {}, "Dagster says warn"),
        (manifest, {"run_id": "another-run"}, "is for stage dev run"),
    ],
)
def test_a_manifest_that_disagrees_with_the_run_fails(built: Any, kwargs: dict[str, str], message: str) -> None:
    checks = evidence.assert_run_checks(run_checks(), PRODUCT)
    with pytest.raises(E2EError, match=message):
        evidence.assert_manifest(built(), checks, **{"stage": STAGE, "run_id": RUN, **kwargs})


def test_manifest_floe_tables_must_be_the_tables_dagster_checked() -> None:
    checks = evidence.assert_run_checks(run_checks(("orders",)), PRODUCT)
    with pytest.raises(E2EError, match=r"Floe records name tables \['accounts', 'orders'\]"):
        evidence.assert_manifest(manifest(), checks, stage=STAGE, run_id=RUN)


def test_evidence_lines_carry_outcome_counts_and_a_report_without_a_query() -> None:
    checks = run_checks()
    presigned = [
        CheckEvaluation(
            e.asset,
            e.name,
            e.passed,
            e.severity,
            floe_metadata(e.name, entity_report_uri=REPORT + "?X-Amz-Signature=s"),
        )
        if e.asset[0] == "sales"
        else e
        for e in checks
    ]

    lines = evidence.evidence_lines(evidence.assert_run_checks(presigned, PRODUCT), manifest(), PRODUCT)

    assert (
        "floe_not_null on sales_silver.orders: passed, rows=100, accepted=98, rejected=2, violations=2, "
        f"report={REPORT}" in lines
    )
    assert (
        f"floe_file_status on sales_silver.accounts: passed, rows=100, accepted=98, rejected=2, report={REPORT}"
        in lines
    )
    assert f"dbt {DBT_COLUMN_TEST} on order_revenue_gold.mart_order_revenue_by_day: warn, failures=3, report=" in lines
    assert len(lines) == 2 * 5 + 2
    assert not any("X-Amz" in line for line in lines)


def test_plain_uri_drops_credentials_query_and_fragment() -> None:
    assert evidence.plain_uri("https://user:pw@host/a/b.json?X-Amz-Signature=s#f") == "https://host/a/b.json"
    assert evidence.plain_uri(REPORT) == REPORT


def test_case_targets_are_a_floe_case_on_silver_and_a_table_level_dbt_case_on_gold() -> None:
    floe, dbt = evidence.case_targets(
        manifest(),
        database_fqn="lakehouse.lakehouse_dev",
        silver_schemas={"sales": "lakehouse.lakehouse_dev.sales_silver"},
        gold_schemas=None,
    )

    stamp = int(AT.timestamp() * 1000)
    assert floe == CaseTarget(
        'lakehouse.lakehouse_dev.sales_silver.accounts."olf.floe.floe_not_null"', stamp, RUN, OmStatus.SUCCESS
    )
    assert dbt == CaseTarget(
        f'lakehouse.lakehouse_dev.order_revenue_gold.mart_order_revenue_by_day."olf.dbt.{DBT_TABLE_TEST}"',
        stamp,
        RUN,
        OmStatus.SUCCESS,
    )


def test_a_column_dbt_case_is_named_under_its_column() -> None:
    only_column = QualityManifest(
        STAGE, "sales", "order_revenue", RUN, tuple(r for r in manifest().results if r.check_id != DBT_TABLE_TEST)
    )

    _, dbt = evidence.case_targets(
        only_column,
        database_fqn="lakehouse.lakehouse_dev",
        silver_schemas=None,
        gold_schemas={"order_revenue": "lakehouse.lakehouse_dev.dev_order_revenue_gold"},
    )

    assert dbt.fqn == (
        "lakehouse.lakehouse_dev.dev_order_revenue_gold.mart_order_revenue_by_day.order_date"
        f'."olf.dbt.{DBT_COLUMN_TEST}"'
    )
    # The column test warned: the publisher's default and a configured warn status each name its expected status.
    assert dbt.status is OmStatus.ABORTED
    _, failed = evidence.case_targets(
        only_column,
        database_fqn="lakehouse.lakehouse_dev",
        silver_schemas=None,
        gold_schemas=None,
        warn_status=OmStatus.FAILED,
    )
    assert failed.status is OmStatus.FAILED


def cronjob_with(env: list[dict[str, str]]) -> dict[str, Any]:
    return {"spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "r", "env": env}]}}}}}}


def test_the_warn_status_is_the_reconcilers_own_or_the_publishers_default() -> None:
    configured = cronjob_with([{"name": "OPENLAKEFORGE_QUALITY_WARN_STATUS", "value": "Failed"}])
    invalid = cronjob_with([{"name": "OPENLAKEFORGE_QUALITY_WARN_STATUS", "value": "Warn"}])

    assert evidence.reconciler_warn_status(configured) is OmStatus.FAILED
    assert evidence.reconciler_warn_status(cronjob_with([])) is OmStatus.ABORTED
    assert evidence.reconciler_warn_status(None) is OmStatus.ABORTED
    with pytest.raises(E2EError, match="not an OpenMetadata status"):
        evidence.reconciler_warn_status(invalid)


def om_result(run: str, timestamp: int, status: str = "Success") -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "testCaseStatus": status,
        "testResultValue": [{"name": "dagster_run_id", "value": run}],
    }


def test_only_the_runs_own_results_count() -> None:
    target = CaseTarget('t."olf.floe.floe_not_null"', 1000, RUN, OmStatus.SUCCESS)
    found = evidence.run_results([om_result("other-run", 1000), om_result(RUN, 1000)], RUN)

    assert evidence.assert_one_result(target, found, "now") == om_result(RUN, 1000)
    with pytest.raises(E2EError, match="expected one result"):
        evidence.assert_one_result(target, [om_result(RUN, 1000), om_result(RUN, 1000)], "now")
    with pytest.raises(E2EError, match="not the run's observation time 1000"):
        evidence.assert_one_result(target, [om_result(RUN, 2000)], "now")


def test_a_result_under_another_status_than_the_outcome_publishes_as_fails() -> None:
    passed = CaseTarget('t."olf.floe.floe_not_null"', 1000, RUN, OmStatus.SUCCESS)
    warned = CaseTarget('t."olf.dbt.t"', 1000, RUN, OmStatus.ABORTED)

    with pytest.raises(E2EError, match="is Failed now, but its manifest outcome publishes as Success"):
        evidence.assert_one_result(passed, [om_result(RUN, 1000, "Failed")], "now")
    with pytest.raises(E2EError, match="publishes as Aborted"):
        evidence.assert_one_result(warned, [om_result(RUN, 1000, "Success")], "now")
    assert evidence.assert_one_result(passed, [om_result(RUN, 1000, "Success")], "now")["testCaseStatus"] == "Success"
    assert evidence.assert_one_result(warned, [om_result(RUN, 1000, "Aborted")], "now")["testCaseStatus"] == "Aborted"


class Catalog:
    """OpenMetadata as the reconciler leaves it: a pass publishes each target once, or every time if `duplicates`."""

    def __init__(
        self,
        targets: list[CaseTarget],
        *,
        backlog: int = 0,
        duplicates: bool = False,
        status: str | None = None,
        restatus: str | None = None,
    ) -> None:
        self.targets = targets
        self.backlog = backlog
        self.duplicates = duplicates
        # `status` publishes every result under it; `restatus` overwrites the stored status on a later pass.
        self.status = status
        self.restatus = restatus
        self.passes = 0
        self.stored: dict[str, list[dict[str, Any]]] = {t.fqn: [] for t in targets}

    def reconcile(self) -> None:
        self.passes += 1
        if self.passes <= self.backlog:
            return
        for target in self.targets:
            stored = self.stored[target.fqn]
            if self.duplicates or not stored:
                stored.append(om_result(target.run_id, target.timestamp, self.status or target.status.value))
            elif self.restatus:
                stored[0] = om_result(target.run_id, target.timestamp, self.restatus)

    def results(self, target: CaseTarget) -> list[dict[str, Any]]:
        return list(self.stored[target.fqn])


TARGETS = [
    CaseTarget('silver."olf.floe.floe_not_null"', 1000, RUN, OmStatus.SUCCESS),
    CaseTarget('gold."olf.dbt.t"', 1000, RUN, OmStatus.ABORTED),
]


def test_passes_continue_through_a_backlog_and_a_second_pass_adds_nothing() -> None:
    catalog = Catalog(TARGETS, backlog=2)

    evidence._publish_and_replay(TARGETS, catalog.reconcile, catalog.results)

    assert catalog.passes == 4
    assert all(len(found) == 1 for found in catalog.stored.values())


def test_a_second_pass_that_publishes_the_run_again_fails() -> None:
    catalog = Catalog(TARGETS, duplicates=True)

    with pytest.raises(E2EError, match="after a second reconciler pass"):
        evidence._publish_and_replay(TARGETS, catalog.reconcile, catalog.results)


def test_a_result_never_published_fails_after_the_bounded_passes() -> None:
    catalog = Catalog(TARGETS, backlog=100)

    with pytest.raises(E2EError, match="after a reconciler pass"):
        evidence._publish_and_replay(TARGETS, catalog.reconcile, catalog.results)
    assert catalog.passes == evidence.MAX_RECONCILER_PASSES


def test_a_run_published_under_the_wrong_status_fails() -> None:
    catalog = Catalog(TARGETS, status="Failed")

    with pytest.raises(E2EError, match="is Failed after a reconciler pass, but its manifest outcome publishes as"):
        evidence._publish_and_replay(TARGETS, catalog.reconcile, catalog.results)


def test_a_second_pass_that_changes_the_status_fails() -> None:
    catalog = Catalog(TARGETS, restatus="Failed")

    with pytest.raises(E2EError, match="is Failed after a second reconciler pass"):
        evidence._publish_and_replay(TARGETS, catalog.reconcile, catalog.results)


ALIASED = "order_revenue_gold.revenue_by_day_v2"
DBT_RESULTS = "s3://ops/activations/dev/run-artifacts/dbt/sales/order_revenue/run/target/run_results.json"


def aliased_manifest() -> QualityManifest:
    """The run's records as the dbt adapter writes them for `mart_order_revenue_by_day` built under an alias."""
    results = tuple(
        dataclasses.replace(r, table=ALIASED, artifact_uri=DBT_RESULTS) if r.engine == "dbt" else r
        for r in manifest().results
    )
    return QualityManifest(STAGE, "sales", "order_revenue", RUN, results, complete=True)


def test_a_dbt_record_on_an_aliased_model_matches_the_model_dagster_checked() -> None:
    checks = evidence.assert_run_checks(run_checks(), PRODUCT)
    dbt_manifest = {
        "nodes": {
            "model.p.mart_order_revenue_by_day": {
                "resource_type": "model",
                "name": "mart_order_revenue_by_day",
                "schema": "order_revenue_gold",
                "alias": "revenue_by_day_v2",
            },
            "model.p.mart_plain": {"resource_type": "model", "name": "mart_plain", "schema": "order_revenue_gold"},
            f"test.p.{DBT_TABLE_TEST}": {"resource_type": "test", "name": DBT_TABLE_TEST, "schema": "x"},
        }
    }
    tables = evidence.model_tables(dbt_manifest)

    assert tables == {
        "mart_order_revenue_by_day": ALIASED,
        "mart_plain": "order_revenue_gold.mart_plain",
    }
    evidence.assert_manifest(aliased_manifest(), checks, stage=STAGE, run_id=RUN, gold_tables=tables)
    with pytest.raises(E2EError, match=r"recorded on order_revenue_gold.revenue_by_day_v2, Dagster checked"):
        evidence.assert_manifest(aliased_manifest(), checks, stage=STAGE, run_id=RUN)
    elsewhere = {"mart_order_revenue_by_day": "order_revenue_gold.revenue_by_day_v3"}
    with pytest.raises(E2EError, match=r"\(order_revenue_gold.revenue_by_day_v3\)"):
        evidence.assert_manifest(aliased_manifest(), checks, stage=STAGE, run_id=RUN, gold_tables=elsewhere)


def test_the_dbt_manifest_is_the_one_beside_the_runs_run_results() -> None:
    assert evidence.dbt_manifest_uri(aliased_manifest()) == DBT_RESULTS.removesuffix("run_results.json") + (
        "manifest.json"
    )
    assert evidence.dbt_manifest_uri(manifest()) is None


def test_the_openmetadata_case_of_an_aliased_model_is_on_its_physical_table() -> None:
    _, dbt = evidence.case_targets(
        aliased_manifest(), database_fqn="lakehouse.lakehouse_dev", silver_schemas=None, gold_schemas=None
    )

    assert dbt.fqn == f'lakehouse.lakehouse_dev.order_revenue_gold.revenue_by_day_v2."olf.dbt.{DBT_TABLE_TEST}"'
