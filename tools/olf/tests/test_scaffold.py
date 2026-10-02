"""Tests for `olf source|domain|product new` (issue #40 golden-path scaffolding).

Every test seeds a throwaway copy of the real `lakehouse_code` tree plus the
JSON Schemas into `tmp_path`, then drives the scaffold engine or its Typer
commands against that copy -- never against the checked-in repository.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml
from openlakeforge_domain import load_lakehouse_inventory
from typer.testing import CliRunner

from olf import contracts_check
from olf.cli import app
from olf.scaffold._commit import commit_plan
from olf.scaffold._shared import ScaffoldError
from olf.scaffold.domain import plan_domain_new
from olf.scaffold.product import plan_product_new
from olf.scaffold.source import plan_source_new

ROOT = Path(__file__).resolve().parents[3]
runner = CliRunner()


def _seed_repo(tmp_path: Path) -> Path:
    """A throwaway repo root carrying the real lakehouse_code tree and schemas."""
    shutil.copytree(ROOT / "lakehouse_code", tmp_path / "lakehouse_code")
    schema_dir = tmp_path / "docs" / "schema"
    schema_dir.mkdir(parents=True)
    shutil.copy(ROOT / "docs/schema/lakehouse.schema.json", schema_dir / "lakehouse.schema.json")
    shutil.copy(ROOT / "docs/schema/source.schema.json", schema_dir / "source.schema.json")
    return tmp_path


def _tree(repo_root: Path) -> set[str]:
    return {str(p.relative_to(repo_root)) for p in repo_root.rglob("*") if p.is_file()}


def _run(*args: str) -> object:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result


# --------------------------------------------------------------------------
# olf source new
# --------------------------------------------------------------------------


def test_source_new_generates_the_documented_file_tree(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)

    plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, plan)

    bronze = repo_root / "lakehouse_code" / "bronze" / "marketing_platform"
    assert (bronze / "source.yaml").is_file()
    assert (bronze / "dlt" / "__init__.py").is_file()
    assert (bronze / "dlt" / "marketing_platform.py").is_file()
    assert (bronze / "examples" / "campaigns.csv").is_file()
    assert (bronze / "README.md").is_file()

    inventory = load_lakehouse_inventory(repo_root)
    assert "marketing_platform" in inventory.source_names


def test_readme_links_from_generated_bronze_and_silver_dirs_resolve(tmp_path: Path) -> None:
    """The Bronze and Silver READMEs link back to docs/ with a relative
    path; that path must actually resolve from where the README lives."""
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, source_plan)
    domain_plan = plan_domain_new(
        repo_root, domain="marketing", display_name=None, inputs=(("marketing_platform", "campaigns"),)
    )
    commit_plan(repo_root, domain_plan)

    bronze_readme = repo_root / "lakehouse_code" / "bronze" / "marketing_platform" / "README.md"
    domain_readme = repo_root / "lakehouse_code" / "silver" / "marketing" / "README.md"
    bronze_link = re.search(r"\]\((\.\./[^)]+)\)", bronze_readme.read_text(encoding="utf-8")).group(1)
    domain_link = re.search(r"\]\((\.\./[^)]+)\)", domain_readme.read_text(encoding="utf-8")).group(1)

    assert (bronze_readme.parent / bronze_link).resolve().is_relative_to(repo_root.resolve())
    assert (bronze_readme.parent / bronze_link).resolve() == (repo_root / "docs/getting-started/first-data-product.md")
    assert (domain_readme.parent / domain_link).resolve().is_relative_to(repo_root.resolve())
    assert (domain_readme.parent / domain_link).resolve() == (
        repo_root / "docs/adr/0004-medallion-layout-and-catalog-namespaces.md"
    )


def test_source_new_handles_a_flow_style_sources_list(tmp_path: Path) -> None:
    """`sources: [crm, erp]` is valid, schema-accepted YAML the descriptor
    could legitimately use; appending to it must not splice a block-style
    list item directly after the flow list (invalid YAML)."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    text = re.sub(r"sources:\n(  - \w+\n)+", "sources: [crm, erp]\n", text)
    lakehouse_path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["sources"] == ["crm", "erp"]

    plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    assert set(inventory.source_names) == {"crm", "erp", "marketing_platform"}


def test_product_new_handles_a_flow_style_non_empty_products_list(tmp_path: Path) -> None:
    """A domain may represent its existing products as a schema-valid,
    non-empty flow sequence (`products: [{id: ..., ...}]`); appending a new
    product must convert it, not fail to even locate the field."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    text = re.sub(
        r"    products:\n(      - id: order_revenue\n(?:.*\n)*?)(?=  - name: supply_chain)",
        "    products: [{id: order_revenue, displayName: Order Revenue, description: d, "
        "status: active, silver_inputs: [orders], gold_tables: {tables: [{name: mart_order_revenue}]}}, "
        "{id: customer_health, displayName: Customer Health, description: d, status: active, "
        "silver_inputs: [accounts], gold_tables: {tables: [{name: mart_customer_health}]}}]\n",
        text,
    )
    lakehouse_path.write_text(text, encoding="utf-8")
    parsed = yaml.safe_load(text)
    sales_domain = next(d for d in parsed["domains"] if d["name"] == "sales")
    assert [p["id"] for p in sales_domain["products"]] == ["order_revenue", "customer_health"]

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert {p.id for p in sales.products} == {"order_revenue", "customer_health", "order_summary"}


def test_source_new_preserves_a_trailing_comment_after_a_flow_style_list(tmp_path: Path) -> None:
    """Converting a flow-style `sources: [crm, erp]` to block style must not
    delete unrelated content (a comment, a blank line) that sits between the
    flow-list line and the next top-level key."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    text = re.sub(
        r"sources:\n(  - \w+\n)+",
        "sources: [crm, erp]\n# TODO: add the marketing source once it's ready.\n\n",
        text,
    )
    lakehouse_path.write_text(text, encoding="utf-8")

    plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, plan)

    result_text = lakehouse_path.read_text(encoding="utf-8")
    assert "# TODO: add the marketing source once it's ready." in result_text
    inventory = load_lakehouse_inventory(repo_root)
    assert set(inventory.source_names) == {"crm", "erp", "marketing_platform"}


