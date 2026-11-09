from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from olf.common import project_code_check


def _source_asset(*, can_subset: bool, output_required: bool) -> SimpleNamespace:
    return SimpleNamespace(
        keys=(SimpleNamespace(path=("crm", "orders")),),
        can_subset=can_subset,
        op=SimpleNamespace(output_defs=(SimpleNamespace(name="orders", is_required=output_required),)),
    )


def test_bronze_subsetability_requires_one_optional_subsettable_multi_asset_per_source() -> None:
    inventory = SimpleNamespace(sources=(SimpleNamespace(name="crm"),))
    definitions = SimpleNamespace(assets=(_source_asset(can_subset=True, output_required=False),))

    project_code_check._verify_bronze_subsetability(inventory, definitions)


@pytest.mark.parametrize(
    ("can_subset", "output_required", "message"),
    [
        (False, False, "must support product-specific subsets"),
        (True, True, "must have optional outputs"),
    ],
)
def test_bronze_subsetability_rejects_non_subsettable_or_required_outputs(
    can_subset: bool, output_required: bool, message: str
) -> None:
    inventory = SimpleNamespace(sources=(SimpleNamespace(name="crm"),))
    definitions = SimpleNamespace(assets=(_source_asset(can_subset=can_subset, output_required=output_required),))

    with pytest.raises(RuntimeError, match=message):
        project_code_check._verify_bronze_subsetability(inventory, definitions)


