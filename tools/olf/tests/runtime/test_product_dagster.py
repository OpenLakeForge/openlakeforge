import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

pytest.importorskip("dagster")

from dagster import (  # noqa: E402
    AssetCheckResult,
    AssetCheckSeverity,
    AssetCheckSpec,
    AssetKey,
    AssetOut,
    DefaultScheduleStatus,
    Definitions,
    Failure,
    asset,
    define_asset_job,
    in_process_executor,
    materialize,
    multi_asset,
)
from openlakeforge_core import FULL_REFRESH_TAG, full_refresh_tags  # noqa: E402
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome, QualityResult  # noqa: E402
from openlakeforge_runtime import product_dagster  # noqa: E402
from openlakeforge_runtime.bronze_csv import BronzeLoadResult  # noqa: E402
from openlakeforge_runtime.dbt_quality import DbtRunEvidence  # noqa: E402
from openlakeforge_runtime.floe_quality import FloeCheckEvidence, FloeEntityEvidence  # noqa: E402
from openlakeforge_runtime.product_dagster import (  # noqa: E402
    ProductCadence,
    ProductDefinitionSpec,
    ResolvedProductInputs,
    SourceDefinitionSpec,
    build_source_definitions,
    build_stage_schedules,
)

_SPEC = ProductDefinitionSpec(
    domain="sales",
    product="revenue",
    silver_inputs=("orders",),
    bronze_inputs=(("shop", "orders"),),
    gold_assets=("daily",),
)


def test_spec_names_the_job_and_selects_its_folder_of_the_gold_project() -> None:
    assert _SPEC.job_name == "revenue_pipeline"
    assert _SPEC.dbt_project_dir.parts[-2:] == ("lakehouse_code", "gold")
    assert _SPEC.dbt_select == "group:revenue"


def test_only_prod_gets_a_daily_schedule_and_it_is_stopped() -> None:
    (schedule,) = build_stage_schedules(_SPEC, stage=" PROD ")

    assert (schedule.name, schedule.job_name, schedule.cron_schedule) == (
        "revenue_daily",
        "revenue_pipeline",
        "0 3 * * *",
    )
    assert schedule.default_status == DefaultScheduleStatus.STOPPED


def test_a_declared_cadence_sets_the_prod_schedule_and_it_is_still_stopped() -> None:
    spec = ProductDefinitionSpec(
        domain="sales", product="revenue", cadence=ProductCadence("0 * * * *", timezone="Europe/Paris")
    )

    (schedule,) = build_stage_schedules(spec, stage="prod")

    assert (schedule.cron_schedule, schedule.execution_timezone) == ("0 * * * *", "Europe/Paris")
    assert schedule.default_status == DefaultScheduleStatus.STOPPED
    assert build_stage_schedules(spec, stage="dev") == []


def test_a_cadence_that_is_enabled_in_prod_gets_a_running_schedule_there_only() -> None:
    spec = ProductDefinitionSpec(
        domain="sales", product="revenue", cadence=ProductCadence("0 * * * *", enabled_in_prod=True)
    )

    (schedule,) = build_stage_schedules(spec, stage="prod")

    assert schedule.default_status == DefaultScheduleStatus.RUNNING
    for stage in ("dev", "uat", ""):
        assert build_stage_schedules(spec, stage=stage) == []


def test_a_manual_only_cadence_cannot_be_enabled_in_prod() -> None:
    with pytest.raises(ValueError, match="manual-only"):
        ProductCadence(cron=None, enabled_in_prod=True)


def test_a_manual_cadence_defines_no_schedule_in_prod() -> None:
    spec = ProductDefinitionSpec(domain="sales", product="revenue", cadence=ProductCadence(cron=None))

    assert build_stage_schedules(spec, stage="prod") == []


def test_an_invalid_cadence_is_refused_when_the_schedule_is_built() -> None:
    spec = ProductDefinitionSpec(domain="sales", product="revenue", cadence=ProductCadence("not a cron"))

    with pytest.raises(Exception, match="cron"):
        build_stage_schedules(spec, stage="prod")


@pytest.mark.parametrize("stage", ["dev", "uat", "", "staging"])
def test_other_stages_get_no_schedule(stage: str) -> None:
    assert build_stage_schedules(_SPEC, stage=stage) == []


def test_the_stage_defaults_to_the_one_the_activation_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_STAGE", "prod")
    assert len(build_stage_schedules(_SPEC)) == 1

    monkeypatch.delenv("OPENLAKEFORGE_STAGE")
    assert build_stage_schedules(_SPEC) == []


def _source(loaded: list[tuple[str, ...]]) -> SourceDefinitionSpec:
    def loader(resources: tuple[str, ...]) -> dict[str, BronzeLoadResult]:
        loaded.append(resources)
        return {
            r: BronzeLoadResult(entity=r, rows=2, uri=f"s3://bronze/shop/{r}.csv", source_file=f"{r}.csv")
            for r in resources
        }

    return SourceDefinitionSpec(source="shop", resources=("orders", "customers"), bronze_loader=loader)


def test_materializing_a_source_loads_every_resource_and_restores_the_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAGSTER_RUN_ID", raising=False)
    loaded: list[tuple[str, ...]] = []
    assets = list(build_source_definitions(_source(loaded)).assets)

    result = materialize(assets)

    assert result.success
    assert loaded == [("orders", "customers")]
    assert "DAGSTER_RUN_ID" not in os.environ


def test_a_subset_run_loads_only_the_selected_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAGSTER_RUN_ID", "outer")
    loaded: list[tuple[str, ...]] = []
    assets = list(build_source_definitions(_source(loaded)).assets)

    result = materialize(assets, selection=["shop/orders"])

    assert result.success
    assert loaded == [("orders",)]
    assert os.environ["DAGSTER_RUN_ID"] == "outer"


def test_a_bronze_full_refresh_loads_incrementally_and_extracts_nothing_again() -> None:
    """A full refresh re-ingests the Bronze already landed; the loader is not asked to start over."""
    loaded: list[tuple[str, ...]] = []
    assets = list(build_source_definitions(_source(loaded)).assets)

    result = materialize(assets, tags=full_refresh_tags("bronze"))

    assert result.success
    assert loaded == [("orders", "customers")]


def test_malformed_refresh_tags_fail_before_anything_loads() -> None:
    loaded: list[tuple[str, ...]] = []
    assets = list(build_source_definitions(_source(loaded)).assets)

    result = materialize(assets, tags={FULL_REFRESH_TAG: "bronze"}, raise_on_error=False)

    assert not result.success
    assert loaded == []


class _Log:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        self.errors.append(message)


def test_dbt_artifacts_upload_only_the_files_the_run_produced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "dbt.log").write_text("log")
    uploaded: list[tuple[str, str, str]] = []

    def upload(path: Path, *, base_uri: str, relative_key: str) -> str:
        uploaded.append((path.name, base_uri, relative_key))
        return f"{base_uri}/{relative_key}"

    monkeypatch.setenv("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI", "s3://ops/runs")
    monkeypatch.setattr(product_dagster, "upload_file_to_base_uri", upload)

    product_dagster._upload_dbt_artifacts(SimpleNamespace(run_id="r1", log=_Log()), _SPEC, tmp_path)

    assert sorted(uploaded) == [
        ("dbt.log", "s3://ops/runs", "dbt/sales/revenue/r1/logs/dbt.log"),
        ("manifest.json", "s3://ops/runs", "dbt/sales/revenue/r1/target/manifest.json"),
    ]


