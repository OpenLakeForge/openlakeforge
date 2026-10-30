import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("dagster")

from dagster import DefaultScheduleStatus, materialize  # noqa: E402
from libs import product_dagster  # noqa: E402
from libs.bronze_csv import BronzeLoadResult  # noqa: E402
from libs.product_dagster import (  # noqa: E402
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