def _job(executor: str, run_config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(executor_def=SimpleNamespace(name=executor), run_config=run_config)


def test_a_product_job_runs_its_steps_in_process() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")

    project_code_check._verify_in_process_execution(product, _job("in_process"))
    project_code_check._verify_in_process_execution(product, _job("in_process", {"ops": {}}))


def test_a_product_job_on_the_multiprocess_executor_is_refused() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")

    with pytest.raises(RuntimeError, match="order_revenue_job must use the in-process executor; it uses 'multi"):
        project_code_check._verify_in_process_execution(product, _job("multi_or_in_process_executor"))


def test_a_product_job_with_execution_run_config_is_refused() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")
    job = _job("in_process", {"execution": {"config": {"multiprocess": {"max_concurrent": 4}}}})

    with pytest.raises(RuntimeError, match="run config must not set execution"):
        project_code_check._verify_in_process_execution(product, job)


@pytest.mark.parametrize(
    "orchestration",
    [
        {"strategy": "sequential"},
        {"strategy": "sequential", "max_concurrent_entities": 4},
        {"strategy": "parallel", "max_concurrent_entities": 4},
        None,
    ],
)
def test_a_bounded_or_sequential_floe_orchestration_is_accepted(orchestration: dict | None) -> None:
    project_code_check._verify_floe_orchestration(orchestration)


def test_parallel_floe_orchestration_without_a_limit_is_refused() -> None:
    with pytest.raises(RuntimeError, match="must set max_concurrent_entities"):
        project_code_check._verify_floe_orchestration({"strategy": "parallel"})


@dataclass(frozen=True)
class _Key:
    """Hashable like Dagster's AssetKey, which keys `asset_deps`."""

    path: tuple[str, ...]


def _key(path: tuple[str, ...]) -> _Key:
    return _Key(path)


def _domain_job(
    entities: dict[str, tuple[str, str]], *, domain: str = "sales", can_subset: bool = True
) -> SimpleNamespace:
    """dagster-floe's asset for one domain's Job: an accepted and a rejected output per entity."""
    specs: list[SimpleNamespace] = []
    deps: dict[_Key, list[_Key]] = {}
    for table, bronze in entities.items():
        accepted, rejected = _key((domain, table)), _key((domain, "rejected", table))
        specs.append(SimpleNamespace(key=accepted, metadata={"floe/entity": table, "floe/output": "accepted"}))
        specs.append(SimpleNamespace(key=rejected, metadata={"floe/entity": table, "floe/output": "rejected"}))
        deps[accepted] = deps[rejected] = [_key(bronze)]
    return SimpleNamespace(
        op=SimpleNamespace(tags={"dagster/compute_kind": "floe"}),
        specs=specs,
        asset_deps=deps,
        can_subset=can_subset,
    )


def _silver_inventory(*tables: str) -> SimpleNamespace:
    entities = [SimpleNamespace(name=table, source="crm", resource=table) for table in tables or ("orders",)]
    return SimpleNamespace(domains=[SimpleNamespace(name="sales")], floe_entities=lambda _domain: entities)


def test_silver_assets_must_each_depend_on_their_own_bronze_resource() -> None:
    wired = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders")})])
    project_code_check._verify_silver_assets(_silver_inventory(), wired)

    shared = SimpleNamespace(assets=[_domain_job({"orders": ("sources", "openlakeforge", "crm")})])
    with pytest.raises(RuntimeError, match="must depend on its Bronze resource crm/orders"):
        project_code_check._verify_silver_assets(_silver_inventory(), shared)


def test_silver_assets_must_match_the_floe_entities() -> None:
    extra = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders"), "returns": ("crm", "returns")})])

    with pytest.raises(RuntimeError, match="do not match the Floe entities"):
        project_code_check._verify_silver_assets(_silver_inventory(), extra)


def test_a_domain_runs_as_one_subsettable_floe_job() -> None:
    split = SimpleNamespace(
        assets=[_domain_job({"orders": ("crm", "orders")}), _domain_job({"accounts": ("crm", "accounts")})]
    )
    with pytest.raises(RuntimeError, match="each domain must run as one Floe Job"):
        project_code_check._verify_silver_assets(_silver_inventory("orders", "accounts"), split)

    whole = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders")}, can_subset=False)])
    with pytest.raises(RuntimeError, match="must support subsets"):
        project_code_check._verify_silver_assets(_silver_inventory(), whole)


def test_a_declared_table_ending_in_rejected_is_not_mistaken_for_a_rejection_output() -> None:
    inventory = _silver_inventory("orders_rejected")
    definitions = SimpleNamespace(assets=[_domain_job({"orders_rejected": ("crm", "orders_rejected")})])

    project_code_check._verify_silver_assets(inventory, definitions)


def test_gold_assets_of_a_product_named_like_its_domain_are_not_silver() -> None:
    gold = SimpleNamespace(
        op=SimpleNamespace(tags={"dagster/compute_kind": "dbt"}),
        specs=[SimpleNamespace(key=_key(("sales", "revenue")), metadata={})],
        asset_deps={},
    )
    definitions = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders")}), gold])

    project_code_check._verify_silver_assets(_silver_inventory(), definitions)


def _schedule_library(
    *,
    stages: tuple[str, ...] = ("prod",),
    job_name: str = "order_revenue_pipeline",
    cron: str = "0 3 * * *",
    status: str = "STOPPED",
    count: int = 1,
) -> SimpleNamespace:
    schedule = SimpleNamespace(
        name="order_revenue_daily",
        job_name=job_name,
        cron_schedule=cron,
        default_status=SimpleNamespace(value=status),
    )
    return SimpleNamespace(
        ProductDefinitionSpec=lambda **fields: SimpleNamespace(**fields),
        build_stage_schedules=lambda spec, *, stage: [schedule] * count if stage in stages else [],
    )


def _inventory() -> SimpleNamespace:
    product = SimpleNamespace(id="order_revenue", domain_name="sales", job_name="order_revenue_pipeline")
    return SimpleNamespace(products=(product,))


def test_stage_schedules_accept_a_stopped_daily_scaffold_in_prod_only() -> None:
    project_code_check._verify_stage_schedules(_inventory(), _schedule_library())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stages": ("dev", "prod")}, "must define no schedule"),
        ({"stages": ("", "prod")}, "must define no schedule"),
        ({"count": 2}, "exactly one daily schedule"),
        ({"job_name": "hand_written_job"}, "not the descriptor job"),
        ({"status": "RUNNING"}, "must not start a schedule"),
        ({"cron": "*/5 * * * *"}, "must run once a day"),
    ],
)
def test_stage_schedules_reject_anything_a_promotion_could_start(overrides: dict, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        project_code_check._verify_stage_schedules(_inventory(), _schedule_library(**overrides))


def test_environment_exports_values_to_the_reader_and_restores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES__X__KEPT", "before")
    monkeypatch.delenv("SOURCES__X__NEW", raising=False)
    snapshot = dict(os.environ)

    with project_code_check._environment({"SOURCES__X__KEPT": "inside", "SOURCES__X__NEW": "inside"}):
        assert os.environ["SOURCES__X__KEPT"] == os.environ["SOURCES__X__NEW"] == "inside"

    assert dict(os.environ) == snapshot


_PUBLIC = {
    "openlakeforge_runtime.product_dagster": {"ProductDefinitionSpec", "build_product_definitions"},
    "openlakeforge_runtime.bronze_csv": {"BronzeLoadResult"},
}


def _project(tmp_path: Path, source: str) -> Path:
    module = tmp_path / "lakehouse_code" / "pipelines" / "dagster" / "orders.py"
    module.parent.mkdir(parents=True)
    module.write_text(source)
    return tmp_path


def test_imports_of_the_public_runtime_api_are_not_reported(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions\n"
        "from openlakeforge_runtime.bronze_csv import BronzeLoadResult\n"
        "import dagster\n",
    )

    assert project_code_check.private_runtime_imports(root, _PUBLIC) == []


def test_private_runtime_modules_and_names_are_reported_with_their_location(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "from openlakeforge_runtime.product_dagster import build_product_definitions, _floe_profile\n"
        "from openlakeforge_runtime.s3_artifacts import upload_file_to_base_uri\n"
        "from openlakeforge_runtime import floe_profile\n"
        "import openlakeforge_runtime.gold_maintenance\n",
    )

    assert project_code_check.private_runtime_imports(root, _PUBLIC) == [
        "lakehouse_code/pipelines/dagster/orders.py:1: openlakeforge_runtime.product_dagster._floe_profile "
        "is not in the module's public API",
        "lakehouse_code/pipelines/dagster/orders.py:2: openlakeforge_runtime.s3_artifacts "
        "is not a public runtime module",
        "lakehouse_code/pipelines/dagster/orders.py:3: openlakeforge_runtime.floe_profile "
        "is not a public runtime module",
        "lakehouse_code/pipelines/dagster/orders.py:4: openlakeforge_runtime.gold_maintenance "
        "is not a public runtime module",
    ]
