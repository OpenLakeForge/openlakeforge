from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module

from dagster import Definitions
from openlakeforge_core import LakehouseInventory, inventory_for

from openlakeforge_runtime.product_dagster import (
    _LAKEHOUSE_CODE_ROOT,
    ProductCadence,
    ProductDefinitionSpec,
    SourceDefinitionSpec,
    build_gold_maintenance_definitions,
    build_maintenance_definitions,
    build_product_definitions,
    build_silver_definitions,
    build_source_definitions,
)

__all__ = ["ProductOverrides", "build_code_location", "product_specs"]


@dataclass(frozen=True)
class ProductOverrides:
    """What a product declares beyond its descriptor entry; every field omitted keeps `ProductDefinitionSpec`'s default.

    The domain and product name come from `governance.yml`, so the code location needs no entry here for a product
    that takes the defaults.
    """

    silver_inputs: tuple[str, ...] | None = None
    bronze_inputs: tuple[tuple[str, str], ...] | None = None
    gold_assets: tuple[str, ...] | None = None
    cadence: ProductCadence | None = None


def product_specs(
    inventory: LakehouseInventory, overrides: Mapping[str, ProductOverrides] | None = None
) -> dict[str, ProductDefinitionSpec]:
    """One spec per product the project declares, in inventory order, with its overrides applied.

    An override for a product the project does not declare is refused: it is almost always a typo, and ignoring it
    would leave the product on its defaults, a cadence included, without a word.
    """
    overrides = overrides or {}
    declared = {product.id for product in inventory.products}
    unknown = sorted(set(overrides) - declared)
    if unknown:
        raise ValueError(
            f"overrides for products governance.yml does not declare: {unknown}; declared: {sorted(declared)}"
        )
    return {
        product.id: ProductDefinitionSpec(
            domain=product.domain_name,
            product=product.id,
            **vars(overrides.get(product.id, ProductOverrides())),
        )
        for product in inventory.products
    }


def build_code_location(overrides: Mapping[str, ProductOverrides] | None = None) -> Definitions:
    """The project's whole Dagster code location, derived from its descriptors.

    Bronze assets come from each source, Silver from the Floe project, and one job (with its PROD schedule) from
    each product of `governance.yml`, plus the maintenance jobs. `overrides` is keyed by product id.
    """
    inventory = inventory_for(_LAKEHOUSE_CODE_ROOT)
    return Definitions.merge(
        *_source_definitions(inventory),
        build_silver_definitions(),
        build_maintenance_definitions(),
        build_gold_maintenance_definitions(),
        *(build_product_definitions(spec) for spec in product_specs(inventory, overrides).values()),
    )


def _source_definitions(inventory: LakehouseInventory) -> list[Definitions]:
    definitions: list[Definitions] = []
    for source in inventory.sources:
        module = import_module(f"lakehouse_code.bronze.{source.name}.dlt.{source.name}")
        loader = getattr(module, f"load_{source.name}_entities_to_bronze")
        definitions.append(
            build_source_definitions(
                SourceDefinitionSpec(
                    source=source.name,
                    resources=tuple(resource.name for resource in source.resources),
                    bronze_loader=loader,
                )
            )
        )
    return definitions