def test_domain_new_handles_a_flow_style_domains_list(tmp_path: Path) -> None:
    """`domains: [{name: sales, ...}]` is valid, schema-accepted YAML;
    appending a new domain must convert it, not splice a block-style item
    after the already-closed flow value."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    domains_block_match = re.search(r"^domains:\n(.*\n)+?(?=^dashboards:)", text, re.MULTILINE)
    parsed_domains = yaml.safe_load(text)["domains"]
    flow_domains = "domains: " + yaml.safe_dump(parsed_domains, default_flow_style=True, width=10**9).strip() + "\n"
    text = text[: domains_block_match.start()] + flow_domains + text[domains_block_match.end() :]
    lakehouse_path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["domains"][0]["name"] == "sales"

    plan = plan_domain_new(repo_root, domain="hr", display_name="HR", inputs=(("crm", "orders"),))
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    assert {d.name for d in inventory.domains} == {"sales", "supply_chain", "hr"}


def test_product_new_locates_an_existing_domain_in_a_flow_style_domains_list(tmp_path: Path) -> None:
    """Adding a product to a domain that already exists must find that
    domain even when the whole `domains:` sequence is flow-style -- the
    same conversion `add_domain()` does before appending a new domain is
    needed by the lookup path too, not just the append path."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    domains_block_match = re.search(r"^domains:\n(.*\n)+?(?=^dashboards:)", text, re.MULTILINE)
    parsed_domains = yaml.safe_load(text)["domains"]
    flow_domains = "domains: " + yaml.safe_dump(parsed_domains, default_flow_style=True, width=10**9).strip() + "\n"
    text = text[: domains_block_match.start()] + flow_domains + text[domains_block_match.end() :]
    lakehouse_path.write_text(text, encoding="utf-8")

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert {p.id for p in sales.products} == {"order_revenue", "customer_health", "order_summary"}


def test_product_new_locates_a_domain_that_is_itself_a_flow_mapping_item(tmp_path: Path) -> None:
    """`domains:` can be block-style while one of its items is a single-line
    flow mapping (`  - {name: sales, ...}`) -- schema-valid, but not covered
    by `_domains_span`'s whole-sequence flow conversion. `_domain_span` must
    parse such an item as a mapping to find its name, then convert it to
    block style so a field can still be located/inserted inside it."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    sales_block_match = re.search(r"^  - name: sales\n(?:.*\n)+?(?=^  - name: supply_chain)", text, re.MULTILINE)
    parsed_domains = yaml.safe_load(text)["domains"]
    sales_domain = next(d for d in parsed_domains if d["name"] == "sales")
    flow_sales = "  - " + yaml.safe_dump(sales_domain, default_flow_style=True, width=10**9).strip() + "\n"
    text = text[: sales_block_match.start()] + flow_sales + text[sales_block_match.end() :]
    lakehouse_path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["domains"][0]["name"] == "sales"

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert {p.id for p in sales.products} == {"order_revenue", "customer_health", "order_summary"}


def test_product_new_reorders_products_before_silver_tables(tmp_path: Path) -> None:
    """Schema doesn't require `silver_tables` before `products` within a
    domain; appending a new product to a domain that reverses this order
    must land inside the products sequence, not past whatever field follows
    it."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    # Swap the sales domain's silver_tables: and products: blocks textually,
    # preserving the file's established block-style formatting exactly --
    # a full yaml.safe_dump round-trip would reformat with PyYAML's own
    # (differently-indented) list style, not what this module is built for.
    match = re.search(
        r"(    silver_tables:\n(?:      .*\n)+?)(    products:\n(?:      .*\n)+?)(?=  - name: supply_chain)",
        text,
    )
    assert match is not None
    text = text[: match.start()] + match.group(2) + match.group(1) + text[match.end() :]
    lakehouse_path.write_text(text, encoding="utf-8")
    reloaded_sales = next(d for d in yaml.safe_load(text)["domains"] if d["name"] == "sales")
    assert list(reloaded_sales.keys()).index("products") < list(reloaded_sales.keys()).index("silver_tables")

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert "order_summary" in {p.id for p in sales.products}
    assert {t.name for t in sales.silver_tables} >= {"orders"}


def test_yaml_dq_escapes_newlines_and_control_characters(tmp_path: Path) -> None:
    """A literal newline in --display-name must not be silently folded into
    a space by YAML's plain-scalar rules; it has to be escaped so the exact
    string round-trips."""
    repo_root = _seed_repo(tmp_path)
    tricky_name = "HR\nOps"

    plan = plan_source_new(repo_root, source="workday", display_name=tricky_name, resources=("employees",))
    commit_plan(repo_root, plan)

    source_doc = yaml.safe_load((repo_root / "lakehouse_code" / "bronze" / "workday" / "source.yaml").read_text())
    assert source_doc["displayName"] == tricky_name


