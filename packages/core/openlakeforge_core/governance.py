"""The ``Governance`` descriptor at ``lakehouse_code/governance.yml``.

It carries business intent only: domains, and data products naming their
domain. Tables belong to a domain or product by their schema
(``<domain>_silver``, ``<product>_gold``), so no table list is declared here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .descriptors import LakehouseDescriptorError, _identifier, _non_empty_string, _optional_string

GOVERNANCE_API_VERSION = "openlakeforge.io/v1beta1"
GOVERNANCE_KIND = "Governance"
_ENTRY_FIELDS = {"displayName", "description", "status"}


def _entries(document: Mapping[str, Any], key: str, id_field: str, extra: set[str], source: str) -> list[Mapping[str, Any]]:
    entries = document.get(key)
    if not isinstance(entries, list):
        raise LakehouseDescriptorError(f"{source}: {key} must be an array")
    # A domain may exist before its first product (`olf domain new`).
    if not entries and key == "domains":
        raise LakehouseDescriptorError(f"{source}: domains must be a non-empty array")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise LakehouseDescriptorError(f"{source}: {key} must contain objects")
        where = f"{source}: {key} entry {entry.get(id_field)!r}"
        _identifier(entry.get(id_field), field=id_field, source=where)
        _non_empty_string(entry.get("displayName"), field="displayName", source=where)
        _optional_string(entry.get("description"), field="description", source=where)
        if "status" in entry:
            _non_empty_string(entry["status"], field="status", source=where)
        unexpected = sorted(set(entry) - _ENTRY_FIELDS - extra - {id_field})
        if unexpected:
            raise LakehouseDescriptorError(f"{where}: must not contain {unexpected!r}")
        if entry[id_field] in seen:
            raise LakehouseDescriptorError(f"{source}: duplicate {id_field} {entry[id_field]!r} in {key}")
        seen.add(entry[id_field])
    return entries


def validate_governance_descriptor(document: Mapping[str, Any], *, source: str = "governance.yml") -> None:
    """Validate the envelope, the domains, the products and each product's domain."""
    if document.get("apiVersion") != GOVERNANCE_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: unsupported apiVersion {document.get('apiVersion')!r}; expected {GOVERNANCE_API_VERSION!r}"
        )
    if document.get("kind") != GOVERNANCE_KIND:
        raise LakehouseDescriptorError(f"{source}: kind must be {GOVERNANCE_KIND!r}")
    unexpected = sorted(set(document) - {"apiVersion", "kind", "domains", "products"})
    if unexpected:
        raise LakehouseDescriptorError(f"{source}: must not contain {unexpected!r}")
    domains = {d["name"] for d in _entries(document, "domains", "name", set(), source)}
    for product in _entries(document, "products", "id", {"domain"}, source):
        _identifier(product.get("domain"), field="domain", source=f"{source}: product {product['id']!r}")
        if product["domain"] not in domains:
            raise LakehouseDescriptorError(
                f"{source}: product {product['id']!r} names unknown domain {product.get('domain')!r}"
            )


def _without_domain(product: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in product.items() if key != "domain"}


def load_governance_lakehouse_document(path: str | Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    """``governance.yml`` as the document shape the inventory loader consumes.

    Sources are the Bronze source directories and dashboards the Superset report
    directories, so neither is declared twice. A dashboard serves the products
    whose ``<product>_gold`` schema its datasets read.
    """
    code = Path(path).parent
    # An `olf init --empty` project has no governance.yml until its first domain.
    if allow_incomplete and not Path(path).is_file():
        document: Mapping[str, Any] = {"domains": [], "products": []}
    else:
        document = load_governance_descriptor(path)
    products = document["products"]
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
            {**domain, "products": [_without_domain(p) for p in products if p["domain"] == domain["name"]]}
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
