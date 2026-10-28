from __future__ import annotations

from types import SimpleNamespace

import pytest

from olf import project_code_check


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


def test_sequential_floe_orchestration_requires_serial_job_config() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")
    job = SimpleNamespace(run_config={"execution": {"config": {"multiprocess": {"max_concurrent": 1}}}})

    project_code_check._verify_sequential_floe_orchestration(product, job)

    job.run_config["execution"]["config"]["multiprocess"]["max_concurrent"] = 2
    with pytest.raises(RuntimeError, match="did not inherit Floe orchestration concurrency"):
        project_code_check._verify_sequential_floe_orchestration(product, job)


def _asset(keys: tuple[tuple[str, ...], ...], deps: tuple[tuple[str, ...], ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        keys=[SimpleNamespace(path=list(key)) for key in keys],
        dependency_keys=[SimpleNamespace(path=list(dep)) for dep in deps],
    )


def _silver_inventory() -> SimpleNamespace:
    table = SimpleNamespace(name="orders", source="crm", resource="orders")
    return SimpleNamespace(domains=[SimpleNamespace(name="sales", silver_tables=[table])])


def test_silver_assets_must_each_depend_on_their_own_bronze_resource() -> None:
    accepted = (("sales", "orders"), ("sales", "orders_rejected"))
    wired = SimpleNamespace(assets=[_asset(accepted, deps=(("crm", "orders"),))])
    project_code_check._verify_silver_assets(_silver_inventory(), wired)

    shared = SimpleNamespace(assets=[_asset(accepted, deps=(("sources", "openlakeforge", "crm"),))])
    with pytest.raises(RuntimeError, match="must depend on its Bronze resource crm/orders"):
        project_code_check._verify_silver_assets(_silver_inventory(), shared)


def test_silver_assets_must_match_the_descriptor_tables() -> None:
    extra = SimpleNamespace(
        assets=[_asset((("sales", "orders"),), deps=(("crm", "orders"),)), _asset((("sales", "returns"),))]
    )

    with pytest.raises(RuntimeError, match="do not match the descriptor's Silver tables"):
        project_code_check._verify_silver_assets(_silver_inventory(), extra)


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