def test_product_new_finds_a_domain_whose_name_field_is_not_first(tmp_path: Path) -> None:
    """A schema-valid domain can order its fields any way; `name` need not
    be first. add_product() (and add_silver_tables()'s --input path) must
    still locate the domain by its list-item marker, not by assuming
    `- name:` opens the entry."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    text = text.replace(
        "  - name: sales\n    displayName: Sales\n",
        "  - displayName: Sales\n    name: sales\n",
        1,
    )
    lakehouse_path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["domains"][0]["name"] == "sales"

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert "order_summary" in {p.id for p in sales.products}


def test_product_new_finds_a_domain_with_a_quoted_or_commented_name(tmp_path: Path) -> None:
    """`name: "sales"` (quoted) and `name: sales  # primary` (inline
    comment) are both schema-valid spellings of the same scalar; the domain
    lookup must parse the value, not match the raw source text."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    text = text.replace('  - name: sales\n', '  - name: "sales"  # primary\n', 1)
    lakehouse_path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["domains"][0]["name"] == "sales"

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert "order_summary" in {p.id for p in sales.products}


def test_product_new_handles_a_trailing_comment_on_products_and_tables_keys(tmp_path: Path) -> None:
    """`tables: # core dims` and `products: # inventory` are schema-valid;
    the locator regexes for those fields must tolerate a trailing inline
    comment the same way the flow-list matcher already does."""
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, source_plan)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    text = text.replace("      tables:\n", "      tables:  # core dims\n", 1)
    text = text.replace("    products:\n", "    products:  # revenue and health\n", 1)
    lakehouse_path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["domains"][0]["name"] == "sales"

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(("marketing_platform", "campaigns"),),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert "order_summary" in {p.id for p in sales.products}
    assert {t.name for t in sales.silver_tables} >= {"orders", "campaigns"}


