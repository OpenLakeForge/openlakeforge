"""Typed, provider-neutral inventory derived from lakehouse descriptors.

The canonical v1alpha3 inventory is composed from the ``Lakehouse`` descriptor
at ``lakehouse_code/lakehouse.yaml`` and one ``Source`` descriptor per bronze
source at ``lakehouse_code/bronze/<source>/source.yaml``.

Canonical identities:

* Bronze  ``<source>.<resource>``
* Silver  ``<domain>.<table>``
* Gold    ``<product>.<table>``

with the dependency chain Source/Bronze -> Domain/Silver -> Product/Gold ->
Dashboard. The legacy v1alpha1/v1alpha2 ``Domain`` inventory loaders below are
preserved for migration diagnostics; the canonical loaders are the
``load_lakehouse_inventory*`` functions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from .descriptors import (
    LakehouseDescriptorError,
    load_lakehouse_descriptor,
    load_source_descriptor,
)

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Table:
    """A provider-neutral logical table or source resource."""

    name: str


@dataclass(frozen=True)
class SourceResource:
    """A source-owned Bronze resource."""

    source: str
    name: str


@dataclass(frozen=True)
class SilverTable:
    """A domain-owned Silver table and its source resource."""

    domain: str
    name: str
    source: str
    resource: str


@dataclass(frozen=True)
class GoldTable:
    """A product-owned Gold table."""

    product: str
    name: str


@dataclass(frozen=True)
class ArtifactPrefixes:
    """Object-store locations derived from an owner's logical identity."""

    floe_report_prefix: str
    dbt_artifact_prefix: str


@dataclass(frozen=True)
class Source:
    """A bronze source and the resources it owns."""

    descriptor_path: Path
    name: str
    display_name: str
    resources: tuple[Table, ...]
    # Names of the settings and credentials its loader needs, never values:
    # each stage binds them in the ConfigMap and Secret below.
    runtime_config: tuple[str, ...] = ()
    runtime_secrets: tuple[str, ...] = ()

    @property
    def bronze_namespace(self) -> str:
        return f"{self.name}_bronze"

    @property
    def config_map_name(self) -> str:
        # Kubernetes names allow no `_` and source ids allow no `-`, so the
        # mapping cannot make two sources collide.
        return f"olf-source-{self.name.replace('_', '-')}"

    @property
    def credentials_secret_name(self) -> str:
        return f"{self.config_map_name}-credentials"

    def runtime_env_name(self, key: str) -> str:
        """The variable dlt reads `key` from: `SOURCES__<section>__<key>`.

        dlt names a source's config section after its module, and the loader
        is `bronze/<source>/dlt/<source>.py`, so the section is the source id.
        """
        return f"SOURCES__{self.name.upper()}__{key.upper()}"


@dataclass(frozen=True)
class Product:
    """A v1alpha3 product and its provider-neutral derived identities.

    Product identity is the product ``id`` (also the product ``name``); it is
    globally unique across the lakehouse inventory. The Silver layer is
    domain-aligned (``{domain}_silver``), the Gold layer is product-aligned
    (``{product}_gold``).
    """

    domain_name: str
    id: str
    display_name: str
    silver_inputs: tuple[str, ...]
    gold_tables: tuple[Table, ...]

    @property
    def name(self) -> str:
        return self.id

    @property
    def job_name(self) -> str:
        return f"{self.id}_pipeline"

    @property
    def silver_namespace(self) -> str:
        return f"{self.domain_name}_silver"

    @property
    def gold_namespace(self) -> str:
        return f"{self.id}_gold"

    @property
    def definitions_module(self) -> str:
        return f"lakehouse_code.pipelines.dagster.{self.id}"

    @property
    def dbt_artifact_prefix(self) -> str:
        return f"run-artifacts/dbt/{self.domain_name}/{self.id}/"

    @property
    def dbt_project_dir(self) -> str:
        return f"lakehouse_code/gold/{self.id}/dbt"

    @property
    def openmetadata_data_product_fqns(self) -> tuple[str, str]:
        return self.id, f"{self.domain_name}.{self.id}"

    @property
    def gold_mart_names(self) -> tuple[str, ...]:
        return tuple(f"{self.gold_namespace}.{table.name}" for table in self.gold_tables)


@dataclass(frozen=True)
class Domain:
    """A v1alpha3 domain and its products."""

    descriptor_path: Path
    name: str
    display_name: str
    silver_tables: tuple[SilverTable, ...]
    products: tuple[Product, ...]

    @property
    def artifact_prefixes(self) -> ArtifactPrefixes:
        return ArtifactPrefixes(
            floe_report_prefix=f"floe/reports/{self.name}/",
            dbt_artifact_prefix="",
        )

    @property
    def silver_namespace(self) -> str:
        return f"{self.name}_silver"


