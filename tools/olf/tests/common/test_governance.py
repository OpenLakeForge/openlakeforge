from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml
from openlakeforge_core import (
    LakehouseDescriptorError,
    load_governance_descriptor,
    load_governance_lakehouse_document,
)

from olf.common.contracts_check._governance import _check_governance_conformance

ROOT = Path(__file__).parents[4]
SCHEMAS = ROOT / "docs/schema"
GOVERNANCE = """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    products:
      - {id: revenue, displayName: Revenue}
"""


def _project(tmp_path: Path, governance: str = GOVERNANCE) -> Path:
    code = tmp_path / "lakehouse_code"
    (code / "silver/sales").mkdir(parents=True)
    (code / "gold/revenue/dbt/target").mkdir(parents=True)
    (code / "governance.yml").write_text(governance, encoding="utf-8")
    return tmp_path


def _manifest(project: Path, *models: tuple[str, str, str]) -> None:
    nodes = {
        f"model.p.{name}": {"name": name, "resource_type": "model", "schema": schema, "config": {"materialized": mat}}
        for name, schema, mat in models
    }
    (project / "lakehouse_code/gold/revenue/dbt/target/manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )


def _detail(project: Path) -> str:
    result = _check_governance_conformance(project, schema_root=SCHEMAS)
    return "" if result.ok else result.detail


def test_matching_project_passes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _manifest(project, ("mart", "revenue_gold", "table"), ("stage", "anywhere", "ephemeral"))
    assert _detail(project) == ""


def test_check_is_skipped_without_governance(tmp_path: Path) -> None:
    assert _check_governance_conformance(tmp_path, schema_root=SCHEMAS).ok


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: (c / "silver/hr").mkdir(), "domain 'hr' has a directory but is not in"),
        (lambda c: (c / "silver/sales").rmdir(), "domain 'sales' has no lakehouse_code directory"),
        (lambda c: (c / "gold/stray").mkdir(), "product 'stray' has a directory but is not in"),
        (lambda c: (c / "gold/revenue/dbt/target").rmdir() or (c / "gold/revenue/dbt").rmdir()
         or (c / "gold/revenue").rmdir(), "product 'revenue' has no lakehouse_code directory"),
    ],
)
def test_governance_and_directories_must_agree(tmp_path: Path, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    project = _project(tmp_path)
    mutate(project / "lakehouse_code")
    assert message in _detail(project)


def test_model_outside_the_product_gold_schema_is_rejected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _manifest(project, ("mart", "revenue_gold", "table"), ("leaky", "other_gold", "table"))
    assert "dbt model 'leaky' of product 'revenue' lands in 'other_gold'" in _detail(project)


_NESTED = "      - {id: revenue, displayName: Revenue}\n"
_BAD = [
    (GOVERNANCE.replace("v1beta2", "v1alpha3"), "unsupported apiVersion"),
    (GOVERNANCE.replace("v1beta2", "v1beta1"), "run `olf project migrate`"),
    (GOVERNANCE + "products:\n  - {id: other, displayName: Other}\n", "root `products` is not part of"),
    (
        GOVERNANCE.replace("{id: revenue,", "{id: revenue, domain: sales,"),
        "domains[sales].products[revenue]: must not contain ['domain']",
    ),
    (
        GOVERNANCE + "  - name: hr\n    displayName: Hr\n    products:\n" + _NESTED,
        "already declared under domain 'sales'",
    ),
    (GOVERNANCE + _NESTED, "duplicate id 'revenue' in domains[sales].products[revenue]"),
    (GOVERNANCE + "  - {name: sales, displayName: Again}\n", "duplicate name 'sales'"),
    (
        GOVERNANCE.replace("    products:\n      - {id: revenue, displayName: Revenue}\n", "    products: nope\n"),
        "domains[sales].products must be an array",
    ),
    (GOVERNANCE.replace("id: revenue", "id: Revenue"), "id must match"),
    (GOVERNANCE.replace(", displayName: Revenue", ""), "displayName must be a non-empty string"),
    (GOVERNANCE.replace("displayName: Revenue}", "displayName: Revenue, tables: []}"), "must not contain ['tables']"),
]


@pytest.mark.parametrize(("text", "message"), _BAD)
def test_invalid_governance_is_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "governance.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LakehouseDescriptorError, match=re.escape(message)):
        load_governance_descriptor(path)
    assert message in _detail(_project(tmp_path / "p", text))


def test_valid_governance_passes_the_json_schema() -> None:
    schema = json.loads((SCHEMAS / "governance.schema.json").read_text(encoding="utf-8"))
    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(yaml.safe_load(GOVERNANCE)))


def test_domains_may_have_no_products_in_the_schema_and_loader(tmp_path: Path) -> None:
    text = (
        GOVERNANCE
        + "  - name: hr\n    displayName: Hr\n    status: planned\n    products: []\n  - {name: it, displayName: IT}\n"
    )
    path = tmp_path / "governance.yml"
    path.write_text(text, encoding="utf-8")
    assert [d["name"] for d in load_governance_descriptor(path)["domains"]] == ["sales", "hr", "it"]
    project = _project(tmp_path / "p", text)
    for name in ("hr", "it"):
        (project / "lakehouse_code/silver" / name).mkdir()
    assert _detail(project) == ""


def test_lakehouse_document_keeps_domain_ownership_without_a_domain_field(tmp_path: Path) -> None:
    code = tmp_path / "lakehouse_code"
    code.mkdir()
    (code / "governance.yml").write_text(
        GOVERNANCE + "      - {id: churn, displayName: Churn}\n  - {name: hr, displayName: HR}\n", encoding="utf-8"
    )
    document = load_governance_lakehouse_document(code / "governance.yml")
    assert {d["name"]: [p["id"] for p in d["products"]] for d in document["domains"]} == {
        "sales": ["revenue", "churn"],
        "hr": [],
    }
    assert all("domain" not in p for d in document["domains"] for p in d["products"])