def test_source_new_rejects_bad_identifier_and_writes_nothing(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    before = _tree(repo_root)
    lakehouse_before = (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text()

    with pytest.raises(ScaffoldError, match=r"must match"):
        plan_source_new(repo_root, source="Bad-Name", display_name=None, resources=("x",))

    assert _tree(repo_root) == before
    assert (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text() == lakehouse_before


@pytest.mark.parametrize("keyword", ["yes", "no", "true", "false", "on", "off", "null"])
def test_source_new_rejects_a_yaml_keyword_identifier_and_writes_nothing(tmp_path: Path, keyword: str) -> None:
    """`on`, `true`, `null`, etc. all match `^[a-z][a-z0-9_]*$`, but YAML
    1.1's implicit resolver (PyYAML's SafeLoader included) reads them back
    as a bool/null rather than a string -- silently changing the type of a
    generated identifier. Reject them up front rather than generating a
    descriptor that fails to round-trip."""
    repo_root = _seed_repo(tmp_path)
    before = _tree(repo_root)
    lakehouse_before = (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text()

    with pytest.raises(ScaffoldError, match=r"YAML boolean/null keyword"):
        plan_source_new(repo_root, source=keyword, display_name=None, resources=("campaigns",))

    assert _tree(repo_root) == before
    assert (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text() == lakehouse_before


def test_source_new_rejects_duplicate_source_and_writes_nothing(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    before = _tree(repo_root)

    with pytest.raises(ScaffoldError, match=r"already exists"):
        plan_source_new(repo_root, source="crm", display_name=None, resources=("x",))

    assert _tree(repo_root) == before


def test_source_new_refuses_to_overwrite_an_existing_target_file(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, plan)
    before = _tree(repo_root)
    lakehouse_before = (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text()

    with pytest.raises(ScaffoldError, match=r"already exists in lakehouse.yaml"):
        plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
        commit_plan(repo_root, plan)

    assert _tree(repo_root) == before
    assert (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text() == lakehouse_before


# --------------------------------------------------------------------------
# olf domain new
# --------------------------------------------------------------------------


def test_domain_new_creates_a_product_less_domain_that_validates_on_its_own(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees",))
    commit_plan(repo_root, source_plan)

    plan = plan_domain_new(repo_root, domain="hr", display_name="HR", inputs=(("workday", "employees"),))
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    hr = next(d for d in inventory.domains if d.name == "hr")
    assert hr.products == ()
    assert hr.silver_namespace == "hr_silver"
    assert {t.name for t in hr.silver_tables} == {"employees"}


def test_domain_new_rejects_a_pre_existing_but_undeclared_domain_directory(tmp_path: Path) -> None:
    """A stray file already sitting under `silver/<domain>/` before that
    domain is declared anywhere would otherwise slip past the exact-file
    check in `check_no_existing_targets` (its name doesn't collide with any
    file the plan writes), then combine badly with what the scaffold adds --
    e.g. two Floe contracts under one domain, which
    scripts/artifacts/floe-manifest.sh rejects. The scaffold must refuse to
    write into a non-empty, not-yet-owned directory at all."""
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees",))
    commit_plan(repo_root, source_plan)

    stray = repo_root / "lakehouse_code" / "silver" / "hr" / "contracts" / "floe" / "old.yml"
    stray.parent.mkdir(parents=True)
    stray.write_text("apiVersion: floe/v1alpha1\n", encoding="utf-8")
    before = _tree(repo_root)

    plan = plan_domain_new(repo_root, domain="hr", display_name="HR", inputs=(("workday", "employees"),))
    with pytest.raises(ScaffoldError, match=r"refusing to scaffold into lakehouse_code/silver/hr"):
        commit_plan(repo_root, plan)

    assert _tree(repo_root) == before


def test_domain_new_rejects_unresolved_source_resource_and_writes_nothing(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    before = _tree(repo_root)

    with pytest.raises(ScaffoldError, match=r"unknown source 'nonexistent'"):
        plan_domain_new(repo_root, domain="hr", display_name=None, inputs=(("nonexistent", "employees"),))

    assert _tree(repo_root) == before

    with pytest.raises(ScaffoldError, match=r"source 'crm' has no resource 'nonexistent_resource'"):
        plan_domain_new(repo_root, domain="hr", display_name=None, inputs=(("crm", "nonexistent_resource"),))

    assert _tree(repo_root) == before


def test_domain_new_rejects_duplicate_domain_and_writes_nothing(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    before = _tree(repo_root)

    with pytest.raises(ScaffoldError, match=r"already exists"):
        plan_domain_new(repo_root, domain="sales", display_name=None, inputs=(("crm", "orders"),))

    assert _tree(repo_root) == before


def test_domain_new_requires_at_least_one_input(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)

    with pytest.raises(ScaffoldError, match=r"at least one --input"):
        plan_domain_new(repo_root, domain="hr", display_name=None, inputs=())


def test_domain_new_rejects_a_dagster_asset_key_collision_with_bronze(tmp_path: Path) -> None:
    """A domain named after an existing source, consuming a table named after
    one of that source's resources, produces a Silver asset key identical to
    the existing Bronze asset key -- Dagster's code location cannot load two
    assets sharing one key. Nothing should be written."""
    repo_root = _seed_repo(tmp_path)
    before = _tree(repo_root)

    with pytest.raises(ScaffoldError, match=r"asset key \('crm', 'orders'\) is claimed by both"):
        plan = plan_domain_new(repo_root, domain="crm", display_name=None, inputs=(("crm", "orders"),))
        commit_plan(repo_root, plan)

    assert _tree(repo_root) == before


# --------------------------------------------------------------------------
# olf product new
# --------------------------------------------------------------------------


def test_product_new_creates_first_product_and_domain_implicitly(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, source_plan)

    plan = plan_product_new(
        repo_root,
        target="marketing/campaign_performance",
        display_name=None,
        silver_inputs=(),
        inputs=(("marketing_platform", "campaigns"),),
        gold_tables=("mart_campaign_performance",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    gold = repo_root / "lakehouse_code" / "gold" / "campaign_performance"
    assert (gold / "dbt" / "dbt_project.yml").is_file()
    assert (gold / "dbt" / "models" / "gold" / "mart_campaign_performance.sql").is_file()
    assert (gold / "dbt" / "models" / "gold" / "schema.yml").is_file()
    assert (gold / "dbt" / "models" / "sources.yml").is_file()
    assert (repo_root / "lakehouse_code" / "pipelines" / "dagster" / "campaign_performance.py").is_file()
    assert (repo_root / "lakehouse_code" / "silver" / "marketing" / "contracts" / "floe" / "marketing.yml").is_file()

    inventory = load_lakehouse_inventory(repo_root)
    product = next(p for p in inventory.products if p.id == "campaign_performance")
    assert product.domain_name == "marketing"
    assert product.gold_namespace == "campaign_performance_gold"


def test_product_new_adds_second_product_to_existing_domain_sharing_silver(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    assert {p.id for p in sales.products} == {"order_revenue", "customer_health", "order_summary"}
    order_summary = next(p for p in sales.products if p.id == "order_summary")
    assert order_summary.silver_namespace == "sales_silver"


def test_one_source_resource_shared_by_two_domains_creates_no_second_ingestion(tmp_path: Path) -> None:
    """Acceptance criterion: a single Source resource referenced by multiple
    downstream domains/products must not generate a duplicate dlt loader or
    duplicate Bronze ownership."""
    repo_root = _seed_repo(tmp_path)
    crm_files_before = _tree(repo_root / "lakehouse_code" / "bronze" / "crm")

    plan = plan_domain_new(repo_root, domain="finance", display_name="Finance", inputs=(("crm", "orders"),))
    commit_plan(repo_root, plan)

    crm_files_after = _tree(repo_root / "lakehouse_code" / "bronze" / "crm")
    assert crm_files_after == crm_files_before

    inventory = load_lakehouse_inventory(repo_root)
    sales = next(d for d in inventory.domains if d.name == "sales")
    finance = next(d for d in inventory.domains if d.name == "finance")
    sales_orders = next(t for t in sales.silver_tables if t.name == "orders")
    finance_orders = next(t for t in finance.silver_tables if t.name == "orders")
    assert (sales_orders.source, sales_orders.resource) == ("crm", "orders")
    assert (finance_orders.source, finance_orders.resource) == ("crm", "orders")


def test_product_new_extends_existing_domain_with_new_input(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees", "absences"))
    commit_plan(repo_root, source_plan)
    domain_plan = plan_domain_new(repo_root, domain="hr", display_name="HR", inputs=(("workday", "employees"),))
    commit_plan(repo_root, domain_plan)

    plan = plan_product_new(
        repo_root,
        target="hr/headcount",
        display_name=None,
        silver_inputs=("employees",),
        inputs=(("workday", "absences"),),
        gold_tables=("mart_headcount", "mart_absence_rate"),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    contract_text = (repo_root / "lakehouse_code" / "silver" / "hr" / "contracts" / "floe" / "hr.yml").read_text()
    assert contract_text.count("incremental_mode") == 2  # employees + absences entities

    inventory = load_lakehouse_inventory(repo_root)
    hr = next(d for d in inventory.domains if d.name == "hr")
    assert {t.name for t in hr.silver_tables} == {"employees", "absences"}
    headcount = next(p for p in hr.products if p.id == "headcount")
    assert set(headcount.silver_inputs) == {"employees", "absences"}


def test_product_new_inserts_a_floe_entity_before_a_trailing_extra_key(tmp_path: Path) -> None:
    """The Floe contract's entities: list is always last in a
    scaffold-generated contract, but a hand-edited one could add another
    top-level field after it. A new --input entity must land inside the
    entities sequence, not get appended past that later field."""
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees", "absences"))
    commit_plan(repo_root, source_plan)
    domain_plan = plan_domain_new(repo_root, domain="hr", display_name="HR", inputs=(("workday", "employees"),))
    commit_plan(repo_root, domain_plan)

    contract_path = repo_root / "lakehouse_code" / "silver" / "hr" / "contracts" / "floe" / "hr.yml"
    contract_path.write_text(contract_path.read_text(encoding="utf-8") + "x_extra: true\n", encoding="utf-8")

    plan = plan_product_new(
        repo_root,
        target="hr/headcount",
        display_name=None,
        silver_inputs=("employees",),
        inputs=(("workday", "absences"),),
        gold_tables=("mart_headcount",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    contract_doc = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert {entity["name"] for entity in contract_doc["entities"]} == {"employees", "absences"}
    assert contract_doc["x_extra"] is True


def test_source_new_quotes_a_yaml_special_scalar_in_a_flow_style_list(tmp_path: Path) -> None:
    """`sources: [crm, erp, "on"]` is valid, schema-accepted YAML -- "on" is
    a perfectly valid source identifier (`^[a-z][a-z0-9_]*$`). Converting
    the flow list to block style must re-emit it as the quoted string "on",
    not the bare scalar `on`, which YAML 1.1 loaders (including PyYAML's
    SafeLoader) read back as the boolean True."""
    repo_root = _seed_repo(tmp_path)
    on_dir = repo_root / "lakehouse_code" / "bronze" / "on"
    on_dir.mkdir(parents=True)
    (on_dir / "source.yaml").write_text(
        'apiVersion: openlakeforge.io/v1alpha3\nkind: Source\nname: "on"\ndisplayName: "On"\n'
        'description: "On source system."\nstatus: planned\nresources:\n  - name: events\n'
        '    description: Raw CSV events.\n',
        encoding="utf-8",
    )

    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    text = lakehouse_path.read_text(encoding="utf-8")
    text = re.sub(r"sources:\n(  - \S+\n)+", 'sources: [crm, erp, "on"]\n', text)
    lakehouse_path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["sources"] == ["crm", "erp", "on"]

    plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, plan)

    inventory = load_lakehouse_inventory(repo_root)
    assert set(inventory.source_names) == {"crm", "erp", "on", "marketing_platform"}


def test_product_new_converts_a_flow_style_entities_list_before_appending(tmp_path: Path) -> None:
    """`entities: [{name: existing, ...}]` is valid, schema-accepted Floe
    YAML; appending a new --input entity must convert it to block style
    first, not splice a block-style entity after the already-closed flow
    value."""
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees", "absences"))
    commit_plan(repo_root, source_plan)
    domain_plan = plan_domain_new(repo_root, domain="hr", display_name="HR", inputs=(("workday", "employees"),))
    commit_plan(repo_root, domain_plan)

    contract_path = repo_root / "lakehouse_code" / "silver" / "hr" / "contracts" / "floe" / "hr.yml"
    contract_doc = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    dumped_entities = yaml.safe_dump(contract_doc["entities"], default_flow_style=True, width=10**9).strip()
    flow_entities = f"entities: {dumped_entities}"
    original_contract = contract_path.read_text(encoding="utf-8")
    contract_text = re.sub(r"^entities:\n(.*\n)*$", flow_entities + "\n", original_contract, flags=re.MULTILINE)
    contract_path.write_text(contract_text, encoding="utf-8")
    assert yaml.safe_load(contract_text)["entities"][0]["name"] == "employees"

    plan = plan_product_new(
        repo_root,
        target="hr/headcount",
        display_name=None,
        silver_inputs=("employees",),
        inputs=(("workday", "absences"),),
        gold_tables=("mart_headcount",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    contract_result = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert {entity["name"] for entity in contract_result["entities"]} == {"employees", "absences"}


def test_product_new_rejects_when_domain_missing_and_no_input_given(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)

    with pytest.raises(ScaffoldError, match=r"does not exist yet"):
        plan_product_new(
            repo_root,
            target="hr/headcount",
            display_name=None,
            silver_inputs=(),
            inputs=(),
            gold_tables=("mart_x",),
            with_report=False,
        )


def test_product_new_rejects_silver_input_when_domain_does_not_exist(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)

    with pytest.raises(ScaffoldError, match=r"does not exist yet"):
        plan_product_new(
            repo_root,
            target="hr/headcount",
            display_name=None,
            silver_inputs=("employees",),
            inputs=(("crm", "orders"),),
            gold_tables=("mart_x",),
            with_report=False,
        )


def test_product_new_rejects_unresolved_silver_input(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)

    with pytest.raises(ScaffoldError, match=r"has no Silver table 'nonexistent'"):
        plan_product_new(
            repo_root,
            target="sales/new_product",
            display_name=None,
            silver_inputs=("nonexistent",),
            inputs=(),
            gold_tables=("mart_x",),
            with_report=False,
        )


def test_product_new_rejects_duplicate_global_product_id(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    before = _tree(repo_root)

    with pytest.raises(ScaffoldError, match=r"globally unique"):
        plan_product_new(
            repo_root,
            target="sales/order_revenue",
            display_name=None,
            silver_inputs=("orders",),
            inputs=(),
            gold_tables=("mart_x",),
            with_report=False,
        )

    assert _tree(repo_root) == before


def test_product_new_requires_at_least_one_gold_table(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)

    with pytest.raises(ScaffoldError, match=r"at least one --gold-table"):
        plan_product_new(
            repo_root,
            target="sales/new_product",
            display_name=None,
            silver_inputs=("orders",),
            inputs=(),
            gold_tables=(),
            with_report=False,
        )


def test_product_new_with_report_generates_a_draft_bundle_the_scaffold_can_build(tmp_path: Path) -> None:
    """#205: a scaffold-fresh `--with-report` bundle has no dashboard, so
    #201's promotion-contract rule ('exports no Superset dashboard') would
    fail it immediately. The scaffold must leave it undeclared and without
    `metadata.yaml` -- what makes a directory a bundle to
    `discover_report_dirs`/`validate_report_registry` -- so the draft is
    invisible to both the descriptor/tree parity check and the promotion
    contract until a real dashboard is exported and declared."""
    from olf import superset

    repo_root = _seed_repo(tmp_path)
    before = load_lakehouse_inventory(repo_root).dashboards

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=True,
    )
    commit_plan(repo_root, plan)

    dashboard_dir = repo_root / "lakehouse_code" / "dashboards" / "superset" / "order_summary"
    assert not (dashboard_dir / "metadata.yaml").exists()
    assert (dashboard_dir / "databases" / "openlakeforge_trino.yaml").is_file()
    assert (dashboard_dir / "datasets" / "OpenLakeForge_Trino" / "mart_order_summary.yaml").is_file()
    readme = (dashboard_dir / "README.md").read_text(encoding="utf-8")
    assert "SUPERSET_REPORT_SOURCE_DIR=lakehouse_code/dashboards/superset/order_summary" in readme
    assert "dashboards:" in readme and "order_summary" in readme
    # #229: `olf report export` accepts an explicit, existing SUPERSET_REPORT_SOURCE_DIR
    # even with zero declared dashboards, so the README no longer needs a
    # declare-before-export ordering workaround for a project's first dashboard.
    assert "do step 2 before step 1" not in readme

    inventory = load_lakehouse_inventory(repo_root)
    assert inventory.dashboards == before

    declared = tuple(d.report_source_dir for d in inventory.dashboards)
    assert superset.validate_report_registry(repo_root, declared) == sorted(declared)


def test_running_the_same_command_twice_is_refused_not_reapplied(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees",))
    commit_plan(repo_root, plan)
    lakehouse_after_first = (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text()

    with pytest.raises(ScaffoldError):
        plan_again = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees",))
        commit_plan(repo_root, plan_again)

    assert (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text() == lakehouse_after_first


def test_write_rolls_back_a_partial_write_on_filesystem_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_verify` proves a plan valid, but a filesystem-level failure
    (permission denied, disk full, ...) partway through the real write is
    still possible and can't be pre-checked. On that failure, every file
    already created this call must be deleted, and every edited file's
    original content restored -- not left stranded for a retry to trip
    over "refusing to overwrite existing file(s)"."""
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees", "absences"))
    commit_plan(repo_root, source_plan)
    domain_plan = plan_domain_new(repo_root, domain="hr", display_name="HR", inputs=(("workday", "employees"),))
    commit_plan(repo_root, domain_plan)

    plan = plan_product_new(
        repo_root,
        target="hr/headcount",
        display_name=None,
        silver_inputs=("employees",),
        inputs=(("workday", "absences"),),
        gold_tables=("mart_headcount",),
        with_report=False,
    )
    assert plan.edits, "expected the Floe contract edit to exercise the edit-rollback path too"
    contract_target = repo_root / plan.edits[0].relative_path
    original_contract = contract_target.read_text(encoding="utf-8")
    lakehouse_before = (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text(encoding="utf-8")

    from pathlib import Path as PathClass

    real_write_text = PathClass.write_text
    calls = {"count": 0}

    def flaky_write_text(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 3:
            raise OSError("simulated disk failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(PathClass, "write_text", flaky_write_text)

    with pytest.raises(OSError, match="simulated disk failure"):
        commit_plan(repo_root, plan)

    monkeypatch.undo()

    for scaffold_file in plan.files:
        assert not (repo_root / scaffold_file.relative_path).exists(), scaffold_file.relative_path
    assert contract_target.read_text(encoding="utf-8") == original_contract
    assert (repo_root / "lakehouse_code" / "lakehouse.yaml").read_text(encoding="utf-8") == lakehouse_before

    # A retry after the transient failure clears must succeed cleanly.
    commit_plan(repo_root, plan)
    inventory = load_lakehouse_inventory(repo_root)
    hr = next(d for d in inventory.domains if d.name == "hr")
    assert "headcount" in {p.id for p in hr.products}


def test_write_restores_lakehouse_yaml_when_its_own_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lakehouse.yaml's write is the last step of `_write`, not a special
    case exempt from rollback -- if it fails partway through (e.g. the disk
    fills while writing it), its own original content must be restored too,
    not just the new files created earlier in the same call."""
    repo_root = _seed_repo(tmp_path)
    lakehouse_path = repo_root / "lakehouse_code" / "lakehouse.yaml"
    lakehouse_before = lakehouse_path.read_text(encoding="utf-8")

    plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))

    from pathlib import Path as PathClass

    real_write_text = PathClass.write_text

    def flaky_write_text(self, *args, **kwargs):
        if self == lakehouse_path:
            raise OSError("simulated disk failure writing lakehouse.yaml")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(PathClass, "write_text", flaky_write_text)

    with pytest.raises(OSError, match="simulated disk failure writing lakehouse.yaml"):
        commit_plan(repo_root, plan)

    monkeypatch.undo()

    assert lakehouse_path.read_text(encoding="utf-8") == lakehouse_before
    for scaffold_file in plan.files:
        assert not (repo_root / scaffold_file.relative_path).exists(), scaffold_file.relative_path

    # A retry after the transient failure clears must succeed cleanly.
    commit_plan(repo_root, plan)
    inventory = load_lakehouse_inventory(repo_root)
    assert "marketing_platform" in inventory.source_names


def test_write_rolls_back_a_file_whose_own_write_partially_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_text() can create or truncate its target and then still raise
    partway through -- a disk-full error doesn't necessarily fail before
    touching the file. That target must be tracked for rollback before the
    write is attempted, not only after it succeeds, or the one file whose
    own write failed is left behind with partial content and blocks a
    retry with "refusing to overwrite existing file(s)"."""
    repo_root = _seed_repo(tmp_path)
    plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    failing_target = repo_root / plan.files[-1].relative_path

    from pathlib import Path as PathClass

    real_write_text = PathClass.write_text

    def flaky_write_text(self, content, *args, **kwargs):
        if self == failing_target:
            real_write_text(self, content[: len(content) // 2], *args, **kwargs)
            raise OSError("simulated partial write failure")
        return real_write_text(self, content, *args, **kwargs)

    monkeypatch.setattr(PathClass, "write_text", flaky_write_text)

    with pytest.raises(OSError, match="simulated partial write failure"):
        commit_plan(repo_root, plan)

    monkeypatch.undo()

    for scaffold_file in plan.files:
        assert not (repo_root / scaffold_file.relative_path).exists(), scaffold_file.relative_path

    # A retry after the transient failure clears must succeed cleanly.
    commit_plan(repo_root, plan)
    inventory = load_lakehouse_inventory(repo_root)
    assert "marketing_platform" in inventory.source_names


def test_add_domain_and_add_source_terminate_a_missing_final_newline(tmp_path: Path) -> None:
    """Property order is unconstrained at every level of the document, so
    `domains:`/`sources:` can legally be the document's last top-level key
    (e.g. `dashboards:` placed before them) -- not just `dashboards:`,
    which is merely the common case. Appending must still not concatenate
    onto an unterminated last line."""
    from olf.scaffold import _lakehouse_edit

    reordered_no_trailing_newline = (
        "apiVersion: openlakeforge.io/v1alpha3\n"
        "kind: Lakehouse\n"
        "name: test\n"
        "displayName: Test\n"
        "description: Test lakehouse.\n"
        "status: planned\n"
        "dashboards: []\n"
        "sources:\n"
        "  - crm"
    )
    result = _lakehouse_edit.add_source(reordered_no_trailing_newline, "erp")
    assert yaml.safe_load(result)["sources"] == ["crm", "erp"]

    reordered_domains_last = (
        "apiVersion: openlakeforge.io/v1alpha3\n"
        "kind: Lakehouse\n"
        "name: test\n"
        "displayName: Test\n"
        "description: Test lakehouse.\n"
        "status: planned\n"
        "sources:\n"
        "  - crm\n"
        "dashboards: []\n"
        "domains:\n"
        "  - name: sales\n"
        "    displayName: Sales\n"
        "    description: d\n"
        "    status: planned\n"
        "    silver_tables:\n"
        "      tables:\n"
        "        - {name: orders, source: crm, resource: orders}\n"
        "    products: []"
    )
    domain_block = (
        "  - name: hr\n"
        "    displayName: HR\n"
        "    description: d\n"
        "    status: planned\n"
        "    silver_tables:\n"
        "      tables:\n"
        "        - {name: employees, source: crm, resource: employees}\n"
        "    products: []\n"
    )
    result = _lakehouse_edit.add_domain(reordered_domains_last, domain_block)
    assert {d["name"] for d in yaml.safe_load(result)["domains"]} == {"sales", "hr"}


# --------------------------------------------------------------------------
# Cross-artifact consistency
# --------------------------------------------------------------------------


def test_generated_product_artifacts_are_internally_consistent(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="marketing_platform", display_name=None, resources=("campaigns",))
    commit_plan(repo_root, source_plan)
    plan = plan_product_new(
        repo_root,
        target="marketing/campaign_performance",
        display_name=None,
        silver_inputs=(),
        inputs=(("marketing_platform", "campaigns"),),
        gold_tables=("mart_campaign_performance",),
        with_report=False,
    )
    commit_plan(repo_root, plan)

    gold = repo_root / "lakehouse_code" / "gold" / "campaign_performance"
    sources_yml = (gold / "dbt" / "models" / "sources.yml").read_text()
    assert "schema: marketing_silver" in sources_yml

    floe_contract = (
        repo_root / "lakehouse_code" / "silver" / "marketing" / "contracts" / "floe" / "marketing.yml"
    ).read_text()
    assert 'namespace: "marketing_silver"' in floe_contract

    dagster_module = (repo_root / "lakehouse_code" / "pipelines" / "dagster" / "campaign_performance.py").read_text()
    assert '"campaigns"' in dagster_module
    assert '("marketing_platform", "campaigns")' in dagster_module


# --------------------------------------------------------------------------
# YAML-safety and Superset-identity regressions
# --------------------------------------------------------------------------


def test_with_report_reuses_the_shared_superset_database_identity(tmp_path: Path) -> None:
    """Every checked-in Superset bundle registers the same 'OpenLakeForge
    Trino' database uuid; a generated bundle must attach to that same
    connection rather than registering a second, disconnected one."""
    repo_root = _seed_repo(tmp_path)
    shared_uuid = "8a87434c-559e-545d-badd-3575affe0185"
    existing_bundle = (
        ROOT / "lakehouse_code" / "dashboards" / "superset" / "sales_order_revenue" / "databases"
        / "openlakeforge_trino.yaml"
    )
    assert yaml.safe_load(existing_bundle.read_text())["uuid"] == shared_uuid

    plan = plan_product_new(
        repo_root,
        target="sales/order_summary",
        display_name=None,
        silver_inputs=("orders",),
        inputs=(),
        gold_tables=("mart_order_summary",),
        with_report=True,
    )
    commit_plan(repo_root, plan)

    dashboard_dir = repo_root / "lakehouse_code" / "dashboards" / "superset" / "order_summary"
    database_doc = yaml.safe_load((dashboard_dir / "databases" / "openlakeforge_trino.yaml").read_text())
    dataset_doc = yaml.safe_load(
        (dashboard_dir / "datasets" / "OpenLakeForge_Trino" / "mart_order_summary.yaml").read_text()
    )
    assert database_doc["uuid"] == shared_uuid
    assert dataset_doc["database_uuid"] == shared_uuid


@pytest.mark.parametrize("tricky_name", ["HR & Ops: Team #1", 'Say "hi"', "Back\\slash"])
def test_display_name_with_yaml_metacharacters_round_trips(tmp_path: Path, tricky_name: str) -> None:
    """A --display-name containing YAML metacharacters (':', '#', quotes,
    backslashes) must not corrupt or break parsing of the generated
    descriptor -- across all three commands."""
    repo_root = _seed_repo(tmp_path)

    source_plan = plan_source_new(repo_root, source="workday", display_name=tricky_name, resources=("employees",))
    commit_plan(repo_root, source_plan)
    domain_plan = plan_domain_new(
        repo_root, domain="hr", display_name=tricky_name, inputs=(("workday", "employees"),)
    )
    commit_plan(repo_root, domain_plan)
    product_plan = plan_product_new(
        repo_root,
        target="hr/headcount",
        display_name=tricky_name,
        silver_inputs=("employees",),
        inputs=(),
        gold_tables=("mart_headcount",),
        with_report=False,
    )
    commit_plan(repo_root, product_plan)

    inventory = load_lakehouse_inventory(repo_root)
    assert "workday" in inventory.source_names
    hr = next(d for d in inventory.domains if d.name == "hr")
    assert hr.display_name == tricky_name
    headcount = next(p for p in hr.products if p.id == "headcount")
    assert headcount.display_name == tricky_name

    source_doc = yaml.safe_load((repo_root / "lakehouse_code" / "bronze" / "workday" / "source.yaml").read_text())
    assert source_doc["displayName"] == tricky_name


def test_csv_header_with_yaml_metacharacters_produces_valid_floe_contract(tmp_path: Path) -> None:
    """An example CSV header containing quotes/colons must not produce a
    Floe contract that fails to parse as YAML."""
    repo_root = _seed_repo(tmp_path)
    source_plan = plan_source_new(repo_root, source="workday", display_name=None, resources=("employees",))
    commit_plan(repo_root, source_plan)

    tricky_column = 'employee "nickname": primary'
    example_csv = repo_root / "lakehouse_code" / "bronze" / "workday" / "examples" / "employees.csv"
    example_csv.write_text(f"employees_id,{tricky_column}\nemployees-001,Al\n", encoding="utf-8")

    plan = plan_domain_new(repo_root, domain="hr", display_name=None, inputs=(("workday", "employees"),))
    commit_plan(repo_root, plan)

    contract_path = repo_root / "lakehouse_code" / "silver" / "hr" / "contracts" / "floe" / "hr.yml"
    contract_doc = yaml.safe_load(contract_path.read_text())
    columns = contract_doc["entities"][0]["schema"]["columns"]
    assert {column["name"] for column in columns} == {"employees_id", tricky_column}


# --------------------------------------------------------------------------
# CLI wiring smoke test
# --------------------------------------------------------------------------


def test_cli_source_domain_product_new_are_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "source" in result.output
    assert "domain" in result.output
    assert "product" in result.output


def test_cli_source_new_end_to_end(tmp_path: Path) -> None:
    repo_root = _seed_repo(tmp_path)
    _run(
        "source", "new", "marketing_platform",
        "--resource", "campaigns",
        "--repo-root", str(repo_root),
    )
    inventory = load_lakehouse_inventory(repo_root)
    assert "marketing_platform" in inventory.source_names


# --------------------------------------------------------------------------
# transitional projects (`olf init --empty`)
# --------------------------------------------------------------------------


_EMPTY_LAKEHOUSE = """\
apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: openlakeforge
displayName: OpenLakeForge
description: Empty OpenLakeForge project. Add a source and product before deployment.
status: planned
sources: []
domains: []
dashboards: []
"""


def _seed_empty_project(tmp_path: Path) -> Path:
    """The tree `olf init --empty` leaves behind, without the toolchain work."""
    lakehouse_code = tmp_path / "lakehouse_code"
    for package in ("", "bronze", "silver", "gold", "dashboards/superset", "pipelines/dagster"):
        directory = lakehouse_code / package if package else lakehouse_code
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (lakehouse_code / "lakehouse.yaml").write_text(_EMPTY_LAKEHOUSE, encoding="utf-8")
    return tmp_path


def test_scaffolding_walks_an_empty_project_to_strict_v1alpha3_validity(tmp_path: Path) -> None:
    repo_root = _seed_empty_project(tmp_path)

    _run("source", "new", "crm", "--resource", "accounts", "--repo-root", str(repo_root))
    _run("domain", "new", "sales", "--input", "crm/accounts", "--repo-root", str(repo_root))
    _run(
        "product",
        "new",
        "sales/accounts_report",
        "--silver-input",
        "accounts",
        "--gold-table",
        "mart_accounts",
        "--repo-root",
        str(repo_root),
    )

    inventory = load_lakehouse_inventory(repo_root)
    assert [product.id for product in inventory.products] == ["accounts_report"]
    assert contracts_check.descriptor_schema_errors(repo_root, schema_root=ROOT / "docs" / "schema") == []


def test_product_new_will_not_leave_an_empty_project_transitional(tmp_path: Path) -> None:
    """`product new` is the step that must produce a strict, deployable
    descriptor, so it never inherits the transitional waiver: with no source
    to consume, it fails rather than writing a half-valid lakehouse."""
    repo_root = _seed_empty_project(tmp_path)

    result = runner.invoke(
        app,
        [
            "product",
            "new",
            "sales/accounts_report",
            "--input",
            "crm/accounts",
            "--gold-table",
            "mart_accounts",
            "--repo-root",
            str(repo_root),
        ],
    )

    assert result.exit_code != 0
    assert load_lakehouse_inventory(repo_root, allow_incomplete=True).products == ()