@dataclass(frozen=True)
class Dashboard:
    """A consumption-aligned dashboard bound to one or more products."""

    name: str
    products: tuple[str, ...]

    @property
    def report_source_dir(self) -> str:
        return f"lakehouse_code/dashboards/superset/{self.name}"

    @property
    def superset_export_bundle_name(self) -> str:
        return f"{self.name}_superset_assets_export.zip"


@dataclass(frozen=True)
class CatalogNamespace:
    """A physical catalog namespace and its object-store location."""

    name: str
    location: str


@dataclass(frozen=True)
class PhysicalProductNames:
    """Provider-specific names resolved for one logical product."""

    product: str
    gold_namespace: str
    gold_schema_fqn: str


@dataclass(frozen=True)
class PhysicalDomainNames:
    """Provider-specific names resolved for one logical Silver domain."""

    domain: str
    silver_namespace: str
    silver_schema_fqn: str


@dataclass(frozen=True)
class PhysicalSourceNames:
    """Provider-specific names resolved for one logical bronze source."""

    source: str
    bronze_namespace: str
    bronze_schema_fqn: str


@dataclass(frozen=True)
class PhysicalInventory:
    """Physical catalog and artifact identities for a provider contract."""

    catalog_namespaces: tuple[CatalogNamespace, ...]
    products: tuple[PhysicalProductNames, ...]
    domains: tuple[PhysicalDomainNames, ...] = ()
    sources: tuple[PhysicalSourceNames, ...] = ()

    @property
    def silver_namespaces(self) -> dict[str, str]:
        return {domain.domain: domain.silver_namespace for domain in self.domains}

    @property
    def gold_namespaces(self) -> dict[str, str]:
        return {product.product: product.gold_namespace for product in self.products}

    @property
    def silver_schema_fqns(self) -> dict[str, str]:
        return {domain.domain: domain.silver_schema_fqn for domain in self.domains}

    @property
    def gold_schema_fqns(self) -> dict[str, str]:
        return {product.product: product.gold_schema_fqn for product in self.products}

    @property
    def bronze_namespaces(self) -> dict[str, str]:
        return {source.source: source.bronze_namespace for source in self.sources}

    @property
    def bronze_schema_fqns(self) -> dict[str, str]:
        return {source.source: source.bronze_schema_fqn for source in self.sources}


