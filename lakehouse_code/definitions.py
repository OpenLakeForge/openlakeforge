"""Aggregate Dagster definitions for the canonical lakehouse code location."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules

import lakehouse_code.pipelines.dagster as dagster_pipelines
from dagster import Definitions
from libs.product_dagster import SourceDefinitionSpec, build_silver_definitions, build_source_definitions
from openlakeforge_domain import LakehouseInventory, load_lakehouse_inventory

_LAKEHOUSE_CODE_ROOT = Path(__file__).resolve().parent


def _product_definitions() -> list[Definitions]:
    """Load Definitions exposed by product pipeline modules in a stable order."""
    product_modules = sorted(
        (
            module
            for module in iter_modules(dagster_pipelines.__path__, "lakehouse_code.pipelines.dagster.")
            if not module.ispkg
        ),
        key=lambda module: module.name,
    )
    return [import_module(module.name).defs for module in product_modules]


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


_inventory = load_lakehouse_inventory(_LAKEHOUSE_CODE_ROOT)
defs = Definitions.merge(*_source_definitions(_inventory), build_silver_definitions(), *_product_definitions())
