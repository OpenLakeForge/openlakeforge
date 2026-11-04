import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("dagster")

from dagster import DefaultScheduleStatus, materialize  # noqa: E402
from openlakeforge_core import FULL_REFRESH_TAG, full_refresh_tags  # noqa: E402
from openlakeforge_runtime import product_dagster  # noqa: E402
from openlakeforge_runtime.bronze_csv import BronzeLoadResult  # noqa: E402
from openlakeforge_runtime.product_dagster import (  # noqa: E402
    ProductDefinitionSpec,
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


def test_spec_names_the_job_and_dbt_project_from_the_product() -> None:
    assert _SPEC.job_name == "revenue_pipeline"
    assert _SPEC.dbt_project_dir.parts[-3:] == ("gold", "revenue", "dbt")


def test_only_prod_gets_a_daily_schedule_and_it_is_stopped() -> None:
    (schedule,) = build_stage_schedules(_SPEC, stage=" PROD ")

    assert (schedule.name, schedule.job_name, schedule.cron_schedule) == (
        "revenue_daily",
        "revenue_pipeline",
        "0 3 * * *",
    )
    assert schedule.default_status == DefaultScheduleStatus.STOPPED


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

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        pass


def test_dbt_artifacts_upload_only_the_files_the_run_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    for name, value in {
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino",
        "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
        "OPENLAKEFORGE_QUERY_TRINO_TLS_PORT": "8443",
        "OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME": "https",
        "OPENLAKEFORGE_QUERY_TRINO_CA": "/ca.crt",
        "OPENLAKEFORGE_DBT_TRINO_USER": "olf-prod-runtime",
        "OPENLAKEFORGE_DBT_TRINO_METHOD": "ldap",
        "OPENLAKEFORGE_DBT_TRINO_PASSWORD": "pw",
        "OPENLAKEFORGE_CATALOG_NAME": "prod_cat",
    }.items():
        monkeypatch.setenv(name, value)

    product_dagster.gold_maintenance.connect()

    assert seen == {
        "host": "trino",
        "port": 8443,
        "user": "olf-prod-runtime",
        "catalog": "prod_cat",
        "http_scheme": "https",
        "auth": ("olf-prod-runtime", "pw"),
        "verify": "/ca.crt",
    }