@dataclass(frozen=True)
class LakehouseInventory:
    """All sources, domains, products, and dashboards declared in one lakehouse."""

    lakehouse_root: Path
    name: str
    sources: tuple[Source, ...]
    domains: tuple[Domain, ...]
    dashboards: tuple[Dashboard, ...]

    @property
    def products(self) -> tuple[Product, ...]:
        return tuple(product for domain in self.domains for product in domain.products)

    @property
    def job_names(self) -> tuple[str, ...]:
        return tuple(product.job_name for product in self.products)

    @property
    def catalog_namespaces(self) -> tuple[str, ...]:
        return tuple(
            namespace
            for namespace in (*self.bronze_namespace_names, *self.silver_namespace_names, *self.gold_namespace_names)
        )

    @property
    def default_product(self) -> Product:
        if not self.products:
            raise LakehouseDescriptorError(f"{self.lakehouse_root}: no data products were discovered")
        return self.products[0]

    @property
    def artifact_prefixes(self) -> tuple[ArtifactPrefixes, ...]:
        return tuple(domain.artifact_prefixes for domain in self.domains)

    @property
    def domain_names(self) -> tuple[str, ...]:
        return tuple(domain.name for domain in self.domains)

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(source.name for source in self.sources)

    @property
    def silver_table_count(self) -> int:
        """Count of distinct product-reachable physical Silver relations.

        A domain may declare Silver tables ahead of any product consuming
        them (see the product-less-domain scaffolding path), so this counts
        only tables referenced by at least one product's ``silver_inputs``,
        not every declared table. Two products in the same domain can
        declare the same table (they share the domain's Silver namespace),
        so this counts unique ``(domain, table)`` pairs rather than summing
        per-product declarations, which would double-count a shared table.
        """
        return sum(
            len({name for product in domain.products for name in product.silver_inputs})
            for domain in self.domains
        )

    @property
    def gold_table_count(self) -> int:
        return sum(len(product.gold_tables) for product in self.products)

    @property
    def gold_mart_names(self) -> tuple[str, ...]:
        return tuple(mart for product in self.products for mart in product.gold_mart_names)

    @property
    def openmetadata_data_products(self) -> dict[str, tuple[str, str]]:
        return {product.name: product.openmetadata_data_product_fqns for product in self.products}

    @property
    def bronze_namespace_names(self) -> frozenset[str]:
        return frozenset(source.bronze_namespace for source in self.sources)

    @property
    def silver_namespace_names(self) -> frozenset[str]:
        return frozenset(domain.silver_namespace for domain in self.domains)

    def domain_for_product(self, product: Product) -> Domain:
        return next(domain for domain in self.domains if domain.name == product.domain_name)

    def resolved_silver_tables(self, product: Product) -> tuple[SilverTable, ...]:
        domain = self.domain_for_product(product)
        by_name = {table.name: table for table in domain.silver_tables}
        return tuple(by_name[name] for name in product.silver_inputs)

    def bronze_resources_for_product(self, product: Product) -> tuple[SourceResource, ...]:
        return tuple(SourceResource(table.source, table.resource) for table in self.resolved_silver_tables(product))

    @property
    def gold_namespace_names(self) -> frozenset[str]:
        return frozenset(product.gold_namespace for product in self.products)

    def resolve_physical_names(
        self,
        *,
        catalog_database_fqn: str,
        bronze_bucket: str,
        silver_bucket: str,
        gold_bucket: str,
        namespace_prefix: str = "",
    ) -> PhysicalInventory:
        """Resolve physical names from provider-contract values.

        `namespace_prefix` (default: none) is prepended to every physical
        database name. Every provider with its own catalog per stage (local/
        Polaris, AWS/Azure custom Glue catalogs) leaves this empty - the
        catalog boundary alone makes two stages' `sales_silver` collision-
        free. It exists for a provider whose catalog is *not* per-stage (AWS
        Glue's shared default catalog, the only kind this account's Glue
        service allows creating): there, two stages' otherwise-identical
        database names would collide in the one shared catalog without it.
        """
        namespaces: list[CatalogNamespace] = []
        products: list[PhysicalProductNames] = []
        domains: list[PhysicalDomainNames] = []
        sources: list[PhysicalSourceNames] = []
        for domain in self.domains:
            physical_name = f"{namespace_prefix}{domain.silver_namespace}"
            namespaces.append(CatalogNamespace(physical_name, f"s3://{silver_bucket}/{domain.silver_namespace}/"))
            domains.append(
                PhysicalDomainNames(
                    domain=domain.name,
                    silver_namespace=physical_name,
                    silver_schema_fqn=f"{catalog_database_fqn}.{physical_name}",
                )
            )
        for product in self.products:
            physical_name = f"{namespace_prefix}{product.gold_namespace}"
            namespaces.append(CatalogNamespace(physical_name, f"s3://{gold_bucket}/{product.gold_namespace}/"))
            products.append(
                PhysicalProductNames(
                    product=product.name,
                    gold_namespace=physical_name,
                    gold_schema_fqn=f"{catalog_database_fqn}.{physical_name}",
                )
            )
        for source in self.sources:
            physical_name = f"{namespace_prefix}{source.bronze_namespace}"
            namespaces.append(CatalogNamespace(physical_name, f"s3://{bronze_bucket}/{source.bronze_namespace}/"))
            sources.append(
                PhysicalSourceNames(
                    source=source.name,
                    bronze_namespace=physical_name,
                    bronze_schema_fqn=f"{catalog_database_fqn}.{physical_name}",
                )
            )
        return PhysicalInventory(
            catalog_namespaces=tuple(namespaces), products=tuple(products), domains=tuple(domains), sources=tuple(sources)
        )


def _lakehouse_root(path: Path) -> Path:
    candidate = path.resolve()
    return candidate / "lakehouse_code" if (candidate / "lakehouse_code").is_dir() else candidate


def _source_table_names(source: Mapping[str, Any]) -> set[str]:
    resources = source.get("resources")
    if not isinstance(resources, list):
        return set()
    return {resource["name"] for resource in resources if isinstance(resource, Mapping) and "name" in resource}