def test_dbt_artifacts_are_skipped_with_a_warning_when_no_destination_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI", raising=False)
    monkeypatch.setattr(product_dagster, "upload_file_to_base_uri", lambda *a, **k: pytest.fail("uploaded"))
    context = SimpleNamespace(run_id="r1", log=_Log())

    product_dagster._upload_dbt_artifacts(context, _SPEC, tmp_path)

    assert "OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI is not set" in context.log.warnings[0]


class _DbtBuildFailed(Exception):
    pass


def _failing_build():
    yield "materialized"
    raise _DbtBuildFailed("model orders failed")


def _ops_store(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> dict[str, str]:
    store: dict[str, str] = {}

    def upload(path: Path, *, base_uri: str, relative_key: str) -> str:
        if fail:
            raise OSError("ops store unreachable")
        store[relative_key] = path.read_text()
        return f"{base_uri}/{relative_key}"

    monkeypatch.setenv("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI", "s3://ops/runs")
    monkeypatch.setattr(product_dagster, "upload_file_to_base_uri", upload)
    return store


def _drain(target_paths: list[Path], context: SimpleNamespace) -> None:
    for _ in product_dagster._retaining_artifacts_on_failure(context, _SPEC, target_paths, _failing_build()):
        pass


def test_a_failed_dbt_build_still_uploads_its_artifacts_and_raises_the_dbt_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "run_results.json").write_text('{"results": []}')
    store = _ops_store(monkeypatch)

    with pytest.raises(_DbtBuildFailed, match="model orders failed"):
        _drain([tmp_path], SimpleNamespace(run_id="r1", log=_Log()))

    assert store == {"dbt/sales/revenue/r1/target/run_results.json": '{"results": []}'}


def test_a_failed_build_without_run_results_leaves_an_incomplete_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dbt.log").write_text("boom")
    store = _ops_store(monkeypatch)

    with pytest.raises(_DbtBuildFailed):
        _drain([tmp_path], SimpleNamespace(run_id="r1", log=_Log()))

    marker = json.loads(store["dbt/sales/revenue/r1/artifacts_incomplete.json"])
    assert marker["status"] == "artifacts_incomplete"
    assert marker["dagster_run_id"] == "r1"
    assert store["dbt/sales/revenue/r1/logs/dbt.log"] == "boom"


def test_a_later_generation_failing_keeps_the_results_of_the_earlier_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "gen1", tmp_path / "gen2"
    first.mkdir()
    second.mkdir()
    (first / "run_results.json").write_text('{"results": [{"unique_id": "model.a"}]}')
    (second / "dbt.log").write_text("gen2 crashed")
    store = _ops_store(monkeypatch)

    with pytest.raises(_DbtBuildFailed):
        _drain([first, second], SimpleNamespace(run_id="r1", log=_Log()))

    assert json.loads(store["dbt/sales/revenue/r1/target/run_results.json"])["results"] == [{"unique_id": "model.a"}]
    assert store["dbt/sales/revenue/r1/logs/dbt.log"] == "gen2 crashed"
    marker = json.loads(store["dbt/sales/revenue/r1/artifacts_incomplete.json"])
    assert marker["generations_without_results"] == [2]


def test_a_build_that_failed_before_any_dbt_invocation_leaves_an_incomplete_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ops_store(monkeypatch)

    with pytest.raises(_DbtBuildFailed):
        _drain([], SimpleNamespace(run_id="r1", log=_Log()))

    assert list(store) == ["dbt/sales/revenue/r1/artifacts_incomplete.json"]


def test_a_build_that_failed_before_any_dbt_invocation_leaves_the_baked_manifest_to_name_its_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baked = tmp_path / "manifest.json"
    baked.write_text('{"nodes": {}}')
    store = _ops_store(monkeypatch)

    with pytest.raises(_DbtBuildFailed):
        for _ in product_dagster._retaining_artifacts_on_failure(
            SimpleNamespace(run_id="r1", log=_Log()), _SPEC, [], _failing_build(), baked_manifest=baked
        ):
            pass

    assert store["dbt/sales/revenue/r1/target/manifest.json"] == '{"nodes": {}}'
    assert "dbt/sales/revenue/r1/artifacts_incomplete.json" in store


def test_the_selected_tests_are_recorded_beside_the_dbt_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _ops_store(monkeypatch)

    product_dagster._upload_selected_tests(
        SimpleNamespace(run_id="r1", log=_Log()), _SPEC, ["test.shop.a", "test.shop.b"]
    )

    assert json.loads(store["dbt/sales/revenue/r1/selected_tests.json"]) == {
        "selected_tests": ["test.shop.a", "test.shop.b"]
    }


def test_a_failing_selected_tests_upload_is_logged_and_does_not_stop_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ops_store(monkeypatch, fail=True)
    context = SimpleNamespace(run_id="r1", log=_Log())

    product_dagster._upload_selected_tests(context, _SPEC, ["test.shop.a"])

    assert "ops store unreachable" in context.log.errors[0]


def test_the_dbt_error_survives_a_failing_upload_and_the_upload_failure_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "run_results.json").write_text("{}")
    _ops_store(monkeypatch, fail=True)
    context = SimpleNamespace(run_id="r1", log=_Log())

    with pytest.raises(_DbtBuildFailed, match="model orders failed"):
        _drain([tmp_path], context)

    assert "ops store unreachable" in context.log.errors[0]


def test_a_successful_build_uploads_nothing_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _ops_store(monkeypatch)

    events = list(product_dagster._retaining_artifacts_on_failure(None, _SPEC, [], iter(["a", "b"])))

    assert events == ["a", "b"]
    assert store == {}


def _fake_maintain(monkeypatch: pytest.MonkeyPatch, results: list[dict]) -> list[dict]:
    calls: list[dict] = []

    def maintain(config_path: str, **kwargs: object) -> list[dict]:
        calls.append({"config_path": config_path, **kwargs})
        return results

    monkeypatch.setattr(product_dagster.floe, "maintain", maintain)
    monkeypatch.setattr(product_dagster, "_floe_profile", lambda: "/profile.yml")
    return calls


_MAINTAINED = [
    {"entity": "orders", "expired_snapshots": 2, "orphan_files": ["a", "b"], "orphan_bytes": 30},
    {"entity": "accounts", "expired_snapshots": 1, "orphan_files": [], "orphan_bytes": 0},
]


def test_maintenance_runs_floe_with_its_retention_defaults_and_reports_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_maintain(monkeypatch, _MAINTAINED)

    result = product_dagster.silver_maintenance.execute_in_process()

    assert result.success
    assert calls == [
        {
            "config_path": str(product_dagster.FLOE_PROJECT),
            "profile_path": "/profile.yml",
            "older_than_days": 7,
            "retain_last": 10,
            "orphan_delay_days": 3,
            "dry_run": False,
        }
    ]
    (output,) = (event for event in result.all_events if event.event_type_value == "STEP_OUTPUT")
    metadata = {key: value.value for key, value in output.step_output_data.metadata.items()}
    assert metadata["expired_snapshots"] == 3
    assert metadata["orphan_files"] == 2
    assert metadata["orphan_bytes"] == 30
    assert metadata["dry_run"] is False
    assert metadata["tables"] == _MAINTAINED


def test_run_config_sets_dry_run_and_the_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_maintain(monkeypatch, [])
    config = {"dry_run": True, "older_than_days": 14, "retain_last": 20, "orphan_delay_days": 5}

    result = product_dagster.silver_maintenance.execute_in_process(
        run_config={"ops": {"maintain_silver": {"config": config}}}
    )

    assert result.success
    assert {key: calls[0][key] for key in config} == config


def test_a_floe_refusal_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(config_path: str, **kwargs: object) -> list[dict]:
        raise RuntimeError("below the retention floor")

    monkeypatch.setattr(product_dagster.floe, "maintain", refuse)
    monkeypatch.setattr(product_dagster, "_floe_profile", lambda: "/profile.yml")

    assert not product_dagster.silver_maintenance.execute_in_process(raise_on_error=False).success


