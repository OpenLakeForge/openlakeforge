from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from openlakeforge_core import (
    LakehouseDescriptorError,
    load_governance_descriptor,
    parse_glossary,
    read_column_term_links,
)
from openlakeforge_core.inventory import load_lakehouse_inventory

from olf.common import contracts_check
from olf.common.contracts_check._glossary import _check_glossary_conformance

ROOT = Path(__file__).resolve().parents[4]

GLOSSARY = """\
apiVersion: openlakeforge.io/v1alpha1
kind: Glossary
name: sales
displayName: Sales Vocabulary
description: Shared commercial definitions
owner: {type: team, ref: sales_data}
terms:
  - name: NetRevenue
    displayName: Net Revenue
    description: Revenue after discounts.
    synonyms: [Net Sales]
    status: In Review
    relatedTerms: [sales.GrossRevenue]
    references:
      - {name: Finance policy, endpoint: "https://example.internal/finance/revenue"}
  - name: GrossRevenue
    displayName: Gross Revenue
    description: Revenue before discounts.
    parent: Revenue
  - name: Revenue
    displayName: Revenue
    description: Money earned from orders.
"""

GOVERNANCE = """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    glossaryTerms: [sales.Revenue]
    products:
      - id: order_revenue
        displayName: Order Revenue
        glossaryTerms: [sales.NetRevenue]
"""

SCHEMA_YML = """\
version: 2
models:
  - name: fct_order_lines
    columns:
      - name: net_amount
        meta:
          openlakeforge:
            glossaryTerms: [sales.NetRevenue]
  - name: mart_order_revenue_by_day
    columns:
      - name: net_revenue
        meta:
          openlakeforge:
            glossaryTerms: [sales.NetRevenue]
"""


def _repo(tmp_path: Path, *, glossaries: dict[str, str] | None = None, schema: str = SCHEMA_YML) -> Path:
    schema_dir = tmp_path / "docs" / "schema"
    schema_dir.mkdir(parents=True)
    for name in ("governance", "glossary"):
        shutil.copy(ROOT / f"docs/schema/{name}.schema.json", schema_dir / f"{name}.schema.json")
    code = tmp_path / "lakehouse_code"
    (code / "glossaries").mkdir(parents=True)
    for name, text in (glossaries if glossaries is not None else {"sales.yml": GLOSSARY}).items():
        (code / "glossaries" / name).write_text(text, encoding="utf-8")
    (code / "governance.yml").write_text(GOVERNANCE, encoding="utf-8")
    (code / "gold/models/order_revenue").mkdir(parents=True)
    (code / "gold/models/order_revenue/schema.yml").write_text(schema, encoding="utf-8")
    return tmp_path


def _edit_term(text: str, **changes: object) -> str:
    document = yaml.safe_load(text)
    document["terms"][0].update(changes)
    return yaml.safe_dump(document, sort_keys=False)


def test_valid_glossary_links_and_marts_warn(tmp_path: Path) -> None:
    result = _check_glossary_conformance(_repo(tmp_path))

    assert result.ok, result.detail
    assert result.detail == "1 glossaries, 3 terms"
    assert len(result.warnings) == 1
    assert "mart_order_revenue_by_day" in result.warnings[0]
    assert "fact or dimension column" in result.warnings[0]


def test_warning_does_not_fail_the_report(tmp_path: Path) -> None:
    report = contracts_check.ContractsCheckReport([_check_glossary_conformance(_repo(tmp_path))])

    assert report.ok
    assert "[WARN] glossary_conformance:" in report.render()


