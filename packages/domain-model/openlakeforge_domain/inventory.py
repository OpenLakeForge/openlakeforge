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

import yaml

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
class FloeEntity:
    """A domain's Silver entity as its Floe file declares it, and the Bronze resource feeding it."""

    domain: str
    name: str
    source: str
    resource: str


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


@dataclass(frozen=True)
class Domain:
    """A v1alpha3 domain and its products."""

    descriptor_path: Path
    name: str
    display_name: str
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

    def floe_entities(self, domain: Domain) -> tuple[FloeEntity, ...]:
        """The domain's Silver entities from its Floe files, each fed by a Bronze resource a Source declares."""
        resources = {(source.name, resource.name) for source in self.sources for resource in source.resources}
        entities = _read_floe_entities(self.lakehouse_root, domain.name)
        for entity in entities:
            if (entity.source, entity.resource) not in resources:
                raise LakehouseDescriptorError(
                    f"{self.lakehouse_root}: Silver entity {domain.name}/{entity.name} reads unknown Bronze "
                    f"resource {entity.source}/{entity.resource}"
                )
        return entities

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


def _product(path: Path, domain_name: str, product: Mapping[str, Any], index: int) -> Product:
    label = product.get("id") or f"products[{index}]"
    product_id = product.get("id")
    if not isinstance(product_id, str) or not _IDENTIFIER_PATTERN.fullmatch(product_id):
        raise LakehouseDescriptorError(f"{path}: product {label!r}: id must match '^[a-z][a-z0-9_]*$'")
    display_name = product.get("displayName")
    if not isinstance(display_name, str) or not display_name:
        raise LakehouseDescriptorError(f"{path}: product {label!r}: displayName is required and must be a non-empty string")
    return Product(domain_name=domain_name, id=product_id, display_name=display_name)


def _read_floe_entities(lakehouse_root: Path, domain: str) -> tuple[FloeEntity, ...]:
    entity_dir = lakehouse_root / "silver" / domain / "contracts" / "floe"
    entities = []
    for path in sorted(entity_dir.glob("*.yml")):
        if path.name == "_domain.yml":
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            entities.append(
                FloeEntity(domain, document["name"], document["source"]["ref"], document["source"]["resource"])
            )
        except (KeyError, TypeError) as exc:
            raise LakehouseDescriptorError(f"{path}: entity must declare name and source.ref/resource") from exc
    return tuple(entities)


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
        for product in domain.products:
            if product.id in seen_product_ids:
                raise LakehouseDescriptorError(
                    f"{domain.descriptor_path}: product {product.id!r}: id must be globally unique across the lakehouse"
                )
            seen_product_ids.add(product.id)
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


def load_lakehouse_inventory_from_descriptors(
    lakehouse_path: str | Path,
    source_paths: Sequence[str | Path],
    *,
    source_label: str | Path | None = None,
    allow_incomplete: bool = False,
) -> LakehouseInventory:
    """Load and validate one Lakehouse descriptor plus its Source descriptors."""
    lakehouse_document = load_lakehouse_descriptor(lakehouse_path, allow_incomplete=allow_incomplete)
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
        products = tuple(
            _product(lakehouse_path, domain_name, product, product_index)
            for product_index, product in enumerate(domain["products"])
            if isinstance(product, Mapping)
        )
        domains.append(
            Domain(
                descriptor_path=Path(lakehouse_path),
                name=domain_name,
                display_name=domain["displayName"],
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
        sources=tuple(sources),
        domains=tuple(domains),
        dashboards=dashboards,
    )
    _validate_inventory_identities(inventory.lakehouse_root, inventory.sources, inventory.domains, inventory.dashboards)
    return inventory


def load_lakehouse_inventory(path: str | Path, *, allow_incomplete: bool = False) -> LakehouseInventory:
    """Load and validate ``lakehouse_code/lakehouse.yaml`` plus every source."""
    lakehouse_root = _lakehouse_root(Path(path))
    lakehouse_descriptor = lakehouse_root / "governance.yml"
    legacy_descriptor = lakehouse_root / "lakehouse.yaml"
    # The empty shell `olf init --empty` once wrote has no domains and declares no sources.
    if (
        not lakehouse_descriptor.is_file()
        and legacy_descriptor.is_file()
        and (yaml.safe_load(legacy_descriptor.read_text(encoding="utf-8")) or {}).get("domains")
    ):
        lakehouse_descriptor = legacy_descriptor
    if not lakehouse_descriptor.is_file() and not allow_incomplete:
        raise LakehouseDescriptorError(f"{lakehouse_root}: no governance.yml (or legacy lakehouse.yaml) found")
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