def test_the_maintenance_job_is_scheduled_only_in_prod_and_stopped() -> None:
    for stage in ("dev", "uat", ""):
        definitions = product_dagster.build_maintenance_definitions(stage=stage)
        assert definitions.resolve_job_def("silver_maintenance")
        assert not definitions.schedules

    (schedule,) = product_dagster.build_maintenance_definitions(stage="prod").schedules
    assert schedule.job_name == "silver_maintenance"
    assert schedule.default_status == DefaultScheduleStatus.STOPPED


class _FakeTrino:
    """Answers the table listing and counts; records every other statement; fails those matching `fail_on`."""

    def __init__(self, tables: list[str], *, fail_on: str = "") -> None:
        self.tables, self.fail_on, self.statements = tables, fail_on, []

    def cursor(self) -> "_FakeTrino":
        return self

    def execute(self, sql: str) -> None:
        self.statements.append(sql)
        self._sql = sql
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("trino unavailable")

    def fetchall(self) -> list[tuple]:
        if "information_schema.tables" in self._sql:
            return [(name,) for name in self.tables]
        return [(4,)] if "count(*)" in self._sql else []


def _gold(monkeypatch: pytest.MonkeyPatch, trino: _FakeTrino) -> None:
    monkeypatch.setattr(product_dagster.gold_maintenance, "connect", lambda: trino)
    monkeypatch.setattr(product_dagster.gold_maintenance, "gold_schemas", lambda root: ["sales_gold"])


def _gold_metadata(result) -> dict:
    (output,) = (event for event in result.all_events if event.event_type_value == "STEP_OUTPUT")
    return {key: value.value for key, value in output.step_output_data.metadata.items()}


def test_gold_maintenance_expires_then_removes_orphans_of_each_table(monkeypatch: pytest.MonkeyPatch) -> None:
    trino = _FakeTrino(["daily", "weekly"])
    _gold(monkeypatch, trino)

    result = product_dagster.gold_maintenance_job.execute_in_process()

    assert result.success
    assert trino.statements[1:] == [
        'ALTER TABLE "sales_gold"."daily" EXECUTE expire_snapshots(retention_threshold => \'7d\', retain_last => 10)',
        'ALTER TABLE "sales_gold"."daily" EXECUTE remove_orphan_files(retention_threshold => \'7d\')',
        'ALTER TABLE "sales_gold"."weekly" EXECUTE expire_snapshots(retention_threshold => \'7d\', retain_last => 10)',
        'ALTER TABLE "sales_gold"."weekly" EXECUTE remove_orphan_files(retention_threshold => \'7d\')',
    ]
    assert _gold_metadata(result)["tables"] == 2


def test_gold_dry_run_counts_snapshots_past_the_window_and_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    trino = _FakeTrino(["daily"])
    _gold(monkeypatch, trino)

    result = product_dagster.gold_maintenance_job.execute_in_process(
        run_config={"ops": {"maintain_gold": {"config": {"dry_run": True, "older_than_days": 14}}}}
    )

    assert result.success
    assert not any("EXECUTE" in statement for statement in trino.statements)
    assert "daily$snapshots" in trino.statements[1] and "INTERVAL '14' DAY" in trino.statements[1]
    metadata = _gold_metadata(result)
    assert metadata["snapshots_past_window"] == 4
    assert metadata["results"][0]["snapshots_past_window"] == 4


@pytest.mark.parametrize("key", ["older_than_days", "orphan_delay_days"])
def test_gold_retention_below_trinos_minimum_fails_before_touching_a_table(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    trino = _FakeTrino(["daily"])
    _gold(monkeypatch, trino)

    result = product_dagster.gold_maintenance_job.execute_in_process(
        run_config={"ops": {"maintain_gold": {"config": {key: 6}}}}, raise_on_error=False
    )

    assert not result.success
    assert trino.statements == []


def test_an_interrupted_gold_table_fails_the_run_after_the_other_tables_are_maintained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trino = _FakeTrino(["daily", "weekly"], fail_on='"daily" EXECUTE expire_snapshots')
    _gold(monkeypatch, trino)

    result = product_dagster.gold_maintenance_job.execute_in_process(raise_on_error=False)

    assert not result.success
    assert any('"weekly" EXECUTE remove_orphan_files' in statement for statement in trino.statements)
    assert not any('"daily" EXECUTE remove_orphan_files' in statement for statement in trino.statements)


def test_the_gold_job_is_scheduled_only_in_prod_and_stopped() -> None:
    for stage in ("dev", "uat", ""):
        definitions = product_dagster.build_gold_maintenance_definitions(stage=stage)
        assert definitions.resolve_job_def("gold_maintenance")
        assert not definitions.schedules

    (schedule,) = product_dagster.build_gold_maintenance_definitions(stage="prod").schedules
    assert (schedule.name, schedule.job_name) == ("gold_maintenance_weekly", "gold_maintenance")
    assert schedule.default_status == DefaultScheduleStatus.STOPPED


def test_gold_connects_with_the_dbt_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    trino = SimpleNamespace(
        dbapi=SimpleNamespace(connect=lambda **kwargs: seen.update(kwargs)),
        auth=SimpleNamespace(BasicAuthentication=lambda user, password: (user, password)),
    )
    monkeypatch.setitem(__import__("sys").modules, "trino", trino)
    monkeypatch.setattr(product_dagster.gold_maintenance, "mint_token", lambda: "polaris-token")
    for name, value in {
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino",
        "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
        "OPENLAKEFORGE_DBT_TRINO_USER": "olf-prod-runtime",
        "OPENLAKEFORGE_CATALOG_NAME": "prod_cat",
    }.items():
        monkeypatch.setenv(name, value)

    product_dagster.gold_maintenance.connect()

    assert seen == {
        "host": "trino",
        "port": 8080,
        "user": "olf-prod-runtime",
        "catalog": "prod_cat",
        "http_scheme": "http",
        "extra_credential": [("token", "polaris-token")],
    }


# What the example products listed by hand before the spec derived it.
_EXAMPLES = {
    ("sales", "order_revenue"): ProductDefinitionSpec(
        domain="sales",
        product="order_revenue",
        # Its marts read sales_core through ref(), so the product loads no Silver of its own.
        silver_inputs=(),
        bronze_inputs=(),
        gold_assets=(
            "mart_order_revenue_by_day",
            "mart_order_revenue_by_channel",
            "mart_order_revenue_margin_by_product",
            "mart_order_revenue_by_account_segment",
        ),
    ),
    ("sales", "customer_health"): ProductDefinitionSpec(
        domain="sales",
        product="customer_health",
        silver_inputs=("accounts", "subscriptions", "support_tickets", "nps_responses"),
        bronze_inputs=tuple(
            ("crm", name) for name in ("accounts", "subscriptions", "support_tickets", "nps_responses")
        ),
        gold_assets=("mart_customer_health_score", "mart_churn_risk_by_segment", "mart_support_sla_by_customer"),
    ),
    ("supply_chain", "inventory_reliability"): ProductDefinitionSpec(
        domain="supply_chain",
        product="inventory_reliability",
        silver_inputs=(
            "warehouses",
            "suppliers",
            "inventory_snapshots",
            "purchase_orders",
            "shipments",
            "stockout_events",
        ),
        bronze_inputs=tuple(
            ("erp", name)
            for name in (
                "warehouses",
                "suppliers",
                "inventory_snapshots",
                "purchase_orders",
                "shipments",
                "stockout_events",
            )
        ),
        gold_assets=("mart_inventory_position", "mart_supplier_delivery_reliability", "mart_stockout_risk"),
    ),
}


@pytest.mark.parametrize(("domain", "product"), sorted(_EXAMPLES))
def test_a_spec_of_only_domain_and_product_selects_what_the_explicit_lists_did(domain: str, product: str) -> None:
    explicit = _EXAMPLES[(domain, product)]

    derived = ProductDefinitionSpec(domain=domain, product=product).resolve()
    listed = explicit.resolve()

    assert listed == ResolvedProductInputs(explicit.silver_inputs, explicit.bronze_inputs, explicit.gold_assets)
    assert set(derived.silver_inputs) == set(listed.silver_inputs)
    assert set(derived.bronze_inputs) == set(listed.bronze_inputs)
    assert set(derived.gold_assets) == set(listed.gold_assets)


def test_an_explicit_list_wins_and_the_bronze_inputs_follow_the_silver_ones() -> None:
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue", silver_inputs=("orders", "channels"))

    resolved = spec.resolve()

    assert resolved.silver_inputs == ("orders", "channels")
    assert resolved.bronze_inputs == (("crm", "orders"), ("crm", "channels"))
    assert "mart_order_revenue_by_day" in resolved.gold_assets


def test_a_dbt_source_that_is_not_a_silver_table_of_the_domain_is_refused() -> None:
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue", silver_inputs=("warehouses",))

    with pytest.raises(ValueError, match="not Floe entities of domain 'sales'"):
        spec.resolve()


def _model(name: str, product: str, *reads: str) -> dict:
    return {
        "unique_id": f"model.gold.{name}",
        "name": name,
        "resource_type": "model",
        "package_name": "gold",
        "fqn": ["gold", product, name],
        "depends_on": {"nodes": list(reads)},
    }


def _silver_source(domain: str, name: str) -> tuple[str, dict]:
    unique_id = f"source.gold.{domain}.{name}"
    return unique_id, {"name": name, "source_name": domain, "resource_type": "source", "package_name": "gold"}


def _manifest(tmp_path: Path, *, source: str, domain: str = "sales") -> Path:
    source_id, source_node = _silver_source(domain, source)
    unused_id, unused_node = _silver_source("sales", "promotions")
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {"project_name": "gold"},
                "nodes": {
                    "model.gold.mart": _model("mart", "order_revenue", source_id),
                    "model.gold.other": _model("other", "customer_health", unused_id),
                },
                "sources": {source_id: source_node, unused_id: unused_node},
            }
        )
    )
    return path


