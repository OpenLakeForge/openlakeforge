from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml
from openlakeforge_core import LakehouseDescriptorError, load_governance_descriptor
from openlakeforge_core.governance import EntityRef
from openlakeforge_core.inventory import load_lakehouse_inventory

from olf.common.contracts_check._governance import _check_governance_conformance

SCHEMAS = Path(__file__).parents[4] / "docs/schema"

RICH = """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    domainType: Consumer-aligned
    owner: {type: team, ref: sales_data}
    experts: [{type: user, ref: finance_analyst}]
    classification: Internal
    products:
      - id: sales_core
        displayName: Sales Core
        type: DATASET
        tier: Tier1
        lifecycle: PRODUCTION
        classification: Confidential
      - id: order_revenue
        displayName: Order Revenue
        type: REPORTS
        owner: {type: team, ref: sales_analytics}
        reviewers: [{type: user, ref: finance_lead}]
        tier: Tier2
        lifecycle: PRODUCTION
        termsOfUse:
          allowed: [internal_reporting]
          prohibited: [external_redistribution]
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "governance.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _project(tmp_path: Path, text: str) -> Path:
    code = tmp_path / "lakehouse_code"
    (code / "silver/sales").mkdir(parents=True)
    for product in ("sales_core", "order_revenue"):
        (code / "gold/models" / product).mkdir(parents=True)
    (code / "governance.yml").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_domain_with_two_differently_governed_products_validates(tmp_path: Path) -> None:
    path = _write(tmp_path, RICH)
    assert load_governance_descriptor(path)["domains"][0]["domainType"] == "Consumer-aligned"
    schema = json.loads((SCHEMAS / "governance.schema.json").read_text(encoding="utf-8"))
    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(yaml.safe_load(RICH)))


_BAD = [
    ("    classification: Internal\n", "    classification: Secret\n", "domains[sales]: classification must be one of"),
    ("domainType: Consumer-aligned", "domainType: Consumer", "domains[sales]: domainType must be one of"),
    ("tier: Tier1", "tier: Gold", "products[sales_core]: tier must be one of"),
    ("type: DATASET", "type: dataset", "products[sales_core]: type must be one of"),
    (
        "lifecycle: PRODUCTION\n        classification",
        "lifecycle: planned\n        classification",
        "lifecycle must be one of",
    ),
    ("owner: {type: team, ref: sales_data}", "owner: {type: group, ref: sales_data}", "domains[sales]: owner must be"),
    ("owner: {type: team, ref: sales_data}", "owner: sales_data", "domains[sales]: owner must be"),
    ("experts: [{type: user, ref: finance_analyst}]", "experts: []", "domains[sales]: experts must be a non-empty"),
    (
        "reviewers: [{type: user, ref: finance_lead}]",
        "reviewers: [{type: user}]",
        "products[order_revenue]: reviewers[0]",
    ),
    ("allowed: [internal_reporting]", "allowed: internal_reporting", "termsOfUse.allowed must be"),
    ("domainType: Consumer-aligned", "domainType: Consumer-aligned\n    status: planned", "domains have no lifecycle"),
    ("tier: Tier1", "tier: Tier1\n        status: planned", "use `lifecycle`"),
    ("tier: Tier1", "tier: Tier1\n        sla: {}", "must not contain ['sla']"),
]


@pytest.mark.parametrize(("old", "new", "message"), _BAD)
def test_malformed_fields_fail_with_the_path_of_the_entry(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in RICH
    with pytest.raises(LakehouseDescriptorError, match=re.escape(message)):
        load_governance_descriptor(_write(tmp_path, RICH.replace(old, new, 1)))


def test_inventory_exposes_inherited_values_and_keeps_product_only_fields_unset(tmp_path: Path) -> None:
    _project(tmp_path, RICH)
    inventory = load_lakehouse_inventory(tmp_path / "lakehouse_code", allow_incomplete=True)

    domain = inventory.domains[0]
    core, revenue = domain.products
    assert domain.domain_type == "Consumer-aligned"
    assert core.owner == EntityRef("team", "sales_data")
    assert core.experts == (EntityRef("user", "finance_analyst"),)
    assert core.classification == "Confidential"
    assert (core.type, core.tier, core.lifecycle) == ("DATASET", "Tier1", "PRODUCTION")
    assert revenue.owner == EntityRef("team", "sales_analytics")
    assert revenue.classification == "Internal"
    assert revenue.reviewers == (EntityRef("user", "finance_lead"),)
    assert revenue.terms_of_use == {"allowed": ("internal_reporting",), "prohibited": ("external_redistribution",)}


def test_a_domain_does_not_pass_tier_lifecycle_or_type_to_its_products(tmp_path: Path) -> None:
    text = "apiVersion: openlakeforge.io/v1beta2\nkind: Governance\ndomains:\n  - name: sales\n    displayName: Sales\n"
    text += "    products:\n      - {id: sales_core, displayName: Core}\n"
    _project(tmp_path, text)
    inventory = load_lakehouse_inventory(tmp_path / "lakehouse_code", allow_incomplete=True)
    product = inventory.domains[0].products[0]
    assert (product.owner, product.tier, product.lifecycle, product.type, product.classification) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_strict_reports_products_missing_owner_tier_or_lifecycle_but_plain_accepts_them(tmp_path: Path) -> None:
    text = RICH.replace("        tier: Tier2\n", "").replace(
        "        lifecycle: PRODUCTION\n        classification", "        classification"
    )
    project = _project(tmp_path, text)

    assert _check_governance_conformance(project, schema_root=SCHEMAS).ok
    strict = _check_governance_conformance(project, schema_root=SCHEMAS, strict=True)
    assert not strict.ok
    assert strict.detail == (
        "product 'sales_core' is missing lifecycle (--strict); product 'order_revenue' is missing tier (--strict)"
    )


def test_strict_counts_an_owner_inherited_from_the_domain(tmp_path: Path) -> None:
    project = _project(tmp_path, RICH)
    assert _check_governance_conformance(project, schema_root=SCHEMAS, strict=True).ok
    no_owner = RICH.replace("    owner: {type: team, ref: sales_data}\n", "")
    missing = _check_governance_conformance(_project(tmp_path / "x", no_owner), schema_root=SCHEMAS, strict=True)
    assert "product 'sales_core' is missing owner" in missing.detail