def _product(
    path: Path, domain_name: str, product: Mapping[str, Any], index: int, silver_tables: tuple[SilverTable, ...]
) -> Product:
    label = product.get("id") or f"products[{index}]"
    product_id = product.get("id")
    if not isinstance(product_id, str) or not _IDENTIFIER_PATTERN.fullmatch(product_id):
        raise LakehouseDescriptorError(f"{path}: product {label!r}: id must match '^[a-z][a-z0-9_]*$'")
    display_name = product.get("displayName")
    if not isinstance(display_name, str) or not display_name:
        raise LakehouseDescriptorError(f"{path}: product {label!r}: displayName is required and must be a non-empty string")
    inputs = product.get("silver_inputs")
    if not isinstance(inputs, list) or not inputs:
        raise LakehouseDescriptorError(f"{path}: product {label!r}: silver_inputs must be a non-empty array")
    return Product(
        domain_name=domain_name,
        id=product_id,
        display_name=display_name,
        silver_inputs=tuple(inputs),
        gold_tables=_table_group(path, product, "gold_tables", label),
    )


def _silver_tables(path: Path, domain_name: str, domain: Mapping[str, Any]) -> tuple[SilverTable, ...]:
    tables = domain["silver_tables"]["tables"]
    return tuple(
        SilverTable(domain=domain_name, name=table["name"], source=table["source"], resource=table["resource"])
        for table in tables
    )


def _table_group(path: Path, product: Mapping[str, Any], field: str, label: str) -> tuple[Table, ...]:
    spec = product.get(field)
    if not isinstance(spec, Mapping):
        raise LakehouseDescriptorError(f"{path}: product {label!r}: {field} is required and must be an object")
    tables = spec.get("tables")
    if not isinstance(tables, list) or not tables:
        raise LakehouseDescriptorError(f"{path}: product {label!r}: {field}.tables must be a non-empty array")
    names = [table.get("name") for table in tables if isinstance(table, Mapping)]
    if len(names) != len(tables) or any(not isinstance(name, str) or not name for name in names):
        raise LakehouseDescriptorError(f"{path}: product {label!r}: {field}.tables must contain non-empty string names")
    if len(names) != len(set(names)):
        raise LakehouseDescriptorError(f"{path}: product {label!r}: {field}.tables must not contain duplicate names")
    return tuple(Table(name=name) for name in names)


def _validate_inventory_identities(
    lakehouse_root: Path,
    sources: tuple[Source, ...],
    domains: tuple[Domain, ...],
    dashboards: tuple[Dashboard, ...],
) -> None:
    seen_sources: set[str] = set()
    for source in sources:
        if source.name in seen_sources:
            raise LakehouseDescriptorError(f"{source.descriptor_path}: duplicate source name {source.name!r}")
        seen_sources.add(source.name)
    seen_domains: set[str] = set()
    seen_product_ids: set[str] = set()
    for domain in domains:
        if domain.name in seen_domains:
            raise LakehouseDescriptorError(f"{domain.descriptor_path}: duplicate domain name {domain.name!r}")
        seen_domains.add(domain.name)
        silver_by_name = {table.name: table for table in domain.silver_tables}
        for table in domain.silver_tables:
            source = next((candidate for candidate in sources if candidate.name == table.source), None)
            if source is None:
                raise LakehouseDescriptorError(
                    f"{domain.descriptor_path}: domain {domain.name!r}: Silver table {table.name!r} references unknown source {table.source!r}"
                )
            if table.resource not in {resource.name for resource in source.resources}:
                raise LakehouseDescriptorError(
                    f"{domain.descriptor_path}: domain {domain.name!r}: Silver table {table.name!r} references unknown "
                    f"resource {table.resource!r} of source {table.source!r}"
                )
        for product in domain.products:
            if product.id in seen_product_ids:
                raise LakehouseDescriptorError(
                    f"{domain.descriptor_path}: product {product.id!r}: id must be globally unique across the lakehouse"
                )
            seen_product_ids.add(product.id)
            unknown = [name for name in product.silver_inputs if name not in silver_by_name]
            if unknown:
                raise LakehouseDescriptorError(
                    f"{domain.descriptor_path}: product {product.id!r}: silver_inputs reference unknown domain Silver tables {unknown!r}"
                )
    seen_dashboards: set[str] = set()
    for dashboard in dashboards:
        if dashboard.name in seen_dashboards:
            raise LakehouseDescriptorError(f"{lakehouse_root}: duplicate dashboard name {dashboard.name!r}")
        seen_dashboards.add(dashboard.name)
        unknown_products = sorted(set(dashboard.products) - seen_product_ids)
        if unknown_products:
            raise LakehouseDescriptorError(
                f"{lakehouse_root}: dashboard {dashboard.name!r}: products {unknown_products!r} must reference declared products"
            )