def test_a_product_takes_its_own_folders_models_and_only_the_silver_they_read(tmp_path: Path) -> None:
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue")

    resolved = spec.resolve(_manifest(tmp_path, source="orders"))

    assert (resolved.silver_inputs, resolved.bronze_inputs, resolved.gold_assets) == (
        ("orders",),
        (("crm", "orders"),),
        ("mart",),
    )


def test_a_product_reading_another_domains_silver_is_refused(tmp_path: Path) -> None:
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue")

    with pytest.raises(ValueError, match=r"reads \['supply_chain.shipments'\]"):
        spec.resolve(_manifest(tmp_path, source="shipments", domain="supply_chain"))


def test_a_product_takes_the_silver_its_tests_read_but_not_other_products_tests(tmp_path: Path) -> None:
    orders_id, orders = _silver_source("sales", "orders")
    accounts_id, accounts = _silver_source("sales", "accounts")
    channels_id, channels = _silver_source("sales", "channels")
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {"project_name": "gold"},
                "nodes": {
                    "model.gold.mart": _model("mart", "order_revenue", orders_id),
                    "model.gold.other": _model("other", "customer_health", orders_id),
                    # A relationships test reads a source no model of the product does.
                    "test.gold.mart_accounts": {
                        "resource_type": "test",
                        "depends_on": {"nodes": ["model.gold.mart", accounts_id]},
                    },
                    "test.gold.other_channels": {
                        "resource_type": "test",
                        "depends_on": {"nodes": ["model.gold.other", channels_id]},
                    },
                },
                "sources": {orders_id: orders, accounts_id: accounts, channels_id: channels},
            }
        )
    )

    resolved = ProductDefinitionSpec(domain="sales", product="order_revenue").resolve(path)

    assert resolved.silver_inputs == ("accounts", "orders")
    assert resolved.bronze_inputs == (("crm", "accounts"), ("crm", "orders"))


def test_a_product_reading_only_another_products_public_mart_needs_no_silver(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {"project_name": "gold"},
                "nodes": {
                    "model.gold.revenue": _model("revenue", "order_revenue"),
                    "model.gold.forecast": _model("forecast", "revenue_forecast", "model.gold.revenue"),
                },
                "sources": {},
            }
        )
    )
    spec = ProductDefinitionSpec(domain="sales", product="revenue_forecast")

    resolved = spec.resolve(path)

    assert (resolved.silver_inputs, resolved.bronze_inputs, resolved.gold_assets) == ((), (), ("forecast",))


def test_a_product_without_a_folder_of_models_is_refused(tmp_path: Path) -> None:
    spec = ProductDefinitionSpec(domain="sales", product="returns")

    with pytest.raises(ValueError, match="no models under models/returns/"):
        spec.resolve(_manifest(tmp_path, source="orders"))


def test_a_renamed_dbt_source_asks_for_explicit_silver_inputs(tmp_path: Path) -> None:
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue")

    with pytest.raises(ValueError, match="pass silver_inputs explicitly"):
        spec.resolve(_manifest(tmp_path, source="orders_renamed"))


def test_explicit_silver_inputs_cover_a_renamed_dbt_source(tmp_path: Path) -> None:
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue", silver_inputs=("orders",))

    resolved = spec.resolve(_manifest(tmp_path, source="orders_renamed"))

    assert (resolved.silver_inputs, resolved.bronze_inputs, resolved.gold_assets) == (
        ("orders",),
        (("crm", "orders"),),
        ("mart",),
    )


def test_a_missing_manifest_names_the_product(tmp_path: Path) -> None:
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue")

    with pytest.raises(ValueError, match="product 'order_revenue': no dbt manifest"):
        spec.resolve(tmp_path / "absent.json")


