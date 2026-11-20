from types import SimpleNamespace

import pytest

pytest.importorskip("dagster")

from openlakeforge_runtime.code_location import ProductOverrides, product_specs  # noqa: E402
from openlakeforge_runtime.product_dagster import ProductCadence  # noqa: E402

_INVENTORY = SimpleNamespace(
    products=(
        SimpleNamespace(id="order_revenue", domain_name="sales"),
        SimpleNamespace(id="inventory_reliability", domain_name="supply_chain"),
    )
)


def test_every_declared_product_gets_a_spec_from_its_descriptor_without_an_override() -> None:
    specs = product_specs(_INVENTORY)

    assert list(specs) == ["order_revenue", "inventory_reliability"]
    assert (specs["inventory_reliability"].domain, specs["inventory_reliability"].product) == (
        "supply_chain",
        "inventory_reliability",
    )
    assert specs["order_revenue"].cadence is None


def test_an_override_changes_only_its_own_product() -> None:
    cadence = ProductCadence(cron="0 2 * * *", enabled_in_prod=True)

    specs = product_specs(_INVENTORY, {"order_revenue": ProductOverrides(cadence=cadence, gold_assets=("daily",))})

    assert specs["order_revenue"].cadence == cadence
    assert specs["order_revenue"].gold_assets == ("daily",)
    assert specs["inventory_reliability"].cadence is None


def test_an_override_for_an_undeclared_product_is_refused() -> None:
    with pytest.raises(ValueError, match=r"does not declare: \['order_revnue'\]"):
        product_specs(_INVENTORY, {"order_revnue": ProductOverrides()})