@pytest.mark.parametrize(
    ("glossary", "expected"),
    [
        (_edit_term(GLOSSARY, description=""), "terms[NetRevenue]: description must be a non-empty string"),
        (_edit_term(GLOSSARY, description="  "), "terms[NetRevenue]: description must be a non-empty string"),
        (_edit_term(GLOSSARY, status="Live"), "terms[NetRevenue]: status must be one of"),
        (_edit_term(GLOSSARY, relatedTerms=["sales.Missing"]), "terms[NetRevenue].relatedTerms[0]: 'sales.Missing'"),
        (_edit_term(GLOSSARY, relatedTerms=["sales.NetRevenue"]), "cannot relate to itself"),
        (_edit_term(GLOSSARY, parent="Nowhere"), "terms[NetRevenue].parent: 'Nowhere' is not a term of 'sales'"),
        (_edit_term(GLOSSARY, name="netrevenue").replace("GrossRevenue", "NetRevenue"), "collides with term"),
        (_edit_term(GLOSSARY, name="Net.Revenue"), "name must match"),
        (_edit_term(GLOSSARY, surprise=1), "must not contain ['surprise']"),
        (_edit_term(GLOSSARY, references=None), "references must be an array"),
        (_edit_term(GLOSSARY, references={}), "references must be an array"),
        (
            _edit_term(GLOSSARY, references=[{"name": "Policy", "endpoint": "http://example.internal/p"}]),
            "endpoint must be an https URL",
        ),
        (
            _edit_term(GLOSSARY, references=[{"name": "Policy", "endpoint": "https://example.com/?next=http://x"}]),
            "endpoint must be an https URL",
        ),
    ],
)
def test_invalid_terms_fail_with_the_term_path(tmp_path: Path, glossary: str, expected: str) -> None:
    result = _check_glossary_conformance(_repo(tmp_path, glossaries={"sales.yml": glossary}))

    assert not result.ok
    assert "lakehouse_code/glossaries/sales.yml" in result.detail
    assert expected in result.detail


def test_parent_cycle_is_reported_once(tmp_path: Path) -> None:
    document = yaml.safe_load(GLOSSARY)
    document["terms"][2]["parent"] = "GrossRevenue"  # Revenue -> GrossRevenue -> Revenue

    result = _check_glossary_conformance(
        _repo(tmp_path, glossaries={"sales.yml": yaml.safe_dump(document, sort_keys=False)})
    )

    assert not result.ok
    assert result.detail.count("cycle") == 1


def test_a_glossary_declared_twice_fails(tmp_path: Path) -> None:
    result = _check_glossary_conformance(_repo(tmp_path, glossaries={"sales.yml": GLOSSARY, "copy.yml": GLOSSARY}))

    assert not result.ok
    assert "lakehouse_code/glossaries/sales.yml" in result.detail
    assert "glossary 'sales' is already declared in lakehouse_code/glossaries/copy.yml" in result.detail


def test_overlapping_display_names_in_two_glossaries_stay_distinct(tmp_path: Path) -> None:
    other = yaml.safe_load(GLOSSARY)
    other["name"] = "finance"
    other["terms"][0]["relatedTerms"] = ["finance.GrossRevenue"]

    result = _check_glossary_conformance(
        _repo(tmp_path, glossaries={"sales.yml": GLOSSARY, "finance.yml": yaml.safe_dump(other, sort_keys=False)})
    )

    assert result.ok, result.detail


def test_dangling_references_name_their_location(tmp_path: Path) -> None:
    schema = SCHEMA_YML.replace("[sales.NetRevenue]", "[sales.Nope]", 1)
    repo = _repo(tmp_path, schema=schema)
    governance = repo / "lakehouse_code/governance.yml"
    governance.write_text(GOVERNANCE.replace("sales.Revenue", "sales.Gone").replace("sales.NetRevenue", "ops.Gone"))

    result = _check_glossary_conformance(repo)

    assert not result.ok
    assert "governance.yml: domains[sales].glossaryTerms: 'sales.Gone'" in result.detail
    assert "domains[sales].products[order_revenue].glossaryTerms: 'ops.Gone'" in result.detail
    assert "gold/models/order_revenue/schema.yml: models[fct_order_lines].columns[net_amount]" in result.detail


def test_references_without_any_glossary_fail(tmp_path: Path) -> None:
    result = _check_glossary_conformance(_repo(tmp_path, glossaries={}))

    assert not result.ok
    assert "'sales.NetRevenue' is not a known term" in result.detail


def test_a_malformed_dbt_reference_fails(tmp_path: Path) -> None:
    result = _check_glossary_conformance(
        _repo(tmp_path, schema=SCHEMA_YML.replace("[sales.NetRevenue]", "NetRevenue", 1))
    )

    assert not result.ok
    assert "glossaryTerms must be a non-empty array" in result.detail


def test_governance_rejects_a_malformed_term_reference(tmp_path: Path) -> None:
    path = tmp_path / "governance.yml"
    path.write_text(GOVERNANCE.replace("[sales.Revenue]", "[Revenue]"), encoding="utf-8")

    with pytest.raises(LakehouseDescriptorError, match=r"glossaryTerms\[0\] must look like"):
        load_governance_descriptor(path)