@pytest.mark.parametrize(("domain", "product"), sorted(_EXAMPLES))
def test_a_product_job_runs_its_steps_in_the_runs_own_process(
    domain: str, product: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_DBT_PROFILES_DIR", str(tmp_path))
    spec = ProductDefinitionSpec(domain=domain, product=product)

    # Unresolved: the job's Bronze and Silver assets come from the merged code location, not these definitions.
    (job,) = product_dagster.build_product_definitions(spec).jobs

    assert job.name == spec.job_name
    assert job.executor_def.name == "in_process"
    assert "execution" not in (job.config or {})


@pytest.mark.parametrize(("domain", "product"), sorted(_EXAMPLES))
def test_a_product_run_carries_its_silver_domain_for_the_run_queue(
    domain: str, product: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_DBT_PROFILES_DIR", str(tmp_path))

    (job,) = product_dagster.build_product_definitions(ProductDefinitionSpec(domain=domain, product=product)).jobs

    assert job.tags[product_dagster.SILVER_DOMAIN_TAG] == domain


@pytest.mark.parametrize(("domain", "product"), sorted(_EXAMPLES))
def test_a_product_job_names_its_product_beside_its_domain(
    domain: str, product: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_DBT_PROFILES_DIR", str(tmp_path))

    (job,) = product_dagster.build_product_definitions(ProductDefinitionSpec(domain=domain, product=product)).jobs

    assert job.tags[product_dagster.PRODUCT_TAG] == product


def test_the_run_queue_admits_one_run_per_silver_domain() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    values = yaml.safe_load((repo_root / "infra/helm/values/local/dagster.yaml").read_text(encoding="utf-8"))

    coordinator = values["dagsterDaemon"]["runCoordinator"]

    assert coordinator["enabled"] is True
    assert coordinator["type"] == "QueuedRunCoordinator"
    assert coordinator["config"]["queuedRunCoordinator"]["tagConcurrencyLimits"] == [
        {
            "key": product_dagster.SILVER_DOMAIN_TAG,
            "value": {"applyLimitPerUniqueValue": True},
            "limit": 1,
        }
    ]


def _product_defs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **spec_fields) -> Definitions:  # noqa: ANN003
    monkeypatch.setenv("OPENLAKEFORGE_DBT_PROFILES_DIR", str(tmp_path))
    spec = ProductDefinitionSpec(domain="sales", product="order_revenue", **spec_fields)
    return product_dagster.build_product_definitions(spec)


def test_a_product_job_carries_no_dagster_retry_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    defs = _product_defs(tmp_path, monkeypatch)
    (job,) = defs.jobs

    # A policy would make Dagster restart a step a terminated run interrupted, instead of stopping the run.
    assert job.op_retry_policy is None
    assert all(gold.op.retry_policy is None for gold in defs.assets)


def test_a_bronze_load_failing_transiently_succeeds_on_a_later_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openlakeforge_runtime.bronze_retry.time.sleep", lambda _s: None)
    attempts: list[tuple[str, ...]] = []

    def flaky(resources: tuple[str, ...]) -> dict[str, BronzeLoadResult]:
        attempts.append(resources)
        if len(attempts) == 1:
            raise ConnectionError("source reset the connection")
        return {
            r: BronzeLoadResult(entity=r, rows=1, uri=f"s3://bronze/shop/{r}.csv", source_file=r) for r in resources
        }

    source = SourceDefinitionSpec(source="shop", resources=("orders",), bronze_loader=flaky)
    defs = Definitions(assets=list(build_source_definitions(source).assets), jobs=[define_asset_job("bronze")])

    assert defs.resolve_job_def("bronze").execute_in_process().success
    assert len(attempts) == 2


def test_a_bronze_load_is_not_retried_when_the_source_turns_retries_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openlakeforge_runtime.bronze_retry.time.sleep", lambda _s: None)
    attempts: list[int] = []

    def down(_resources: tuple[str, ...]) -> dict[str, BronzeLoadResult]:
        attempts.append(1)
        raise ConnectionError("down")

    source = SourceDefinitionSpec(source="shop", resources=("orders",), bronze_loader=down, bronze_retry_attempts=0)
    defs = Definitions(assets=list(build_source_definitions(source).assets), jobs=[define_asset_job("bronze")])

    assert not defs.resolve_job_def("bronze").execute_in_process(raise_on_error=False).success
    assert len(attempts) == 1


def test_a_silver_style_failure_runs_once_in_a_product_style_job() -> None:
    runs: list[int] = []

    @asset
    def silver_like() -> None:
        runs.append(1)
        raise Failure("floe contract failure")

    job = define_asset_job("product", executor_def=in_process_executor)
    defs = Definitions(assets=[silver_like], jobs=[job])

    assert not defs.resolve_job_def("product").execute_in_process(raise_on_error=False).success
    assert len(runs) == 1


def _local_dagster_values() -> dict:
    repo_root = Path(__file__).resolve().parents[4]
    return yaml.safe_load((repo_root / "infra/helm/values/local/dagster.yaml").read_text(encoding="utf-8"))


def test_a_run_whose_pod_is_lost_is_failed_by_the_daemon_within_a_bounded_time() -> None:
    monitoring = _local_dagster_values()["dagsterDaemon"]["runMonitoring"]

    assert monitoring["enabled"] is True
    assert monitoring["pollIntervalSeconds"] <= 60
    assert monitoring["startTimeoutSeconds"] <= 600
    assert 0 < monitoring["maxRuntimeSeconds"] <= 24 * 3600


def test_the_size_derived_run_limit_is_left_to_the_terraform_module() -> None:
    queue = _local_dagster_values()["dagsterDaemon"]["runCoordinator"]["config"]["queuedRunCoordinator"]

    # The module sets it from release/query-sizes.yaml; a value here would shadow the sized one.
    assert "maxConcurrentRuns" not in queue


_OPS = "s3://ops/activations/dev"


def _quality_store(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {}

    def upload(path: Path, *, bucket: str, key: str) -> str:
        if fail:
            raise OSError("ops store unreachable")
        store[(bucket, key)] = path.read_text()
        return f"s3://{bucket}/{key}"

    monkeypatch.setenv("OPENLAKEFORGE_STAGE", "dev")
    monkeypatch.setenv("OPENLAKEFORGE_ARTIFACT_BASE_URI", _OPS)
    monkeypatch.setenv("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI", f"{_OPS}/run-artifacts")

    def read(uri: str) -> dict[str, Any]:
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        return json.loads(store[(bucket, key)])

    monkeypatch.setattr(product_dagster, "upload_file", upload)
    monkeypatch.setattr(product_dagster, "read_json_uri", read)
    monkeypatch.setattr(product_dagster, "read_dbt_evidence", lambda *a, **k: None)
    monkeypatch.setattr(product_dagster, "read_entity_report", lambda *a, **k: None)
    return store


def _floe_events(*checks: tuple[str, bool]) -> list[SimpleNamespace]:
    """The ASSET_CHECK_EVALUATION log entries of one `orders` Silver step."""
    return [
        SimpleNamespace(
            dagster_event=SimpleNamespace(
                event_specific_data=SimpleNamespace(
                    asset_key=AssetKey(["sales", "orders"]),
                    check_name=name,
                    passed=passed,
                    severity="ERROR",
                    metadata={"floe_run_status": "success" if passed else "aborted"},
                )
            )
        )
        for name, passed in checks
    ]


def _quality_context(run_id: str, events: list[SimpleNamespace]) -> SimpleNamespace:
    instance = SimpleNamespace(all_logs=lambda run, of_type=None: events)
    return SimpleNamespace(run_id=run_id, log=_Log(), instance=instance)


def _manifest_of(store: dict[tuple[str, str], str], run_id: str) -> QualityManifest:
    key = f"activations/dev/run-artifacts/quality/sales/revenue/{run_id}/manifest.json"
    return QualityManifest.from_json(store[("ops", key)])


def test_a_floe_abort_leaves_a_valid_quality_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _quality_store(monkeypatch)
    context = _quality_context("r1", _floe_events(("floe_cast_error", False), ("floe_not_null", True)))

    product_dagster._publish_quality_manifest(context, _SPEC, ("orders",))

    manifest = _manifest_of(store, "r1")
    assert (manifest.stage, manifest.dagster_run_id) == ("dev", "r1")
    assert {r.check_id: r.outcome for r in manifest.results}["floe_cast_error"] is QualityOutcome.ERROR
    assert context.log.errors == []


def test_a_failed_dbt_build_leaves_a_quality_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _quality_store(monkeypatch)
    node: dict[str, Any] = {
        "resource_type": "test",
        "package_name": "shop",
        "unique_id": "test.shop.not_null_daily_id.abc",
        "name": "not_null_daily_id",
        "attached_node": None,
        "column_name": "id",
        "config": {"severity": "ERROR"},
        "depends_on": {"nodes": []},
    }
    evidence = DbtRunEvidence(
        manifest={"metadata": {"project_name": "shop"}, "nodes": {node["unique_id"]: node}, "sources": {}},
        run_results={"results": [{"unique_id": node["unique_id"], "status": "fail", "failures": 2}]},
        incomplete_marker={"status": "artifacts_incomplete"},
    )
    read: list[str] = []

    def read_evidence(prefix: str, **_: object) -> DbtRunEvidence:
        read.append(prefix)
        return evidence

    monkeypatch.setattr(product_dagster, "read_dbt_evidence", read_evidence)

    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))

    (record,) = _manifest_of(store, "r1").results
    assert (record.engine, record.outcome, record.pipeline_outcome) == (
        "dbt",
        QualityOutcome.ERROR,
        PipelineOutcome.BLOCKED,
    )
    assert read == [f"{_OPS}/run-artifacts/dbt/sales/revenue/r1"]


def test_a_manifest_is_complete_only_once_the_dbt_evidence_has_been_read(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _quality_store(monkeypatch)
    context = _quality_context("r1", _floe_events(("floe_not_null", True)))

    product_dagster._publish_quality_manifest(context, _SPEC, ("orders",))
    assert _manifest_of(store, "r1").complete is False

    monkeypatch.setattr(
        product_dagster,
        "read_dbt_evidence",
        lambda *a, **k: DbtRunEvidence(
            manifest={"metadata": {"project_name": "shop"}, "nodes": {}, "sources": {}}, run_results={"results": []}
        ),
    )
    product_dagster._publish_quality_manifest(context, _SPEC, ("orders",))
    assert _manifest_of(store, "r1").complete is True

    monkeypatch.setattr(product_dagster, "read_dbt_evidence", lambda *a, **k: None)
    product_dagster._publish_quality_manifest(context, _SPEC, ("orders",))
    assert _manifest_of(store, "r1").complete is True


def test_two_runs_of_one_product_write_two_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _quality_store(monkeypatch)
    events = _floe_events(("floe_not_null", True))

    for run_id in ("r1", "r2"):
        product_dagster._publish_quality_manifest(_quality_context(run_id, events), _SPEC, ("orders",))

    assert {key for _, key in store} == {
        f"activations/dev/run-artifacts/quality/sales/revenue/{run}/manifest.json" for run in ("r1", "r2")
    }


def test_a_failing_quality_upload_is_logged_and_never_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    _quality_store(monkeypatch, fail=True)
    context = _quality_context("r1", _floe_events(("floe_not_null", True)))

    product_dagster._publish_quality_manifest(context, _SPEC, ("orders",))

    assert "ops store unreachable" in context.log.errors[0]


def test_a_step_with_no_quality_evidence_uploads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _quality_store(monkeypatch)

    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))

    assert store == {}


