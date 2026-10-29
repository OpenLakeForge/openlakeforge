from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml
from openlakeforge_domain import (
    LakehouseDescriptorError,
    load_governance_descriptor,
    load_governance_inventory,
    load_lakehouse_inventory,
)

ROOT = Path(__file__).parents[3]
GOVERNANCE = """\
apiVersion: openlakeforge.io/v1beta1
kind: Governance
domains:
  - name: sales
    displayName: Sales
    products:
      - id: revenue
        displayName: Revenue
"""


def _project(tmp_path: Path) -> Path:
    """One source, one domain, one product, one dashboard: the smallest valid project."""
    code = tmp_path / "lakehouse_code"
    files = {
        "governance.yml": GOVERNANCE,
        "bronze/crm/source.yaml": (
            "apiVersion: openlakeforge.io/v1alpha3\nkind: Source\nname: crm\ndisplayName: CRM\n"
            "description: d\nstatus: planned\nresources:\n  - name: orders\n"
        ),
        "silver/floe.yml": 'version: "0.8"\nmetadata: {project: demo}\n',
        "silver/sales/contracts/floe/_domain.yml": "name: sales\n",
        "silver/sales/contracts/floe/orders.yml": "name: orders\nsource: {ref: crm, resource: orders}\n",
        "gold/revenue/dbt/dbt_project.yml": "name: revenue\n",
        "gold/revenue/dbt/models/gold/mart_revenue.sql": "select * from {{ source('silver', 'orders') }}\n",
        "pipelines/dagster/revenue.py": "",
        "dashboards/superset/revenue_board/metadata.yaml": "type: assets\n",
        "dashboards/superset/revenue_board/datasets/Trino/mart_revenue.yaml": "schema: revenue_gold\n",
    }
    for relative, text in files.items():
        (code / relative).parent.mkdir(parents=True, exist_ok=True)
        (code / relative).write_text(text, encoding="utf-8")
    return tmp_path


def test_inventory_is_derived_from_code_with_no_table_lists(tmp_path: Path) -> None:
    inventory = load_governance_inventory(_project(tmp_path))

    assert inventory.name == "demo"
    assert [(t.name, t.source, t.resource) for t in inventory.domains[0].silver_tables] == [("orders", "crm", "orders")]
    product = inventory.products[0]
    assert product.silver_inputs == ("orders",)
    assert [t.name for t in product.gold_tables] == ["mart_revenue"]
    assert [(d.name, d.products) for d in inventory.dashboards] == [("revenue_board", ("revenue",))]


def test_formatting_variants_do_not_change_the_derived_inventory(tmp_path: Path) -> None:
    project = _project(tmp_path)
    code = project / "lakehouse_code"
    (code / _MODEL).write_text("select * from {{ source ('silver',\n 'orders') }}\n", encoding="utf-8")
    (code / _DATASET).rename(code / _DATASET.replace(".yaml", ".yml"))

    inventory = load_governance_inventory(project)

    assert inventory.products[0].silver_inputs == ("orders",)
    assert inventory.dashboards[0].products == ("revenue",)


