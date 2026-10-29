"""Versioned descriptor validation for the Lakehouse and Source kinds.

The canonical v1alpha3 model is the ``Lakehouse`` descriptor at
``lakehouse_code/lakehouse.yaml`` together with one ``Source`` descriptor per
bronze source at ``lakehouse_code/bronze/<source>/source.yaml``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

LAKEHOUSE_API_VERSION = "openlakeforge.io/v1alpha3"
SOURCE_API_VERSION = "openlakeforge.io/v1alpha3"
LAKEHOUSE_KIND = "Lakehouse"
SOURCE_KIND = "Source"
_PRODUCT_IDENTIFIER_PATTERN = r"[a-z][a-z0-9_]*"
_IDENTIFIER_PATTERN = re.compile(rf"^{_PRODUCT_IDENTIFIER_PATTERN}$")


class LakehouseDescriptorError(ValueError):
    """Raised when a v1alpha3 lakehouse or source descriptor is invalid."""


def _identifier(value: object, *, field: str, source: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise LakehouseDescriptorError(f"{source}: {field} must match '^[a-z][a-z0-9_]*$'")
    return value


def _non_empty_string(value: object, *, field: str, source: str) -> str:
    if not isinstance(value, str) or not value:
        raise LakehouseDescriptorError(f"{source}: {field} must be a non-empty string")
    return value


def _optional_string(value: object, *, field: str, source: str) -> None:
    if value is not None and not isinstance(value, str):
        raise LakehouseDescriptorError(f"{source}: {field} must be a string")


def validate_source_descriptor(document: Mapping[str, Any], *, source: str = "source.yaml") -> None:
    """Validate a v1alpha3 Source descriptor envelope and resource list."""
    if document.get("apiVersion") != SOURCE_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: unsupported apiVersion {document.get('apiVersion')!r}; expected {SOURCE_API_VERSION!r}"
        )
    if document.get("kind") != SOURCE_KIND:
        raise LakehouseDescriptorError(f"{source}: kind must be {SOURCE_KIND!r}")
    for field in ("name", "displayName", "description", "status", "resources"):
        if field not in document:
            raise LakehouseDescriptorError(f"{source}: missing required field {field!r}")
    _identifier(document["name"], field="name", source=source)
    _non_empty_string(document["displayName"], field="displayName", source=source)
    _non_empty_string(document["status"], field="status", source=source)
    _optional_string(document["description"], field="description", source=source)
    resources = document["resources"]
    if not isinstance(resources, list) or not resources:
        raise LakehouseDescriptorError(f"{source}: resources must be a non-empty array")
    seen: set[str] = set()
    for index, resource in enumerate(resources):
        if not isinstance(resource, Mapping):
            raise LakehouseDescriptorError(f"{source}: resources[{index}] must be an object")
        if "name" not in resource:
            raise LakehouseDescriptorError(f"{source}: resources[{index}] is missing required field 'name'")
        name = _identifier(resource["name"], field=f"resources[{index}].name", source=source)
        if name in seen:
            raise LakehouseDescriptorError(f"{source}: duplicate resource name {name!r}")
        seen.add(name)
        if "description" in resource and not isinstance(resource["description"], str):
            raise LakehouseDescriptorError(f"{source}: resources[{index}].description must be a string")
        unexpected = set(resource) - {"name", "description"}
        if unexpected:
            raise LakehouseDescriptorError(
                f"{source}: resources[{index}] must not contain {sorted(unexpected)!r} (provider-neutral fields only)"
            )
    if "runtime" in document:
        _validate_source_runtime(document["runtime"], source=source)
    unexpected = set(document) - {
        "apiVersion",
        "kind",
        "name",
        "displayName",
        "description",
        "status",
        "resources",
        "runtime",
    }
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: must not contain {sorted(unexpected)!r} (provider-neutral fields only)"
        )


def _validate_source_runtime(runtime: object, *, source: str) -> None:
    # Key names only: their values are a stage's to bind, in its own ConfigMap
    # and Secret, so a revision promotes unchanged from DEV to PROD (#546).
    if not isinstance(runtime, Mapping):
        raise LakehouseDescriptorError(f"{source}: runtime must be an object")
    unexpected = set(runtime) - {"config", "secrets"}
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: runtime must not contain {sorted(unexpected)!r}; it names config and secrets keys only"
        )
    listed: dict[str, str] = {}
    for group in ("config", "secrets"):
        keys = runtime.get(group, [])
        if not isinstance(keys, list):
            raise LakehouseDescriptorError(f"{source}: runtime.{group} must be an array of key names")
        for index, key in enumerate(keys):
            name = _identifier(key, field=f"runtime.{group}[{index}]", source=source)
            if name in listed:
                raise LakehouseDescriptorError(
                    f"{source}: runtime key {name!r} is listed twice (runtime.{listed[name]} and runtime.{group})"
                )
            listed[name] = group


def _validate_table_group(product: Mapping[str, Any], *, group: str, source: str) -> None:
    spec = product.get(group)
    if not isinstance(spec, Mapping):
        raise LakehouseDescriptorError(f"{source}: product {product.get('id')!r}: {group} must be an object")
    tables = spec.get("tables")
    if not isinstance(tables, list) or not tables:
        raise LakehouseDescriptorError(f"{source}: product {product.get('id')!r}: {group}.tables must be a non-empty array")
    seen: set[str] = set()
    for index, table in enumerate(tables):
        if not isinstance(table, Mapping) or not _IDENTIFIER_PATTERN.fullmatch(table.get("name") or ""):
            raise LakehouseDescriptorError(
                f"{source}: product {product.get('id')!r}: {group}.tables[{index}].name must match '^[a-z][a-z0-9_]*$'"
            )
        if table["name"] in seen:
            raise LakehouseDescriptorError(
                f"{source}: product {product.get('id')!r}: {group}.tables must not contain duplicate names"
            )
        seen.add(table["name"])
        if "description" in table and not isinstance(table["description"], str):
            raise LakehouseDescriptorError(f"{source}: product {product.get('id')!r}: {group}.tables[{index}].description must be a string")
        unexpected = set(table) - {"name", "description"}
        if unexpected:
            raise LakehouseDescriptorError(
                f"{source}: product {product.get('id')!r}: {group}.tables[{index}] must not contain "
                f"{sorted(unexpected)!r} (provider-neutral fields only)"
            )
    if "schema" in spec:
        raise LakehouseDescriptorError(
            f"{source}: product {product.get('id')!r}: {group}.schema must be derived from provider contracts"
        )
    unexpected = set(spec) - {"tables"}
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: product {product.get('id')!r}: {group} must not contain {sorted(unexpected)!r} "
            "(provider-neutral fields only)"
        )


def _validate_product(product: Mapping[str, Any], *, domain_name: str, source: str) -> None:
    label = product.get("id") or f"product {product!r}"
    for field in ("id", "displayName", "description", "status"):
        if field not in product:
            raise LakehouseDescriptorError(f"{source}: product {label!r}: missing required field {field!r}")
    _identifier(product["id"], field="id", source=f"{source}: product {label!r}")
    _non_empty_string(product["displayName"], field="displayName", source=f"{source}: product {label!r}")
    _non_empty_string(product["status"], field="status", source=f"{source}: product {label!r}")
    _optional_string(product["description"], field="description", source=f"{source}: product {label!r}")
    if "domain" in product and product["domain"] != domain_name:
        raise LakehouseDescriptorError(
            f"{source}: product {label!r}: domain {product['domain']!r} must match enclosing domain {domain_name!r}"
        )
    silver_inputs = product.get("silver_inputs")
    if not isinstance(silver_inputs, list) or not silver_inputs:
        raise LakehouseDescriptorError(f"{source}: product {label!r}: silver_inputs must be a non-empty array")
    if any(not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value) for value in silver_inputs):
        raise LakehouseDescriptorError(
            f"{source}: product {label!r}: silver_inputs must contain identifiers matching '^[a-z][a-z0-9_]*$'"
        )
    if len(silver_inputs) != len(set(silver_inputs)):
        raise LakehouseDescriptorError(f"{source}: product {label!r}: silver_inputs must not contain duplicates")
    _validate_table_group(product, group="gold_tables", source=source)
    unexpected = set(product) - {
        "id", "displayName", "description", "status", "domain", "silver_inputs", "gold_tables",
    }
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: product {label!r} must not contain {sorted(unexpected)!r} (provider-neutral fields only)"
        )


def _validate_domain(domain: Mapping[str, Any], *, source: str) -> None:
    required = ("name", "displayName", "description", "status", "silver_tables", "products")
    for field in required:
        if field not in domain:
            raise LakehouseDescriptorError(f"{source}: domain: missing required field {field!r}")
    _identifier(domain["name"], field="domain.name", source=source)
    _non_empty_string(domain["displayName"], field="domain.displayName", source=source)
    _non_empty_string(domain["status"], field="domain.status", source=source)
    _optional_string(domain["description"], field="domain.description", source=source)
    _validate_silver_table_group(domain, source=source)
    products = domain["products"]
    if not isinstance(products, list):
        raise LakehouseDescriptorError(f"{source}: domain {domain['name']!r}: products must be an array")
    seen_products: set[str] = set()
    silver_names = {table["name"] for table in domain["silver_tables"]["tables"]}
    for product in products:
        if not isinstance(product, Mapping):
            raise LakehouseDescriptorError(f"{source}: domain {domain['name']!r}: products must contain objects")
        _validate_product(product, domain_name=domain["name"], source=source)
        if product["id"] in seen_products:
            raise LakehouseDescriptorError(
                f"{source}: domain {domain['name']!r}: duplicate product id {product['id']!r}"
            )
        seen_products.add(product["id"])
        unknown_inputs = sorted(set(product["silver_inputs"]) - silver_names)
        if unknown_inputs:
            raise LakehouseDescriptorError(
                f"{source}: product {product['id']!r}: silver_inputs reference unknown domain Silver tables "
                f"{unknown_inputs!r}"
            )
    unexpected = set(domain) - {"name", "displayName", "description", "status", "silver_tables", "products"}
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: domain {domain['name']!r} must not contain {sorted(unexpected)!r} (provider-neutral fields only)"
        )


def _validate_silver_table_group(domain: Mapping[str, Any], *, source: str) -> None:
    spec = domain.get("silver_tables")
    if not isinstance(spec, Mapping) or not isinstance(spec.get("tables"), list) or not spec["tables"]:
        raise LakehouseDescriptorError(f"{source}: domain {domain.get('name')!r}: silver_tables.tables must be a non-empty array")
    if set(spec) != {"tables"}:
        unexpected = sorted(set(spec) - {"tables"})
        raise LakehouseDescriptorError(
            f"{source}: domain {domain.get('name')!r}: silver_tables must not contain {unexpected!r} "
            "(provider-neutral fields only)"
        )
    seen: set[str] = set()
    for index, table in enumerate(spec["tables"]):
        if not isinstance(table, Mapping) or not _IDENTIFIER_PATTERN.fullmatch(table.get("name") or ""):
            raise LakehouseDescriptorError(f"{source}: domain {domain.get('name')!r}: silver_tables.tables[{index}].name must be an identifier")
        if table["name"] in seen:
            raise LakehouseDescriptorError(f"{source}: domain {domain.get('name')!r}: silver_tables.tables must not contain duplicate names")
        seen.add(table["name"])
        if not _IDENTIFIER_PATTERN.fullmatch(table.get("source") or "") or not _IDENTIFIER_PATTERN.fullmatch(table.get("resource") or ""):
            raise LakehouseDescriptorError(f"{source}: domain {domain.get('name')!r}: silver_tables.tables[{index}] must declare source and resource identifiers")
        if "description" in table and not isinstance(table["description"], str):
            raise LakehouseDescriptorError(f"{source}: domain {domain.get('name')!r}: silver_tables.tables[{index}].description must be a string")
        unexpected = set(table) - {"name", "source", "resource", "description"}
        if unexpected:
            raise LakehouseDescriptorError(f"{source}: domain {domain.get('name')!r}: silver_tables.tables[{index}] must not contain {sorted(unexpected)!r}")


def _validate_dashboard(dashboard: Mapping[str, Any], *, source: str) -> None:
    for field in ("name", "products"):
        if field not in dashboard:
            raise LakehouseDescriptorError(f"{source}: dashboard: missing required field {field!r}")
    _identifier(dashboard["name"], field="dashboard.name", source=source)
    products = dashboard["products"]
    if not isinstance(products, list) or not products:
        raise LakehouseDescriptorError(f"{source}: dashboard {dashboard['name']!r}: products must be a non-empty array")
    if any(not isinstance(product, str) or not _IDENTIFIER_PATTERN.fullmatch(product) for product in products):
        raise LakehouseDescriptorError(f"{source}: dashboard {dashboard['name']!r}: products must contain identifiers")
    if len(products) != len(set(products)):
        raise LakehouseDescriptorError(f"{source}: dashboard {dashboard['name']!r}: products must not contain duplicates")
    unexpected = set(dashboard) - {"name", "products"}
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: dashboard {dashboard['name']!r} must not contain {sorted(unexpected)!r} "
            "(provider-neutral fields only)"
        )


def validate_lakehouse_descriptor(
    document: Mapping[str, Any], *, source: str = "lakehouse.yaml", allow_incomplete: bool = False
) -> None:
    """Validate the v1alpha3 Lakehouse envelope, domains, and dashboards."""
    if document.get("apiVersion") != LAKEHOUSE_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: unsupported apiVersion {document.get('apiVersion')!r}; expected {LAKEHOUSE_API_VERSION!r}"
        )
    if document.get("kind") != LAKEHOUSE_KIND:
        raise LakehouseDescriptorError(f"{source}: kind must be {LAKEHOUSE_KIND!r}")
    for field in ("name", "displayName", "description", "status", "sources", "domains", "dashboards"):
        if field not in document:
            raise LakehouseDescriptorError(f"{source}: missing required field {field!r}")
    _identifier(document["name"], field="name", source=source)
    _non_empty_string(document["displayName"], field="displayName", source=source)
    _non_empty_string(document["status"], field="status", source=source)
    _optional_string(document["description"], field="description", source=source)
    sources = document["sources"]
    if not isinstance(sources, list) or (not sources and not allow_incomplete):
        raise LakehouseDescriptorError(f"{source}: sources must be a non-empty array")
    for index, source_name in enumerate(sources):
        _identifier(source_name, field=f"sources[{index}]", source=source)
    if len(sources) != len(set(sources)):
        raise LakehouseDescriptorError(f"{source}: sources must not contain duplicates")
    domains = document["domains"]
    if not isinstance(domains, list) or (not domains and not allow_incomplete):
        raise LakehouseDescriptorError(f"{source}: domains must be a non-empty array")
    seen_domains: set[str] = set()
    seen_products: set[str] = set()
    for domain in domains:
        if not isinstance(domain, Mapping):
            raise LakehouseDescriptorError(f"{source}: domains must contain objects")
        _validate_domain(domain, source=source)
        if domain["name"] in seen_domains:
            raise LakehouseDescriptorError(f"{source}: duplicate domain name {domain['name']!r}")
        seen_domains.add(domain["name"])
        for product in domain["products"]:
            if product["id"] in seen_products:
                raise LakehouseDescriptorError(
                    f"{source}: product id {product['id']!r} must be globally unique across domains"
                )
            seen_products.add(product["id"])
    if not seen_products and not allow_incomplete:
        raise LakehouseDescriptorError(
            f"{source}: lakehouse must declare at least one product across its domains "
            "(an individual domain may have zero products while it is being seeded, "
            "but the lakehouse as a whole cannot -- deployment tooling requires a "
            "default product)"
        )
    dashboards = document["dashboards"]
    if not isinstance(dashboards, list):
        raise LakehouseDescriptorError(f"{source}: dashboards must be an array")
    seen_dashboards: set[str] = set()
    for dashboard in dashboards:
        if not isinstance(dashboard, Mapping):
            raise LakehouseDescriptorError(f"{source}: dashboards must contain objects")
        _validate_dashboard(dashboard, source=source)
        if dashboard["name"] in seen_dashboards:
            raise LakehouseDescriptorError(f"{source}: duplicate dashboard name {dashboard['name']!r}")
        seen_dashboards.add(dashboard["name"])
        unknown_products = sorted(set(dashboard["products"]) - seen_products)
        if unknown_products:
            raise LakehouseDescriptorError(
                f"{source}: dashboard {dashboard['name']!r}: products reference unknown product ids "
                f"{unknown_products!r}"
            )
    unexpected = set(document) - {
        "apiVersion", "kind", "name", "displayName", "description", "status", "sources", "domains", "dashboards",
    }
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: must not contain {sorted(unexpected)!r} (provider-neutral fields only)"
        )


def load_lakehouse_descriptor(path: str | Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    """Load and validate the v1alpha3 Lakehouse descriptor at ``path``."""
    source = str(path)
    with Path(path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise LakehouseDescriptorError(f"{source}: descriptor must contain a YAML object")
    validate_lakehouse_descriptor(document, source=source, allow_incomplete=allow_incomplete)
    return document


def load_source_descriptor(path: str | Path) -> dict[str, Any]:
    """Load and validate the v1alpha3 Source descriptor at ``path``."""
    source = str(path)
    with Path(path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise LakehouseDescriptorError(f"{source}: descriptor must contain a YAML object")
    validate_source_descriptor(document, source=source)
    return document

