"""The ``Governance`` descriptor at ``lakehouse_code/governance.yml``.

It carries business intent only: domains, their data products and metadata.
Tables, source wiring and dashboards are not declared here; they are derived
from the Floe, dbt, Dagster and Superset code that owns them (``derive.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .descriptors import LakehouseDescriptorError, _identifier, _non_empty_string, _optional_string

GOVERNANCE_API_VERSION = "openlakeforge.io/v1beta1"
GOVERNANCE_KIND = "Governance"
# Fields of the retired Lakehouse descriptor. Naming them in the error tells an
# author migrating a lakehouse.yaml that the information moved to code.
_DERIVED_FIELDS = ("sources", "silver_tables", "silver_inputs", "gold_tables", "dashboards")


def _reject_unexpected(entry: Mapping[str, Any], allowed: set[str], *, where: str) -> None:
    unexpected = sorted(set(entry) - allowed)
    if not unexpected:
        return
    derived = [name for name in unexpected if name in _DERIVED_FIELDS]
    hint = f"; {derived!r} are derived from project code, not declared" if derived else ""
    raise LakehouseDescriptorError(f"{where}: must not contain {unexpected!r}{hint}")


def _validate_product(product: object, *, domain: str, source: str) -> str:
    if not isinstance(product, Mapping):
        raise LakehouseDescriptorError(f"{source}: domain {domain!r}: products must contain objects")
    where = f"{source}: product {product.get('id')!r}"
    for field in ("id", "displayName"):
        if field not in product:
            raise LakehouseDescriptorError(f"{where}: missing required field {field!r}")
    _identifier(product["id"], field="id", source=where)
    _non_empty_string(product["displayName"], field="displayName", source=where)
    _optional_string(product.get("description"), field="description", source=where)
    if "status" in product:
        _non_empty_string(product["status"], field="status", source=where)
    _reject_unexpected(product, {"id", "displayName", "description", "status"}, where=where)
    return str(product["id"])


def _validate_domain(domain: object, *, source: str) -> list[str]:
    if not isinstance(domain, Mapping):
        raise LakehouseDescriptorError(f"{source}: domains must contain objects")
    where = f"{source}: domain {domain.get('name')!r}"
    for field in ("name", "displayName", "products"):
        if field not in domain:
            raise LakehouseDescriptorError(f"{where}: missing required field {field!r}")
    _identifier(domain["name"], field="name", source=where)
    _non_empty_string(domain["displayName"], field="displayName", source=where)
    _optional_string(domain.get("description"), field="description", source=where)
    if "status" in domain:
        _non_empty_string(domain["status"], field="status", source=where)
    if not isinstance(domain["products"], list):
        raise LakehouseDescriptorError(f"{where}: products must be an array")
    _reject_unexpected(domain, {"name", "displayName", "description", "status", "products"}, where=where)
    return [_validate_product(product, domain=domain["name"], source=source) for product in domain["products"]]


def validate_governance_descriptor(
    document: Mapping[str, Any], *, source: str = "governance.yml", allow_incomplete: bool = False
) -> None:
    """Validate the Governance envelope, its domains and their products.

    `allow_incomplete` waives only the cardinality an `olf init --empty`
    project has not reached yet: at least one domain and one product.
    """
    if document.get("apiVersion") != GOVERNANCE_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: unsupported apiVersion {document.get('apiVersion')!r}; expected {GOVERNANCE_API_VERSION!r}"
        )
    if document.get("kind") != GOVERNANCE_KIND:
        raise LakehouseDescriptorError(f"{source}: kind must be {GOVERNANCE_KIND!r}")
    if "domains" not in document:
        raise LakehouseDescriptorError(f"{source}: missing required field 'domains'")
    _reject_unexpected(document, {"apiVersion", "kind", "domains"}, where=source)
    domains = document["domains"]
    if not isinstance(domains, list) or (not domains and not allow_incomplete):
        raise LakehouseDescriptorError(f"{source}: domains must be a non-empty array")
    domain_names: set[str] = set()
    product_ids: set[str] = set()
    for domain in domains:
        ids = _validate_domain(domain, source=source)
        if domain["name"] in domain_names:
            raise LakehouseDescriptorError(f"{source}: duplicate domain name {domain['name']!r}")
        domain_names.add(domain["name"])
        for product_id in ids:
            if product_id in product_ids:
                raise LakehouseDescriptorError(
                    f"{source}: product id {product_id!r} must be globally unique across domains"
                )
            product_ids.add(product_id)
    if not product_ids and not allow_incomplete:
        raise LakehouseDescriptorError(f"{source}: governance must declare at least one product")


def load_governance_descriptor(path: str | Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    """Load and validate the Governance descriptor at ``path``."""
    source = str(path)
    with Path(path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise LakehouseDescriptorError(f"{source}: descriptor must contain a YAML object")
    validate_governance_descriptor(document, source=source, allow_incomplete=allow_incomplete)
    return document
