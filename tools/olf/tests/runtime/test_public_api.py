"""The `openlakeforge_runtime` names customer projects may import (docs/reference/runtime-api.md).

A name leaves this list only through a deprecation: it stays exported, and in
this list, for at least one minor release. Adding a name is free.
"""

from importlib import import_module

import pytest

pytest.importorskip("dagster")

import openlakeforge_runtime  # noqa: E402

# Frozen with the v1beta1 promise: do not remove an entry; see the docs page for the process.
_PUBLISHED = {
    "openlakeforge_runtime.bronze_csv": {"BronzeLoadResult", "load_entities_to_bronze", "load_entity_to_bronze"},
    "openlakeforge_runtime.bronze_dlt": {"load_dlt_source"},
    "openlakeforge_runtime.bronze_incremental": {
        "CursorNotReached",
        "keyset_batches",
        "newest_first_pages",
        "sql_engine",
    },
    "openlakeforge_runtime.product_dagster": {
        "ProductDefinitionSpec",
        "ResolvedProductInputs",
        "SourceDefinitionSpec",
        "build_gold_maintenance_definitions",
        "build_maintenance_definitions",
        "build_product_definitions",
        "build_silver_definitions",
        "build_source_definitions",
        "build_stage_schedules",
    },
}


def test_every_published_module_is_still_public() -> None:
    assert set(_PUBLISHED) <= set(openlakeforge_runtime.PUBLIC_MODULES)


@pytest.mark.parametrize("module_name", sorted(_PUBLISHED))
def test_no_published_name_disappears(module_name: str) -> None:
    module = import_module(module_name)

    missing = _PUBLISHED[module_name] - set(module.__all__)

    assert not missing, f"{module_name} dropped public names {sorted(missing)}; deprecate them instead"


@pytest.mark.parametrize("module_name", sorted(_PUBLISHED))
def test_every_exported_name_exists(module_name: str) -> None:
    module = import_module(module_name)

    assert [name for name in module.__all__ if not hasattr(module, name)] == []
