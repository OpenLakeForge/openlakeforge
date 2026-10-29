"""Derive the executable inventory offline from the code that owns it.

``governance.yml`` names domains and products; everything else is read from
the checked-in contracts, with no cluster, Trino or OpenMetadata involved:

* Bronze  ``bronze/<source>/source.yaml``
* Silver  ``silver/<domain>/contracts/floe/*.yml`` (one Floe entity per table)
* Gold    ``gold/<product>/dbt/models/**/*.sql`` (one dbt model per table)
* Silver inputs of a product: the ``source('silver', ...)`` calls in its models
* Dashboard products: the ``<product>_gold`` schema of its Superset datasets

A contract that disagrees with governance or with another contract is an
error; nothing is guessed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .descriptors import LakehouseDescriptorError
from .governance import load_governance_descriptor
from .inventory import (
    Dashboard,
    Domain,
    LakehouseInventory,
    Product,
    SilverTable,
    Table,
    _lakehouse_root,
    _load_sources,
    _validate_inventory_identities,
)

# ponytail: a regex over the model text, so a call inside a Jinja comment still
# counts. Parse dbt's manifest instead when a project hits that.
_DBT_SOURCE_CALL = re.compile(r"""\bsource\s*\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""")
# An alias renames the relation dbt creates and an ephemeral model creates none,
# so the model name would stop being a table name.
_DBT_UNSUPPORTED = re.compile(r"\balias\s*=|['\"]?ephemeral\b")


def _yaml(path: Path) -> Mapping[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise LakehouseDescriptorError(f"{path}: must contain a YAML object")
    return document


def _silver_tables(root: Path, domain: str) -> tuple[SilverTable, ...]:
    floe_dir = root / "silver" / domain / "contracts" / "floe"
    domain_file = floe_dir / "_domain.yml"
    if not domain_file.is_file():
        raise LakehouseDescriptorError(f"{domain_file}: domain {domain!r} has no Floe domain contract")
    if _yaml(domain_file).get("name") != domain:
        raise LakehouseDescriptorError(f"{domain_file}: name must be {domain!r} to match governance")
    tables: dict[str, SilverTable] = {}
    for path in sorted(floe_dir.glob("*.yml")):
        if path == domain_file:
            continue
        entity = _yaml(path)
        name, ref = entity.get("name"), entity.get("source")
        if not isinstance(ref, Mapping) or not name or not ref.get("ref") or not ref.get("resource"):
            raise LakehouseDescriptorError(f"{path}: a Floe entity needs a name and a source ref and resource")
        if name in tables:
            raise LakehouseDescriptorError(f"{path}: duplicate Silver table {name!r} in domain {domain!r}")
        tables[name] = SilverTable(domain=domain, name=name, source=ref["ref"], resource=ref["resource"])
    return tuple(tables.values())


def _gold(root: Path, domain: str, product_id: str, display_name: str) -> Product:
    dbt_dir = root / "gold" / product_id / "dbt"
    project_file = dbt_dir / "dbt_project.yml"
    if not project_file.is_file():
        raise LakehouseDescriptorError(f"{project_file}: product {product_id!r} has no dbt project")
    if _yaml(project_file).get("name") != product_id:
        raise LakehouseDescriptorError(f"{project_file}: name must be {product_id!r} to match governance")
    if _DBT_UNSUPPORTED.search(project_file.read_text(encoding="utf-8")):
        raise LakehouseDescriptorError(f"{project_file}: dbt aliases and ephemeral models are not supported")
    models = sorted((dbt_dir / "models").rglob("*.sql"))
    names = [model.stem for model in models]
    if not names:
        raise LakehouseDescriptorError(f"{dbt_dir}: product {product_id!r} has no dbt models")
    if len(names) != len(set(names)):
        raise LakehouseDescriptorError(f"{dbt_dir}: product {product_id!r} has duplicate dbt model names")
    inputs: list[str] = []
    for model in models:
        text = model.read_text(encoding="utf-8")
        if _DBT_UNSUPPORTED.search(text):
            raise LakehouseDescriptorError(f"{model}: dbt aliases and ephemeral models are not supported")
        for source, table in _DBT_SOURCE_CALL.findall(text):
            if source != "silver":
                raise LakehouseDescriptorError(
                    f"{model}: reads dbt source {source!r}; a product reads Silver through source('silver', ...)"
                )
            if table not in inputs:
                inputs.append(table)
    if not inputs:
        raise LakehouseDescriptorError(f"{dbt_dir}: product {product_id!r} reads no Silver table")
    return Product(
        domain_name=domain,
        id=product_id,
        display_name=display_name,
        silver_inputs=tuple(sorted(inputs)),
        gold_tables=tuple(Table(name) for name in names),
    )


def _dashboards(root: Path, products: tuple[Product, ...]) -> tuple[Dashboard, ...]:
    gold_schemas = {product.gold_namespace: product.id for product in products}
    dashboards = []
    for metadata in sorted((root / "dashboards" / "superset").glob("*/metadata.yaml")):
        used: list[str] = []
        for dataset in sorted(metadata.parent.glob("datasets/**/*")):
            if dataset.suffix not in {".yaml", ".yml"}:
                continue
            schema = _yaml(dataset).get("schema")
            if isinstance(schema, str) and schema.endswith("_gold"):
                if schema not in gold_schemas:
                    raise LakehouseDescriptorError(f"{dataset}: schema {schema!r} is no governed product's Gold schema")
                if gold_schemas[schema] not in used:
                    used.append(gold_schemas[schema])
        dashboards.append(Dashboard(name=metadata.parent.name, products=tuple(used)))
    return tuple(dashboards)


def _reject_ungoverned(root: Path, domains: tuple[Domain, ...]) -> None:
    products = {product.id for domain in domains for product in domain.products}
    pipelines = {path.stem for path in (root / "pipelines" / "dagster").glob("*.py") if path.stem != "__init__"}
    for label, found, governed in (
        ("Silver domain", {p.parts[-4] for p in (root / "silver").glob("*/contracts/floe/_domain.yml")},
         {domain.name for domain in domains}),
        ("Gold product", {p.parts[-3] for p in (root / "gold").glob("*/dbt/dbt_project.yml")}, products),
        ("Dagster module", pipelines, products),
    ):
        if found - governed:
            raise LakehouseDescriptorError(f"{root}: {label} {sorted(found - governed)!r} is not in governance.yml")
    if products - pipelines:
        raise LakehouseDescriptorError(f"{root}: governed products {sorted(products - pipelines)!r} have no Dagster module")


def load_governance_inventory(path: str | Path, *, allow_incomplete: bool = False) -> LakehouseInventory:
    """Load ``lakehouse_code/governance.yml`` and derive the rest from project code."""
    root = _lakehouse_root(Path(path))
    descriptor = root / "governance.yml"
    if not descriptor.is_file():
        raise LakehouseDescriptorError(f"{root}: no governance descriptor found at governance.yml")
    document = load_governance_descriptor(descriptor, allow_incomplete=allow_incomplete)
    source_paths = sorted(root.glob("bronze/*/source.yaml"))
    if not source_paths and not allow_incomplete:
        raise LakehouseDescriptorError(f"{root}: no source descriptors found at bronze/*/source.yaml")
    domains = tuple(
        Domain(
            descriptor_path=descriptor,
            name=domain["name"],
            display_name=domain["displayName"],
            silver_tables=_silver_tables(root, domain["name"]),
            products=tuple(
                _gold(root, domain["name"], product["id"], product["displayName"]) for product in domain["products"]
            ),
        )
        for domain in document["domains"]
    )
    _reject_ungoverned(root, domains)
    dashboards = _dashboards(root, tuple(product for domain in domains for product in domain.products))
    sources = _load_sources(source_paths)
    _validate_inventory_identities(root, sources, domains, dashboards)
    # Floe's project name is the one name the project already carries.
    name = _yaml(root / "silver" / "floe.yml").get("metadata", {}).get("project", root.parent.name)
    return LakehouseInventory(lakehouse_root=root, name=name, sources=sources, domains=domains, dashboards=dashboards)
