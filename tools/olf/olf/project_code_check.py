"""Runtime invariants for descriptor-driven Dagster project definitions.

This module deliberately runs in the project-code dependency environment created
by ``olf check project-code``.  It is kept as Python so it remains testable and
does not depend on a checkout script or shell interpolation.
"""

from __future__ import annotations

import sys
from collections import Counter
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from openlakeforge_domain import LakehouseInventory, Product


# Dagster, Floe and the project's `libs` package are untyped or not importable
# from the CLI package, so these protocols name the surface this check relies on.
class _Job(Protocol):
    run_config: Mapping[str, Any]


class _Schedule(Protocol):
    name: str
    job_name: str
    cron_schedule: str | Sequence[str]
    default_status: object


class _ProductDagsterLibrary(Protocol):
    """The `libs.product_dagster` members this check calls or patches."""

    ProductDefinitionSpec: Callable[..., object]

    def build_stage_schedules(self, spec: object, *, stage: str | None = None) -> Sequence[_Schedule]: ...


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def _asset_keys(definitions: object) -> list[tuple[str, ...]]:
    return [
        tuple(key.path)
        for asset_def in getattr(definitions, "assets", ())
        if hasattr(asset_def, "keys")
        for key in asset_def.keys
    ]


def _product_inputs(product: Product) -> tuple[str, ...]:
    module = import_module(f"lakehouse_code.pipelines.dagster.{product.id}")
    name = f"{product.id.upper()}_SILVER_INPUTS"
    values = getattr(module, name, getattr(module, f"{product.id.upper()}_ENTITIES", None))
    if values is None:
        _fail(f"{module.__name__} does not define {name}")
    return tuple(values)


def validate(root: Path) -> None:
    """Validate product selection, Floe-derived Silver assets, and their Bronze edges."""
    from dagster import Definitions
    from dagster._core.workspace.autodiscovery import loadable_targets_from_python_module
    from floe_dagster.definitions import build_runner_from_env
    from floe_dagster.runner import InProcessRunner
    from openlakeforge_domain import load_lakehouse_inventory

    # Imported by name so mypy does not follow into the user project's tree.
    merged_defs = import_module("lakehouse_code.definitions").defs
    product_dagster_lib = cast(_ProductDagsterLibrary, import_module("libs.product_dagster"))

    if not isinstance(build_runner_from_env(), InProcessRunner):
        _fail("Floe must run in-process in the Dagster run pod; is floe-python installed?")
    inventory = load_lakehouse_inventory(root / "lakehouse_code")
    products = tuple(inventory.products)
    if not products:
        _fail("lakehouse inventory has no products")
    product_ids = {product.id for product in products}
    module_names = ["lakehouse_code.definitions", *(f"lakehouse_code.pipelines.dagster.{p}" for p in product_ids)]
    for module_name in module_names:
        targets = loadable_targets_from_python_module(module_name, str(root))
        if len(targets) != 1 or targets[0].attribute != "defs":
            _fail(f"{module_name} must expose exactly one defs target")

    product_defs = {
        product.id: import_module(f"lakehouse_code.pipelines.dagster.{product.id}").defs for product in products
    }
    merged_keys_list = _asset_keys(merged_defs)
    merged_keys = set(merged_keys_list)
    if len(merged_keys_list) != len(merged_keys):
        _fail("duplicate Dagster asset keys found")
    _verify_bronze_subsetability(inventory, merged_defs)
    _verify_silver_assets(inventory, merged_defs)

    canonical_bronze = {(source.name, item.name) for source in inventory.sources for item in source.resources}
    canonical_silver = {(domain.name, item.name) for domain in inventory.domains for item in domain.silver_tables}
    canonical_gold = {(product.id, item.name) for product in products for item in product.gold_tables}
    all_product_keys: set[tuple[str, ...]] = set()

    for product in products:
        inputs = _product_inputs(product)
        expected_inputs = {item.name for item in inventory.resolved_silver_tables(product)}
        if set(inputs) != expected_inputs:
            _fail(f"{product.id} Silver inputs do not match lakehouse descriptor")
        domain = product.domain_name

        definition_keys = set(_asset_keys(product_defs[product.id]))
        all_product_keys.update(definition_keys)
        other_products = product_ids - {product.id}
        if not any(key[0] == product.id for key in definition_keys):
            _fail(f"{product.id} definitions did not load product assets")
        if any(key[0] in other_products for key in definition_keys):
            _fail(f"{product.id} definitions load another product's assets")

        job = merged_defs.resolve_job_def(product.job_name)
        _verify_sequential_floe_orchestration(product, job)
        selected = {tuple(key.path) for key in job.asset_layer.selected_asset_keys}
        expected_bronze = {(item.source, item.name) for item in inventory.bronze_resources_for_product(product)}
        expected_silver = {(domain, item) for item in inputs}
        expected_gold = {(product.id, item.name) for item in product.gold_tables}
        if selected & canonical_bronze != expected_bronze:
            _fail(f"{product.job_name} does not select exactly its Bronze inputs")
        if selected & canonical_silver != expected_silver:
            _fail(f"{product.job_name} does not select exactly its Silver inputs")
        if selected & canonical_gold != expected_gold:
            _fail(f"{product.job_name} does not select exactly its Gold assets")
        if not expected_bronze | expected_silver | expected_gold <= merged_keys:
            _fail(f"{product.id} selected assets are absent from merged definitions")

    if not all_product_keys <= merged_keys:
        _fail("merged definitions do not contain every product asset")
    _verify_shared_assets(inventory, merged_keys_list)
    _verify_stage_schedules(inventory, product_dagster_lib)
    Definitions.validate_loadable(merged_defs)
    merged_defs.get_repository_def().load_all_definitions()