def test_a_failing_quality_upload_changes_no_steps_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _quality_store(monkeypatch, fail=True)
    entities = [FloeEntityEvidence("orders", checks=(FloeCheckEvidence("floe_not_null", True, "ERROR", {}),))]
    monkeypatch.setattr(product_dagster, "_floe_entities", lambda *a: entities)

    @asset
    def passing() -> None: ...

    @asset(deps=[passing])
    def failing() -> None:
        raise Failure("floe aborted")

    job = define_asset_job("quality", hooks=product_dagster._quality_manifest_hooks(_SPEC, ("orders",)))
    defs = Definitions(assets=[passing, failing], jobs=[job])
    result = defs.get_job_def("quality").execute_in_process(raise_on_error=False)

    assert [e.step_key for e in result.get_step_success_events()] == ["passing"]
    assert [e.step_key for e in result.get_step_failure_events()] == ["failing"]


def _dbt_test_node(name: str) -> dict[str, Any]:
    return {
        "resource_type": "test",
        "package_name": "shop",
        "unique_id": f"test.shop.{name}",
        "name": name,
        "attached_node": None,
        "column_name": "id",
        "config": {"severity": "ERROR"},
        "depends_on": {"nodes": []},
    }


def test_the_quality_manifest_of_a_run_reads_the_selected_tests_the_build_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Generation 1's test failed and generation 2's never started: only the recorded selection can tell.
    store = _quality_store(monkeypatch)
    prefix = f"{_OPS}/run-artifacts/dbt/sales/revenue/r1"
    nodes = {n["unique_id"]: n for n in (_dbt_test_node("gen1_test"), _dbt_test_node("gen2_test"))}
    files = {
        f"{prefix}/target/manifest.json": {"metadata": {"project_name": "shop"}, "nodes": nodes, "sources": {}},
        f"{prefix}/target/run_results.json": {"results": [{"unique_id": "test.shop.gen1_test", "status": "fail"}]},
        f"{prefix}/selected_tests.json": {"selected_tests": sorted(nodes)},
    }
    monkeypatch.setattr(product_dagster, "read_dbt_evidence", lambda p, **k: _read_with(files, p, **k))

    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))

    results = {r.check_id: r for r in _manifest_of(store, "r1").results}
    assert results["gen1_test"].pipeline_outcome is PipelineOutcome.BLOCKED
    assert results["gen2_test"].outcome is QualityOutcome.NOT_RUN


def _read_with(files: dict[str, Any], prefix: str, **kwargs: Any) -> Any:
    from openlakeforge_runtime.dbt_quality import read_dbt_evidence

    return read_dbt_evidence(prefix, read=lambda uri: files[uri], **kwargs)


def test_a_build_that_failed_before_run_results_leaves_every_selected_test_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _quality_store(monkeypatch)
    prefix = f"{_OPS}/run-artifacts/dbt/sales/revenue/r1"
    nodes = {n["unique_id"]: n for n in (_dbt_test_node("gen1_test"), _dbt_test_node("gen2_test"))}
    files = {
        f"{prefix}/target/manifest.json": {"metadata": {"project_name": "shop"}, "nodes": nodes, "sources": {}},
        f"{prefix}/artifacts_incomplete.json": {"status": "artifacts_incomplete"},
        f"{prefix}/selected_tests.json": {"selected_tests": sorted(nodes)},
    }
    monkeypatch.setattr(product_dagster, "read_dbt_evidence", lambda p, **k: _read_with(files, p, **k))

    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))

    results = _manifest_of(store, "r1").results
    assert {r.check_id for r in results} == {"gen1_test", "gen2_test"}
    assert {(r.outcome, r.pipeline_outcome) for r in results} == {(QualityOutcome.UNKNOWN, PipelineOutcome.UNKNOWN)}


def test_the_baked_fallback_manifest_names_the_selected_tests_but_not_their_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The baked manifest has an empty schema prefix while Glue adds one at run time, so its tables are not the run's.
    prefix = f"{_OPS}/run-artifacts/dbt/sales/revenue/r1"
    node = {**_dbt_test_node("gen1_test"), "attached_node": "model.shop.daily"}
    manifest = {
        "metadata": {"project_name": "shop"},
        "nodes": {node["unique_id"]: node, "model.shop.daily": {"name": "daily", "schema": "revenue_gold"}},
        "sources": {},
    }
    baked = tmp_path / "manifest.json"
    baked.write_text(json.dumps(manifest))
    ops = _ops_store(monkeypatch)
    with pytest.raises(_DbtBuildFailed):
        for _ in product_dagster._retaining_artifacts_on_failure(
            SimpleNamespace(run_id="r1", log=_Log()), _SPEC, [], _failing_build(), baked_manifest=baked
        ):
            pass
    store = _quality_store(monkeypatch)
    files = {
        f"{prefix}/target/manifest.json": manifest,
        f"{prefix}/artifacts_incomplete.json": json.loads(ops["dbt/sales/revenue/r1/artifacts_incomplete.json"]),
        f"{prefix}/selected_tests.json": {"selected_tests": [node["unique_id"]]},
    }
    monkeypatch.setattr(product_dagster, "read_dbt_evidence", lambda p, **k: _read_with(files, p, **k))

    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))

    (record,) = _manifest_of(store, "r1").results
    assert (record.check_id, record.table, record.column) == ("gen1_test", None, None)
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.UNKNOWN, PipelineOutcome.UNKNOWN)