def test_parse_glossary_requires_the_envelope() -> None:
    with pytest.raises(LakehouseDescriptorError, match="unsupported apiVersion"):
        parse_glossary({"apiVersion": "openlakeforge.io/v1", "kind": "Glossary"})


def test_the_repository_inventory_exposes_glossaries_and_resolved_terms() -> None:
    inventory = load_lakehouse_inventory(ROOT)

    net = inventory.terms["sales.NetRevenue"]
    assert (net.display_name, net.status, net.synonyms) == ("Net Revenue", "Approved", ("Net Sales",))
    assert net.related_terms == ("sales.GrossRevenue", "sales.Discount")
    product = next(p for p in inventory.products if p.id == "order_revenue")
    assert [t.fqn for t in inventory.terms_of(product.glossary_terms)] == ["sales.NetRevenue", "sales.GrossRevenue"]


def test_inventory_rejects_a_dangling_reference_and_exposes_column_terms(tmp_path: Path) -> None:
    code = tmp_path / "lakehouse_code"
    shutil.copytree(ROOT / "lakehouse_code", code, ignore=shutil.ignore_patterns("__pycache__"))
    (code / "gold/models/order_revenue/extra.yml").write_text(
        SCHEMA_YML.replace("fct_order_lines", "fct_demo").replace("mart_order_revenue_by_day", "dim_demo"),
        encoding="utf-8",
    )

    inventory = load_lakehouse_inventory(tmp_path)

    assert {k: [t.fqn for t in v] for k, v in inventory.column_terms("order_revenue").items()} == {
        ("fct_demo", "net_amount"): ["sales.NetRevenue"],
        ("dim_demo", "net_revenue"): ["sales.NetRevenue"],
    }
    (code / "gold/models/order_revenue/extra.yml").write_text(
        SCHEMA_YML.replace("sales.NetRevenue", "sales.Typo"), encoding="utf-8"
    )
    with pytest.raises(LakehouseDescriptorError, match="'sales.Typo' is not a known term"):
        load_lakehouse_inventory(tmp_path)


def test_malformed_yaml_is_reported_with_its_file_and_keeps_other_diagnostics(tmp_path: Path) -> None:
    repo = _repo(tmp_path, glossaries={"sales.yml": GLOSSARY, "broken.yml": "terms: ["}, schema="models: [")

    result = _check_glossary_conformance(repo)

    assert not result.ok
    assert "lakehouse_code/glossaries/broken.yml: invalid YAML" in result.detail
    assert "order_revenue/schema.yml: invalid YAML" in result.detail


def test_the_sales_core_columns_carry_their_terms() -> None:
    inventory = load_lakehouse_inventory(ROOT)

    terms = {key: [t.fqn for t in value] for key, value in inventory.column_terms("sales_core").items()}
    assert terms == {
        ("fct_order_lines", "gross_amount"): ["sales.GrossRevenue"],
        ("fct_order_lines", "discount_amount"): ["sales.Discount"],
        ("fct_order_lines", "net_amount"): ["sales.NetRevenue"],
    }


@pytest.mark.parametrize("columns", ["7", "{a: 1}"])
def test_a_non_array_columns_value_is_a_diagnostic_not_a_crash(tmp_path: Path, columns: str) -> None:
    schema = f"models:\n  - name: fct_demo\n    columns: {columns}\n"

    result = _check_glossary_conformance(_repo(tmp_path, schema=schema))

    assert not result.ok
    assert "models[fct_demo].columns must be an array" in result.detail


def test_column_links_in_nested_model_folders_belong_to_the_top_level_product(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    nested = repo / "lakehouse_code/gold/models/order_revenue/facts"
    nested.mkdir()
    (nested / "schema.yml").write_text(SCHEMA_YML.replace("sales.NetRevenue", "sales.Typo"), encoding="utf-8")

    links = read_column_term_links(repo / "lakehouse_code/gold/models")
    result = _check_glossary_conformance(repo)

    assert {(link.product, link.model) for link in links} >= {("order_revenue", "fct_order_lines")}
    assert not result.ok
    assert "order_revenue/facts/schema.yml" in result.detail
    assert "'sales.Typo' is not a known term" in result.detail