def _load_sources(source_paths: Sequence[str | Path]) -> tuple[Source, ...]:
    sources: list[Source] = []
    for source_path in source_paths:
        document = load_source_descriptor(source_path)
        name = document["name"]
        if name != Path(source_path).parent.name:
            raise LakehouseDescriptorError(
                f"{source_path}: name {name!r} must match source directory {Path(source_path).parent.name!r}"
            )
        resources = tuple(
            Table(name=resource["name"])
            for resource in document["resources"]
            if isinstance(resource, Mapping) and "name" in resource
        )
        sources.append(
            Source(
                descriptor_path=Path(source_path),
                name=name,
                display_name=document["displayName"],
                resources=resources,
                runtime_config=tuple(document.get("runtime", {}).get("config", ())),
                runtime_secrets=tuple(document.get("runtime", {}).get("secrets", ())),
            )
        )
    return tuple(sources)


def load_lakehouse_inventory_from_descriptors(
    lakehouse_path: str | Path,
    source_paths: Sequence[str | Path],
    *,
    source_label: str | Path | None = None,
    allow_incomplete: bool = False,
) -> LakehouseInventory:
    """Load and validate one Lakehouse descriptor plus its Source descriptors."""
    lakehouse_document = load_lakehouse_descriptor(lakehouse_path, allow_incomplete=allow_incomplete)
    sources = _load_sources(source_paths)
    declared_sources = set(lakehouse_document["sources"])
    discovered_sources = {source.name for source in sources}
    if declared_sources != discovered_sources:
        raise LakehouseDescriptorError(
            f"{lakehouse_path}: lakehouse sources {sorted(declared_sources)!r} must match discovered "
            f"source descriptors {sorted(discovered_sources)!r} under lakehouse_code/bronze/*/source.yaml"
        )
    domains: list[Domain] = []
    for index, domain in enumerate(lakehouse_document["domains"]):
        if not isinstance(domain, Mapping):
            raise LakehouseDescriptorError(f"{lakehouse_path}: domains[{index}] must be an object")
        domain_name = domain["name"]
        silver_tables = _silver_tables(lakehouse_path, domain_name, domain)
        products = tuple(
            _product(lakehouse_path, domain_name, product, product_index, silver_tables)
            for product_index, product in enumerate(domain["products"])
            if isinstance(product, Mapping)
        )
        domains.append(
            Domain(
                descriptor_path=Path(lakehouse_path),
                name=domain_name,
                display_name=domain["displayName"],
                silver_tables=silver_tables,
                products=products,
            )
        )
    dashboards = tuple(
        Dashboard(
            name=dashboard["name"],
            products=tuple(dashboard["products"]),
        )
        for dashboard in lakehouse_document["dashboards"]
        if isinstance(dashboard, Mapping)
    )
    inventory = LakehouseInventory(
        lakehouse_root=Path(source_label) if source_label is not None else Path(lakehouse_path).parent,
        name=lakehouse_document["name"],
        sources=sources,
        domains=tuple(domains),
        dashboards=dashboards,
    )
    _validate_inventory_identities(inventory.lakehouse_root, inventory.sources, inventory.domains, inventory.dashboards)
    return inventory


def load_lakehouse_inventory(path: str | Path, *, allow_incomplete: bool = False) -> LakehouseInventory:
    """Load and validate ``lakehouse_code/lakehouse.yaml`` plus every source."""
    lakehouse_root = _lakehouse_root(Path(path))
    lakehouse_descriptor = lakehouse_root / "lakehouse.yaml"
    if not lakehouse_descriptor.is_file():
        raise LakehouseDescriptorError(f"{lakehouse_root}: no lakehouse descriptor found at lakehouse.yaml")
    source_descriptors = sorted(lakehouse_root.glob("bronze/*/source.yaml"))
    if not source_descriptors and not allow_incomplete:
        raise LakehouseDescriptorError(f"{lakehouse_root}: no source descriptors found at bronze/*/source.yaml")
    return load_lakehouse_inventory_from_descriptors(
        lakehouse_descriptor, source_descriptors, source_label=lakehouse_root, allow_incomplete=allow_incomplete
    )


def load_transitional_lakehouse_inventory(path: str | Path) -> LakehouseInventory:
    """Load an `olf init --empty` project while it has no runnable product.

    This is intentionally limited to scaffold planning. Deployment and
    ordinary descriptor validation retain the strict v1alpha3 contract.
    """
    return load_lakehouse_inventory(path, allow_incomplete=True)


@cache
def _cached_lakehouse_inventory(resolved_path: str) -> LakehouseInventory:
    return load_lakehouse_inventory(resolved_path)


def inventory_for(repo_root: str | Path) -> LakehouseInventory:
    """Load and cache the canonical lakehouse inventory for a repository root."""
    return _cached_lakehouse_inventory(str(Path(repo_root).resolve()))