def test_a_dbt_process_that_died_before_writing_a_manifest_still_gets_the_baked_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target dir is registered when dbt starts, so a profile or parse failure leaves it registered but empty.
    prefix = f"{_OPS}/run-artifacts/dbt/sales/revenue/r1"
    node = {**_dbt_test_node("gen1_test"), "attached_node": "model.shop.daily"}
    manifest = {
        "metadata": {"project_name": "shop"},
        "nodes": {node["unique_id"]: node, "model.shop.daily": {"name": "daily", "schema": "revenue_gold"}},
        "sources": {},
    }
    baked = tmp_path / "baked" / "manifest.json"
    baked.parent.mkdir()
    baked.write_text(json.dumps(manifest))
    empty_target = tmp_path / "target"
    empty_target.mkdir()
    ops = _ops_store(monkeypatch)
    with pytest.raises(_DbtBuildFailed):
        for _ in product_dagster._retaining_artifacts_on_failure(
            SimpleNamespace(run_id="r1", log=_Log()), _SPEC, [empty_target], _failing_build(), baked_manifest=baked
        ):
            pass
    assert "dbt/sales/revenue/r1/target/manifest.json" in ops
    marker = json.loads(ops["dbt/sales/revenue/r1/artifacts_incomplete.json"])
    assert marker["baked_manifest"] is True
    store = _quality_store(monkeypatch)
    files = {
        f"{prefix}/target/manifest.json": manifest,
        f"{prefix}/artifacts_incomplete.json": marker,
        f"{prefix}/selected_tests.json": {"selected_tests": [node["unique_id"]]},
    }
    monkeypatch.setattr(product_dagster, "read_dbt_evidence", lambda p, **k: _read_with(files, p, **k))

    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))

    (record,) = _manifest_of(store, "r1").results
    assert (record.check_id, record.table, record.column) == ("gen1_test", None, None)
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.UNKNOWN, PipelineOutcome.UNKNOWN)


def test_a_later_publish_never_turns_a_known_floe_outcome_into_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _quality_store(monkeypatch)
    passed = _floe_entities_with_report(checks_passed=True)
    monkeypatch.setattr(product_dagster, "_floe_entities", lambda *a: passed)
    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))
    first = {r.check_id: r.outcome for r in _manifest_of(store, "r1").results}
    assert QualityOutcome.PASSED in first.values()

    # The next step's report read fails: the same checks now come back without evidence.
    monkeypatch.setattr(product_dagster, "_floe_entities", lambda *a: [FloeEntityEvidence("orders", checks=())])
    product_dagster._publish_quality_manifest(_quality_context("r1", []), _SPEC, ("orders",))

    assert {r.check_id: r.outcome for r in _manifest_of(store, "r1").results} == first


def _floe_entities_with_report(*, checks_passed: bool) -> list[FloeEntityEvidence]:
    names = ("floe_cast_error", "floe_not_null")
    checks = tuple(FloeCheckEvidence(n, checks_passed, "ERROR", {}) for n in names)
    return [FloeEntityEvidence("orders", checks=checks, report={"files": [{"name": "orders.csv"}]})]


def test_a_later_publish_replaces_an_unknown_outcome_and_keeps_a_record_it_lacks() -> None:
    def record(check: str, outcome: QualityOutcome) -> QualityResult:
        return QualityResult(
            engine="floe",
            stage="dev",
            domain="sales",
            product="revenue",
            check_id=check,
            engine_severity="reject",
            outcome=outcome,
            pipeline_outcome=PipelineOutcome.UNKNOWN,
            observed_at=datetime(2026, 11, 16, tzinfo=UTC),
            dagster_run_id="r1",
        )

    def manifest(*results: QualityResult) -> QualityManifest:
        return QualityManifest(stage="dev", domain="sales", product="revenue", dagster_run_id="r1", results=results)

    published = manifest(record("a", QualityOutcome.UNKNOWN), record("b", QualityOutcome.PASSED))
    fresh = manifest(record("a", QualityOutcome.PASSED), record("c", QualityOutcome.UNKNOWN))

    merged = product_dagster._without_regressions(fresh, published)

    assert {r.check_id: r.outcome for r in merged.results} == {
        "a": QualityOutcome.PASSED,
        "b": QualityOutcome.PASSED,
        "c": QualityOutcome.UNKNOWN,
    }


def test_a_rewritten_manifest_keeps_the_first_observation_time_of_an_execution() -> None:
    def manifest(at: datetime) -> QualityManifest:
        result = QualityResult(
            engine="floe",
            stage="dev",
            domain="sales",
            product="revenue",
            check_id="a",
            engine_severity="reject",
            outcome=QualityOutcome.PASSED,
            pipeline_outcome=PipelineOutcome.CONTINUED,
            observed_at=at,
            dagster_run_id="r1",
        )
        return QualityManifest(
            stage="dev", domain="sales", product="revenue", dagster_run_id="r1", results=(result,)
        )

    first = datetime(2026, 11, 16, 10, tzinfo=UTC)
    merged = product_dagster._without_regressions(manifest(datetime(2026, 11, 16, 10, 9, tzinfo=UTC)), manifest(first))

    assert [r.observed_at for r in merged.results] == [first]


def test_a_product_job_attaches_the_quality_manifest_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (job,) = _product_defs(tmp_path, monkeypatch).jobs

    assert {hook.name for hook in job.hooks} == {
        "publish_quality_manifest_on_success",
        "publish_quality_manifest_on_failure",
    }


def test_the_hooks_publish_a_valid_manifest_when_a_silver_step_yields_checks_and_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _quality_store(monkeypatch)
    monkeypatch.setattr(product_dagster, "read_entity_report", lambda uri: {"files": [{"name": "orders.csv"}]})
    key = AssetKey(["sales", "orders"])

    @multi_asset(
        outs={"orders": AssetOut(key=key)},
        check_specs=[AssetCheckSpec(name, asset=key) for name in ("floe_cast_error", "floe_not_null")],
    )
    def silver():
        for name, passed in (("floe_cast_error", False), ("floe_not_null", True)):
            yield AssetCheckResult(
                asset_key=key,
                check_name=name,
                passed=passed,
                severity=AssetCheckSeverity.ERROR,
                metadata={"floe_run_status": "aborted", "entity_report_uri": "s3://ops/floe/orders/run.json"},
            )
        raise Failure("floe aborted")

    job = define_asset_job("quality_e2e", hooks=product_dagster._quality_manifest_hooks(_SPEC, ("orders",)))
    result = (
        Definitions(assets=[silver], jobs=[job]).get_job_def("quality_e2e").execute_in_process(raise_on_error=False)
    )

    assert not result.success
    manifest = _manifest_of(store, result.run_id)
    assert QualityManifest.from_json(manifest.to_json()) == manifest
    outcomes = {r.check_id: r.outcome for r in manifest.results}
    assert outcomes["floe_cast_error"] is QualityOutcome.ERROR
    assert outcomes["floe_not_null"] is QualityOutcome.PASSED


