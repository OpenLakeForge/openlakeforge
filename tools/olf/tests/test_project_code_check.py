from __future__ import annotations

from dataclasses import dataclass
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


def test_sequential_floe_orchestration_requires_serial_job_config() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")
    job = SimpleNamespace(run_config={"execution": {"config": {"multiprocess": {"max_concurrent": 1}}}})

    project_code_check._verify_sequential_floe_orchestration(product, job)

    job.run_config["execution"]["config"]["multiprocess"]["max_concurrent"] = 2
    with pytest.raises(RuntimeError, match="did not inherit Floe orchestration concurrency"):
        project_code_check._verify_sequential_floe_orchestration(product, job)


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
