"""The ``Governance`` descriptor at ``lakehouse_code/governance.yml``.

It carries business intent only: domains, and the data products nested
under them. Tables belong to a domain or product by their schema
(``<domain>_silver``, ``<product>_gold``), so no table list is declared here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .descriptors import LakehouseDescriptorError, _identifier, _non_empty_string, _optional_string

GOVERNANCE_API_VERSION = "openlakeforge.io/v1beta2"
GOVERNANCE_KIND = "Governance"
_LEGACY_API_VERSION = "openlakeforge.io/v1beta1"
_ENTRY_FIELDS = {"displayName", "description", "status"}


def _check_entry(entry: object, *, id_field: str, where: str, extra: set[str]) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise LakehouseDescriptorError(f"{where}: must be an object")
    _identifier(entry.get(id_field), field=id_field, source=where)
    where = f"{where[: where.rindex('[')]}[{entry[id_field]}]"
    _non_empty_string(entry.get("displayName"), field="displayName", source=where)
    _optional_string(entry.get("description"), field="description", source=where)
    if "status" in entry:
        _non_empty_string(entry["status"], field="status", source=where)
    unexpected = sorted(set(entry) - _ENTRY_FIELDS - extra - {id_field})
    if unexpected:
        hint = ""
        if "domain" in unexpected:
            hint = "; a product belongs to the domain it is nested under, so `domain` is not declared"
        raise LakehouseDescriptorError(f"{where}: must not contain {unexpected!r}{hint}")
    return entry


def governance_products(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every product of a validated document, in domain order."""
    return [product for domain in document["domains"] for product in domain.get("products", [])]


def validate_governance_descriptor(document: Mapping[str, Any], *, source: str = "governance.yml") -> None:
    """Validate the envelope, the domains and the products nested under them.

    Product ids are unique across all domains: the ``<product>_gold`` schema
    does not carry its domain.
    """
    api_version = document.get("apiVersion")
    if api_version == _LEGACY_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: apiVersion {_LEGACY_API_VERSION!r} declares a flat root `products:` list; "
            f"run `olf project migrate` to nest products under their domains as {GOVERNANCE_API_VERSION!r}"
        )
    if api_version != GOVERNANCE_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: unsupported apiVersion {api_version!r}; expected {GOVERNANCE_API_VERSION!r}"
        )
    if document.get("kind") != GOVERNANCE_KIND:
        raise LakehouseDescriptorError(f"{source}: kind must be {GOVERNANCE_KIND!r}")
    if "products" in document:
        raise LakehouseDescriptorError(
            f"{source}: root `products` is not part of {GOVERNANCE_API_VERSION!r}; declare each product under "
            "`domains[].products`"
        )
    unexpected = sorted(set(document) - {"apiVersion", "kind", "domains"})
    if unexpected:
        raise LakehouseDescriptorError(f"{source}: must not contain {unexpected!r}")
    domains = document.get("domains")
    if not isinstance(domains, list) or not domains:
        raise LakehouseDescriptorError(f"{source}: domains must be a non-empty array")
    seen_domains: set[str] = set()
    owner_of: dict[str, str] = {}
    for domain in domains:
        entry = _check_entry(domain, id_field="name", where=f"{source}: domains[]", extra={"products"})
        name = entry["name"]
        if name in seen_domains:
            raise LakehouseDescriptorError(f"{source}: duplicate name {name!r} in domains")
        seen_domains.add(name)
        # A domain may exist before its first product (`olf domain new`).
        products = entry.get("products", [])
        if not isinstance(products, list):
            raise LakehouseDescriptorError(f"{source}: domains[{name}].products must be an array")
        for product in products:
            where = f"{source}: domains[{name}].products[]"
            checked = _check_entry(product, id_field="id", where=where, extra=set())
            if checked["id"] in owner_of:
                raise LakehouseDescriptorError(
                    f"{source}: duplicate id {checked['id']!r} in domains[{name}].products[{checked['id']}]; "
                    f"already declared under domain {owner_of[checked['id']]!r}"
                )
            owner_of[checked["id"]] = name


def load_governance_lakehouse_document(path: str | Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    """``governance.yml`` as the document shape the inventory loader consumes.

    Sources are the Bronze source directories and dashboards the Superset report
    directories, so neither is declared twice. A dashboard serves the products
    whose ``<product>_gold`` schema its datasets read.
    """
    code = Path(path).parent
    # An `olf init --empty` project has no governance.yml until its first domain.
    if allow_incomplete and not Path(path).is_file():
        document: Mapping[str, Any] = {"domains": []}
    else:
        document = load_governance_descriptor(path)
    products = governance_products(document)
    if not products and not allow_incomplete:
        raise LakehouseDescriptorError(f"{path}: governance.yml must declare at least one product")
    dashboards = []
    for metadata in sorted(code.glob("dashboards/superset/*/metadata.yaml")):
        schemas = {
            yaml.safe_load(dataset.read_text(encoding="utf-8")).get("schema")
            for dataset in (metadata.parent / "datasets").rglob("*")
            if dataset.suffix in {".yaml", ".yml"}
        }
        served = [p["id"] for p in products if f"{p['id']}_gold" in schemas]
        dashboards.append({"name": metadata.parent.name, "products": served})
    return {
        "name": "openlakeforge",
        "sources": sorted(p.parent.name for p in code.glob("bronze/*/source.yaml")),
        "domains": [
            {**domain, "products": [dict(p) for p in domain.get("products", [])]}
            for domain in document["domains"]
        ],
        "dashboards": dashboards,
    }


def load_governance_descriptor(path: str | Path) -> dict[str, Any]:
    """Load and validate the Governance descriptor at ``path``."""
    source = str(path)
    with Path(path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise LakehouseDescriptorError(f"{source}: descriptor must contain a YAML object")
    validate_governance_descriptor(document, source=source)
    return document