def _two_product_manifest(path: Path) -> Path:
    """A parsed Gold project of two products: `revenue_mart` of `consumer` refs `dim_account` of `sales_core`."""

    def model(name: str, product: str, parents: tuple[str, ...] = (), access: str = "private") -> dict[str, Any]:
        return {
            "unique_id": f"model.gold.{name}",
            "name": name,
            "resource_type": "model",
            "package_name": "gold",
            "path": f"{product}/{name}.sql",
            "original_file_path": f"models/{product}/{name}.sql",
            "fqn": ["gold", product, name],
            "config": {"materialized": "table", "group": product, "enabled": True},
            "depends_on": {"nodes": list(parents)},
            "description": "",
            "tags": [],
            "meta": {},
            "checksum": {"name": "sha256", "checksum": "x"},
            "schema": f"{product}_gold",
            "database": "catalog",
            "alias": name,
            "relation_name": f'"catalog"."{product}_gold"."{name}"',
            "group": product,
            "access": access,
            "columns": {},
            "unrendered_config": {},
        }

    nodes = {
        "model.gold.dim_account": model("dim_account", "sales_core", access="public"),
        "model.gold.revenue_mart": model("revenue_mart", "consumer", ("model.gold.dim_account",)),
    }
    groups = {
        f"group.gold.{name}": {
            "name": name,
            "resource_type": "group",
            "package_name": "gold",
            "path": "schema.yml",
            "original_file_path": "schema.yml",
            "unique_id": f"group.gold.{name}",
            "owner": {"name": name},
        }
        for name in ("sales_core", "consumer")
    }
    manifest = {
        "metadata": {"project_name": "gold", "dbt_version": "1.9.0"},
        "nodes": nodes,
        "sources": {},
        "exposures": {},
        "metrics": {},
        "semantic_models": {},
        "saved_queries": {},
        "unit_tests": {},
        "child_map": {"model.gold.dim_account": ["model.gold.revenue_mart"], "model.gold.revenue_mart": []},
        "parent_map": {"model.gold.dim_account": [], "model.gold.revenue_mart": ["model.gold.dim_account"]},
        "group_map": {"sales_core": ["model.gold.dim_account"], "consumer": ["model.gold.revenue_mart"]},
        "groups": groups,
        "macros": {},
        "docs": {},
        "disabled": {},
        "selectors": {},
        "files": {},
    }
    path.write_text(json.dumps(manifest))
    return path


def _consumer_defs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Definitions:
    manifest = _two_product_manifest(tmp_path / "manifest.json")
    monkeypatch.setenv("OPENLAKEFORGE_DBT_PROFILES_DIR", str(tmp_path))
    monkeypatch.setattr(product_dagster, "ensure_baked_manifest", lambda _project_dir: manifest)
    spec = ProductDefinitionSpec(domain="sales", product="consumer")
    return product_dagster.build_product_definitions(spec)


def test_a_ref_to_another_products_public_model_is_an_upstream_asset_the_job_does_not_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    defs = _consumer_defs(tmp_path, monkeypatch)

    (dbt_assets,) = defs.assets
    (spec,) = dbt_assets.specs
    job = defs.resolve_job_def("consumer_pipeline")

    assert spec.key == AssetKey(["consumer", "revenue_mart"])
    assert [dep.asset_key for dep in spec.deps] == [AssetKey(["sales_core", "dim_account"])]
    assert {tuple(key.path) for key in job.asset_layer.selected_asset_keys} == {("consumer", "revenue_mart")}


def test_the_dagster_graph_of_the_merged_location_carries_the_cross_product_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer = _consumer_defs(tmp_path, monkeypatch)

    @asset(key=AssetKey(["sales_core", "dim_account"]))
    def dim_account() -> None: ...

    graph = Definitions.merge(consumer, Definitions(assets=[dim_account])).resolve_asset_graph()

    assert graph.get(AssetKey(["consumer", "revenue_mart"])).parent_keys == {AssetKey(["sales_core", "dim_account"])}


def _noop_job() -> Any:
    @asset
    def nothing() -> None: ...

    return Definitions(assets=[nothing], jobs=[define_asset_job("noop")]).resolve_job_def("noop")


def _materialise(instance: Any, *path: str) -> None:
    @asset(key=AssetKey(list(path)))
    def built() -> None: ...

    materialize([built], instance=instance)


def test_a_consumer_fails_with_a_clear_message_when_its_producer_never_built() -> None:
    from dagster import DagsterInstance

    instance = DagsterInstance.ephemeral()

    with pytest.raises(
        product_dagster.UpstreamProductNotReady,
        match=r"consumer_pipeline cannot start: sales_core has never materialised dim_account on stage \w+; "
        r"run sales_core_pipeline first",
    ):
        product_dagster._require_upstream_products_ready(instance, "consumer", {"sales_core": ("dim_account",)})


def test_a_consumer_starts_once_its_producer_has_built() -> None:
    from dagster import DagsterInstance

    instance = DagsterInstance.ephemeral()
    _materialise(instance, "sales_core", "dim_account")

    product_dagster._require_upstream_products_ready(instance, "consumer", {"sales_core": ("dim_account",)})


def test_a_consumer_does_not_start_while_its_producer_is_running() -> None:
    from dagster import DagsterInstance, DagsterRunStatus

    instance = DagsterInstance.ephemeral()
    _materialise(instance, "sales_core", "dim_account")
    run = instance.create_run_for_job(job_def=_noop_job(), tags={product_dagster.PRODUCT_TAG: "sales_core"})
    assert run.status == DagsterRunStatus.NOT_STARTED

    with pytest.raises(
        product_dagster.UpstreamProductNotReady,
        match=rf"sales_core_pipeline run {run.run_id} is queued or in progress.*retry consumer_pipeline after it ends",
    ):
        product_dagster._require_upstream_products_ready(instance, "consumer", {"sales_core": ("dim_account",)})


def test_a_consumer_ignores_runs_of_other_products() -> None:
    from dagster import DagsterInstance

    instance = DagsterInstance.ephemeral()
    _materialise(instance, "sales_core", "dim_account")
    instance.create_run_for_job(job_def=_noop_job(), tags={product_dagster.PRODUCT_TAG: "unrelated"})

    product_dagster._require_upstream_products_ready(instance, "consumer", {"sales_core": ("dim_account",)})


def test_a_product_with_no_upstream_products_needs_no_check() -> None:
    product_dagster._require_upstream_products_ready(object(), "order_revenue", {})


def test_a_run_refused_by_the_readiness_guard_leaves_the_lineage_job_name_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dagster import DagsterInstance

    defs = _consumer_defs(tmp_path, monkeypatch)
    (dbt_assets,) = defs.assets
    (tmp_path / "profiles.yml").write_text("gold: {}\n", encoding="utf-8")
    # The resource checks its executable exists; the run is refused before dbt is ever started.
    monkeypatch.setenv("OPENLAKEFORGE_DBT_EXECUTABLE", sys.executable)
    monkeypatch.delenv("OPENLINEAGE_DBT_JOB_NAME", raising=False)

    result = materialize(
        [dbt_assets],
        instance=DagsterInstance.ephemeral(),
        resources={},
        raise_on_error=False,
    )

    assert not result.success
    assert "sales_core has never materialised dim_account" in str(
        [event.event_specific_data for event in result.get_step_failure_events()]
    )
    assert "OPENLINEAGE_DBT_JOB_NAME" not in os.environ


def test_a_run_refused_by_the_readiness_guard_leaves_its_incomplete_artifacts_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dagster import DagsterInstance

    defs = _consumer_defs(tmp_path, monkeypatch)
    (dbt_assets,) = defs.assets
    (tmp_path / "profiles.yml").write_text("gold: {}\n", encoding="utf-8")
    monkeypatch.setenv("OPENLAKEFORGE_DBT_EXECUTABLE", sys.executable)
    monkeypatch.setenv("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI", "s3://ops/run-artifacts")
    keys: list[str] = []

    def upload(path: Path, *, base_uri: str, relative_key: str) -> str:
        keys.append(relative_key)
        return f"{base_uri}/{relative_key}"

    monkeypatch.setattr(product_dagster, "upload_file_to_base_uri", upload)

    result = materialize([dbt_assets], instance=DagsterInstance.ephemeral(), raise_on_error=False)

    assert not result.success
    assert any(key.endswith("/artifacts_incomplete.json") for key in keys)
