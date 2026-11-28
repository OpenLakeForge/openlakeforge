from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from openlakeforge_core import (
    LakehouseDescriptorError,
    load_governance_descriptor,
    profiling_metrics,
    resolve_profiling,
)
from openlakeforge_core.inventory import load_lakehouse_inventory

from olf.common.contracts_check._governance import _check_governance_conformance

SCHEMAS = Path(__file__).parents[4] / "docs/schema"

GOVERNANCE = """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    classification: Internal
    products:
      - id: sales_core
        displayName: Sales Core
        profiling: {enabled: true}
      - id: order_revenue
        displayName: Order Revenue
"""

ROLES = {
    "sales_core": {"fct_order_lines": "Fact", "dim_account": "Dimension", "mart_summary": "Mart"},
    "order_revenue": {"fct_x": "Fact"},
}


def _project(tmp_path: Path, text: str = GOVERNANCE) -> Path:
    code = tmp_path / "lakehouse_code"
    (code / "silver/sales").mkdir(parents=True)
    for product in ("sales_core", "order_revenue"):
        (code / "gold/models" / product).mkdir(parents=True)
    (code / "governance.yml").write_text(text, encoding="utf-8")
    return tmp_path


def _products(tmp_path: Path, text: str = GOVERNANCE):  # noqa: ANN202
    inventory = load_lakehouse_inventory(_project(tmp_path, text) / "lakehouse_code", allow_incomplete=True)
    return inventory.products


def test_profiling_is_off_unless_a_product_enables_it(tmp_path: Path) -> None:
    core, revenue = _products(tmp_path)
    assert core.profiling_enabled is True
    assert revenue.profiling_enabled is False


def test_only_facts_and_dimensions_of_an_enabled_product_are_targets(tmp_path: Path) -> None:
    targets = resolve_profiling(_products(tmp_path), ROLES)

    assert [(t.product, t.schema, t.tables) for t in targets] == [
        ("sales_core", "sales_core_gold", ("dim_account", "fct_order_lines"))
    ]


def test_internal_products_may_store_values_and_confidential_ones_only_counts() -> None:
    internal = profiling_metrics("Internal")
    assert internal is not None
    assert {"min", "max", "mean", "rowCount", "nullCount", "distinctCount"} <= set(internal)
    assert profiling_metrics("Public") == internal
    confidential = profiling_metrics("Confidential")
    assert confidential is not None
    assert not {"min", "max", "mean"} & set(confidential)
    assert {"rowCount", "nullCount", "distinctCount"} <= set(confidential)


@pytest.mark.parametrize("classification", ["Restricted", None])
def test_restricted_and_unclassified_products_are_never_profiled(tmp_path: Path, classification: str | None) -> None:
    text = GOVERNANCE.replace("    classification: Internal\n", "")
    if classification:
        text = text.replace("        profiling:", f"        classification: {classification}\n        profiling:")
    assert resolve_profiling(_products(tmp_path, text), ROLES) == ()


def test_a_product_with_no_facts_or_dimensions_has_no_target(tmp_path: Path) -> None:
    assert resolve_profiling(_products(tmp_path), {"sales_core": {"mart_summary": "Mart"}}) == ()


@pytest.mark.parametrize("classification", ["Restricted", None])
def test_contracts_report_profiling_on_a_classification_that_forbids_it(
    tmp_path: Path, classification: str | None
) -> None:
    text = GOVERNANCE.replace("    classification: Internal\n", "")
    if classification:
        text = text.replace("        profiling:", f"        classification: {classification}\n        profiling:")
    result = _check_governance_conformance(_project(tmp_path, text), schema_root=SCHEMAS)

    assert not result.ok
    assert "product 'sales_core' enables profiling" in result.detail
    assert "order_revenue" not in result.detail


def test_contracts_accept_profiling_on_an_internal_product(tmp_path: Path) -> None:
    assert _check_governance_conformance(_project(tmp_path), schema_root=SCHEMAS).ok


@pytest.mark.parametrize(
    "block",
    ["profiling: {enabled: yes please}", "profiling: {}", "profiling: true", "profiling: {enabled: true, sample: 10}"],
)
def test_a_malformed_profiling_block_is_rejected_by_loader_and_schema(tmp_path: Path, block: str) -> None:
    text = GOVERNANCE.replace("profiling: {enabled: true}", block)
    path = tmp_path / "governance.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LakehouseDescriptorError, match="profiling"):
        load_governance_descriptor(path)
    schema = json.loads((SCHEMAS / "governance.schema.json").read_text(encoding="utf-8"))
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(yaml.safe_load(text)))


def test_a_domain_cannot_set_profiling(tmp_path: Path) -> None:
    text = GOVERNANCE.replace(
        "    classification: Internal\n", "    classification: Internal\n    profiling: {enabled: true}\n"
    )
    path = tmp_path / "governance.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LakehouseDescriptorError, match="profiling"):
        load_governance_descriptor(path)


def test_a_profile_is_stale_when_absent_or_older_than_the_period_plus_slack():
    from datetime import UTC, datetime, timedelta

    from openlakeforge_core import PROFILING_PERIOD, PROFILING_STALE_SLACK, profile_is_stale

    now = datetime(2026, 11, 28, 12, tzinfo=UTC)

    def at(age: timedelta) -> float:
        return (now - age).timestamp() * 1000

    assert profile_is_stale(None, now=now)
    assert not profile_is_stale(at(PROFILING_PERIOD), now=now)
    assert not profile_is_stale(at(PROFILING_PERIOD + PROFILING_STALE_SLACK), now=now)
    assert profile_is_stale(at(PROFILING_PERIOD + PROFILING_STALE_SLACK + timedelta(minutes=1)), now=now)