def test_sql_that_merely_mentions_config_words_is_accepted(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sql = "select 'ephemeral' as kind from {{ source('silver', 'orders') }} where enabled = true"
    (project / "lakehouse_code" / _MODEL).write_text(sql, encoding="utf-8")

    assert load_governance_inventory(project).products[0].silver_inputs == ("orders",)


def test_reference_project_derives_what_lakehouse_yaml_declares(tmp_path: Path) -> None:
    code = tmp_path / "lakehouse_code"
    shutil.copytree(ROOT / "lakehouse_code", code, ignore=shutil.ignore_patterns("__pycache__"))
    legacy = yaml.safe_load((code / "lakehouse.yaml").read_text(encoding="utf-8"))
    (code / "lakehouse.yaml").unlink()
    governance = {
        "apiVersion": "openlakeforge.io/v1beta1",
        "kind": "Governance",
        "domains": [
            {
                **{key: domain[key] for key in ("name", "displayName", "description", "status")},
                "products": [
                    {key: product[key] for key in ("id", "displayName", "description", "status")}
                    for product in domain["products"]
                ],
            }
            for domain in legacy["domains"]
        ],
    }
    (code / "governance.yml").write_text(yaml.safe_dump(governance), encoding="utf-8")

    derived = load_governance_inventory(tmp_path)
    declared = load_lakehouse_inventory(ROOT)

    assert [d.name for d in derived.domains] == [d.name for d in declared.domains]
    for got, want in zip(derived.domains, declared.domains, strict=True):
        assert {(t.name, t.source, t.resource) for t in got.silver_tables} == {
            (t.name, t.source, t.resource) for t in want.silver_tables
        }
    assert [(p.id, p.display_name) for p in derived.products] == [(p.id, p.display_name) for p in declared.products]
    for got_product, want_product in zip(derived.products, declared.products, strict=True):
        assert set(got_product.silver_inputs) == set(want_product.silver_inputs)
        assert {t.name for t in got_product.gold_tables} == {t.name for t in want_product.gold_tables}
    assert {(d.name, d.products) for d in derived.dashboards} == {(d.name, d.products) for d in declared.dashboards}
    assert derived.source_names == declared.source_names


def _break(tmp_path: Path, relative: str, text: str | None) -> Path:
    project = _project(tmp_path)
    target = project / "lakehouse_code" / relative
    if text is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return project


_ORDERS = "silver/sales/contracts/floe/orders.yml"
_DOMAIN = "silver/sales/contracts/floe/_domain.yml"
_DUP = "silver/sales/contracts/floe/dup.yml"
_MODEL = "gold/revenue/dbt/models/gold/mart_revenue.sql"
_DATASET = "dashboards/superset/revenue_board/datasets/Trino/mart_revenue.yaml"


@pytest.mark.parametrize(
    ("relative", "text", "message"),
    [
        ("silver/sales/contracts/floe/_domain.yml", "name: marketing\n", "to match governance"),
        ("silver/sales/contracts/floe/_domain.yml", None, "no Floe domain contract"),
        (_ORDERS, "name: orders\nsource: {ref: erp, resource: x}\n", "unknown source"),
        (_ORDERS, "name: orders\nsource: {ref: crm, resource: x}\n", "unknown resource"),
        (_DUP, "name: orders\nsource: {ref: crm, resource: orders}\n", "duplicate Silver"),
        (_ORDERS, "name: orders\n", "needs a name and a source"),
        ("gold/revenue/dbt/dbt_project.yml", "name: other\n", "to match governance"),
        ("gold/revenue/dbt/dbt_project.yml", None, "no dbt project"),
        (_MODEL, None, "no dbt models"),
        (_MODEL, "select 1\n", "reads no Silver table"),
        (_MODEL, "{{ config(alias='daily') }} select 1 from {{ source('silver', 'orders') }}", "not supported"),
        (_MODEL, "{{ config(materialized='ephemeral') }} select 1", "not supported"),
        (_MODEL, "{{ config(enabled=false) }} select 1", "not supported"),
        ("gold/revenue/dbt/dbt_project.yml", "name: revenue\nmodel-paths: [sql]\n", "model-paths"),
        ("gold/revenue/dbt/dbt_project.yml", "name: revenue\nmodels: {revenue: {+schema: elsewhere}}\n", "schema"),
        ("gold/revenue/dbt/models/m.yml", "models:\n- name: mart_revenue\n  config: {schema: elsewhere}\n", "schema"),
        ("gold/revenue/dbt/models/s.yml", "sources:\n- name: silver\n  schema: hr_silver\n", "must read the schema"),
        ("gold/revenue/dbt/models/s.yml", "sources:\n- name: silver\n  schema: sales_silver\n  tables:\n"
         "  - {name: orders, identifier: archived}\n", "must not remap"),
        (_DOMAIN, "name: sales\ndefaults: {sink: {accepted: {iceberg: {namespace: hr_silver}}}}\n", "claims a schema"),
        (_ORDERS, "name: orders\nsource: {ref: crm, resource: orders}\nsink: {accepted: {iceberg: {namespace: x}}}\n",
         "claims a schema"),
        ("silver/hr/__init__.py", "", "Silver domain ['hr'] is not in governance.yml"),
        ("gold/stray/__init__.py", "", "Gold product ['stray'] is not in governance.yml"),
        (_MODEL, "{{ config(schema='other') }} select 1", "not supported"),
        ("gold/revenue/dbt/dbt_project.yml", "name: revenue\nmodels: {+materialized: ephemeral}\n", "ephemeral"),
        (_MODEL, "select * from {{ source('silver', 'ghost') }}", "unknown domain Silver"),
        (_MODEL, "select * from {{ source('raw', 'orders') }}", "source('silver'"),
        ("gold/revenue/dbt/models/other/mart_revenue.sql", "select 1", "duplicate dbt model"),
        ("pipelines/dagster/revenue.py", None, "no Dagster module"),
        ("pipelines/dagster/stray.py", "", "Dagster module ['stray'] is not in governance.yml"),
        ("gold/stray/dbt/dbt_project.yml", "name: stray\n", "Gold product ['stray'] is not in governance.yml"),
        ("silver/hr/contracts/floe/_domain.yml", "name: hr\n", "Silver domain ['hr'] is not in governance.yml"),
        (_DATASET, "schema: gone_gold\n", "no governed product"),
        ("governance.yml", None, "no governance descriptor"),
    ],
)
def test_contradictory_project_contracts_are_rejected(
    tmp_path: Path, relative: str, text: str | None, message: str
) -> None:
    with pytest.raises(LakehouseDescriptorError, match=re.escape(message)):
        load_governance_inventory(_break(tmp_path, relative, text))


_BAD_GOVERNANCE = [
    (GOVERNANCE.replace("v1beta1", "v1alpha3"), "unsupported apiVersion"),
    (GOVERNANCE.replace("Governance", "Lakehouse"), "kind must be"),
    (GOVERNANCE + "dashboards: []\n", "'dashboards'\\] are derived from project code"),
    (GOVERNANCE.replace("    products:", "    silver_tables: {}\n    products:"), "'silver_tables'\\] are derived"),
    (GOVERNANCE + "        gold_tables: {}\n", "'gold_tables'\\] are derived"),
    (GOVERNANCE + "        silver_inputs: [orders]\n", "'silver_inputs'\\] are derived"),
    (GOVERNANCE + "      - id: revenue\n        displayName: Again\n", "globally unique"),
    (GOVERNANCE + "  - name: sales\n    displayName: Again\n    products: []\n", "duplicate domain"),
    (GOVERNANCE.replace("id: revenue", "id: Revenue"), "must match"),
    (GOVERNANCE.replace("        displayName: Revenue\n", ""), "missing required field 'displayName'"),
    (GOVERNANCE.replace("      - id: revenue\n        displayName: Revenue\n", "").replace("products:", "products: []"),
     "at least one product"),
]


@pytest.mark.parametrize(("text", "message"), _BAD_GOVERNANCE)
def test_invalid_governance_is_rejected_by_model_and_schema(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "governance.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LakehouseDescriptorError, match=message):
        load_governance_descriptor(path)
    if "globally unique" in message or "duplicate domain" in message:
        return  # cross-entry uniqueness is the model's rule; JSON Schema cannot express it
    schema = json.loads((ROOT / "docs/schema/governance.schema.json").read_text(encoding="utf-8"))
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(yaml.safe_load(text)))


def test_valid_governance_passes_model_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "governance.yml"
    path.write_text(GOVERNANCE, encoding="utf-8")
    document = load_governance_descriptor(path)
    schema = json.loads((ROOT / "docs/schema/governance.schema.json").read_text(encoding="utf-8"))
    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(document))


def test_contracts_check_skips_without_governance_and_reports_code_mismatches(tmp_path: Path) -> None:
    from olf.contracts_check._descriptors import _check_governance_conformance

    schemas = ROOT / "docs/schema"
    assert _check_governance_conformance(tmp_path, schema_root=schemas).ok

    project = _project(tmp_path)
    assert _check_governance_conformance(project, schema_root=schemas).ok

    (project / "lakehouse_code/gold/stray").mkdir()
    result = _check_governance_conformance(project, schema_root=schemas)
    assert not result.ok
    assert "Gold product ['stray'] is not in governance.yml" in result.detail