def _verify_bronze_subsetability(inventory: LakehouseInventory, definitions: object) -> None:
    """Require each shared Bronze source multi-asset to support product subsets."""
    source_names = {source.name for source in inventory.sources}
    source_assets = [
        asset_def
        for asset_def in getattr(definitions, "assets", ())
        if any(tuple(key.path)[0] in source_names for key in getattr(asset_def, "keys", ()))
    ]
    if len(source_assets) != len(source_names):
        _fail("each Source must have exactly one executable Bronze multi-asset")
    for source_asset in source_assets:
        if not getattr(source_asset, "can_subset", False):
            _fail("Source Bronze multi-assets must support product-specific subsets")
        output_defs = getattr(getattr(source_asset, "op", None), "output_defs", ())
        required_outputs = [output.name for output in output_defs if output.is_required]
        if required_outputs:
            _fail(
                "subsettable Source Bronze multi-assets must have optional outputs; "
                f"required outputs: {required_outputs}"
            )


def _verify_silver_assets(inventory: LakehouseInventory, definitions: object) -> None:
    """Require the Floe project to define exactly the descriptor's Silver tables, each fed by its own resource.

    The Bronze edge comes from the entity's `source.ref`/`resource` in Floe and
    the table list from `lakehouse.yaml`; the two must agree.
    """
    tables = {
        (domain.name, item.name): (item.source, item.resource)
        for domain in inventory.domains
        for item in domain.silver_tables
    }
    # dagster-floe tags each entity's op with the table it writes; the same
    # op also emits `<table>_rejected`. Identifying Floe assets by that tag,
    # rather than by key shape, keeps a declared `*_rejected` table and a
    # product's Gold assets sharing a domain's name out of the comparison.
    found: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for asset_def in getattr(definitions, "assets", ()):
        tags = getattr(getattr(asset_def, "op", None), "tags", None) or {}
        if tags.get("dagster/compute_kind") != "floe":
            continue
        table = str(tags.get("floe/table", "")).rpartition(".")[2]
        if not table:
            _fail(f"Floe asset {sorted(tuple(key.path) for key in asset_def.keys)} has no floe/table tag")
        dependencies = {tuple(key.path) for key in getattr(asset_def, "dependency_keys", ())}
        for key in getattr(asset_def, "keys", ()):
            path = tuple(key.path)
            if path[-1] == table:
                found[path] = dependencies
    if set(found) != set(tables):
        _fail(f"Floe Silver assets {sorted(found)} do not match the descriptor's Silver tables {sorted(tables)}")
    for key, bronze in tables.items():
        if bronze not in found[key]:
            _fail(f"Silver asset {'/'.join(key)} must depend on its Bronze resource {'/'.join(bronze)}")


def _verify_sequential_floe_orchestration(product: Product, job: _Job) -> None:
    """Require the serialized Dagster run config that Floe's sequential orchestration produces."""
    try:
        max_concurrent = job.run_config["execution"]["config"]["multiprocess"]["max_concurrent"]
    except (AttributeError, KeyError, TypeError):
        _fail(f"{product.job_name} is missing Floe orchestration concurrency")
    if max_concurrent != 1:
        _fail(f"{product.job_name} did not inherit Floe orchestration concurrency")


def _verify_stage_schedules(inventory: LakehouseInventory, library: _ProductDagsterLibrary) -> None:
    """Require the daily scaffold to exist only where a stage asks for it, and never to start itself.

    Every stage runs the same immutable revision, so this is the only thing
    standing between a promotion and a recurring production-shaped workload
    appearing in DEV.
    """
    for product in inventory.products:
        spec = library.ProductDefinitionSpec(
            domain=product.domain_name,
            product=product.id,
            silver_inputs=(),
            bronze_inputs=(),
            gold_assets=(),
        )
        for stage in ("dev", "uat", ""):
            if library.build_stage_schedules(spec, stage=stage):
                _fail(f"stage {stage!r} must define no schedule for {product.job_name}")
        schedules = library.build_stage_schedules(spec, stage="prod")
        if len(schedules) != 1:
            _fail(f"prod must define exactly one daily schedule for {product.job_name}")
        schedule = schedules[0]
        if schedule.job_name != product.job_name:
            _fail(f"{schedule.name} targets {schedule.job_name}, not the descriptor job {product.job_name}")
        status = getattr(schedule.default_status, "value", schedule.default_status)
        if status != "STOPPED":
            _fail(f"{schedule.name} defaults to {status}; deploying a revision must not start a schedule")
        _verify_daily_cron(schedule)


def _verify_daily_cron(schedule: _Schedule) -> None:
    fields = str(schedule.cron_schedule).split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"] or not all(field.isdigit() for field in fields[:2]):
        _fail(f"{schedule.name} must run once a day; got {schedule.cron_schedule!r}")


def _verify_shared_assets(inventory: LakehouseInventory, asset_keys: list[tuple[str, ...]]) -> None:
    counts = Counter(
        (item.source, item.name)
        for product in inventory.products
        for item in inventory.bronze_resources_for_product(product)
    )
    counts.update(
        (item.domain, item.name)
        for product in inventory.products
        for item in inventory.resolved_silver_tables(product)
    )
    for key, count in counts.items():
        if count > 1 and asset_keys.count(key) != 1:
            _fail(f"shared asset {key[0]}/{key[1]} must have exactly one definition")


def main() -> None:
    try:
        validate(Path(sys.argv[1]).resolve())
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print("Merged and product Dagster definitions loaded.")


if __name__ == "__main__":
    main()
