"""The ``Governance`` descriptor at ``lakehouse_code/governance.yml``.

It carries business intent only: domains, and the data products nested
under them. Tables belong to a domain or product by their schema
(``<domain>_silver``, ``<product>_gold``), so no table list is declared here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .descriptors import LakehouseDescriptorError, _identifier, _non_empty_string, _optional_string
from .sla import parse_sla

GOVERNANCE_API_VERSION = "openlakeforge.io/v1beta2"
GOVERNANCE_KIND = "Governance"
_LEGACY_API_VERSION = "openlakeforge.io/v1beta1"
# The enums below are the OpenMetadata 1.13.6 ones (`entity/domains/domain.json`,
# `dataProduct.json`), so the deployer publishes values without translating them.
DOMAIN_TYPES = ("Source-aligned", "Consumer-aligned", "Aggregate")
PRODUCT_TYPES = (
    "RAW_DATA",
    "DERIVED_DATA",
    "DATASET",
    "REPORTS",
    "ANALYTIC_VIEW",
    "VISUALISATION_3D",
    "ALGORITHM",
    "DECISION_SUPPORT",
    "AUTOMATED_DECISION_MAKING",
    "DATA_ENHANCED_PRODUCT",
    "DATA_DRIVEN_SERVICE",
    "DATA_ENABLED_PERFORMANCE",
    "BI_DIRECTIONAL",
)
LIFECYCLES = ("IDEATION", "DESIGN", "DEVELOPMENT", "TESTING", "PRODUCTION", "DEPRECATED", "RETIRED")
TIERS = ("Tier1", "Tier2", "Tier3", "Tier4", "Tier5")
CLASSIFICATIONS = ("Public", "Internal", "Confidential", "Restricted")
REF_TYPES = ("user", "team")
# `<glossary>.<Term>`; the glossary part is a descriptor identifier, the term part has no `.`.
TERM_REF_PATTERN = r"^[a-z][a-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$"

_COMMON_FIELDS = {"displayName", "description", "owner", "experts", "classification", "glossaryTerms"}
_DOMAIN_FIELDS = _COMMON_FIELDS | {"domainType", "products"}
_PRODUCT_FIELDS = _COMMON_FIELDS | {"reviewers", "type", "tier", "lifecycle", "termsOfUse", "profiling", "sla"}
_INHERITED = ("owner", "experts", "classification")


@dataclass(frozen=True)
class EntityRef:
    """A reference to an existing OpenMetadata user or team; never a new one."""

    type: str
    ref: str


def _enum(value: object, *, field: str, allowed: tuple[str, ...], where: str) -> None:
    if value not in allowed:
        raise LakehouseDescriptorError(f"{where}: {field} must be one of {list(allowed)!r}, got {value!r}")


def _entity_ref(value: object, *, field: str, where: str) -> EntityRef:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"type", "ref"}
        or value["type"] not in REF_TYPES
        or not isinstance(value["ref"], str)
        or not value["ref"].strip()
    ):
        raise LakehouseDescriptorError(
            f"{where}: {field} must be an object {{type: user|team, ref: <name>}}, got {value!r}"
        )
    return EntityRef(value["type"], value["ref"])


def _entity_refs(value: object, *, field: str, where: str) -> tuple[EntityRef, ...]:
    if not isinstance(value, list) or not value:
        raise LakehouseDescriptorError(f"{where}: {field} must be a non-empty array of {{type, ref}} objects")
    return tuple(_entity_ref(item, field=f"{field}[{index}]", where=where) for index, item in enumerate(value))


def parse_term_refs(value: object, *, field: str, where: str) -> tuple[str, ...]:
    """A non-empty array of ``<glossary>.<Term>`` references; whether they resolve is checked elsewhere."""
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise LakehouseDescriptorError(f"{where}: {field} must be a non-empty array of non-empty strings")
    for index, ref in enumerate(value):
        if not re.fullmatch(TERM_REF_PATTERN, ref):
            raise LakehouseDescriptorError(f"{where}: {field}[{index}] must look like '<glossary>.<Term>', got {ref!r}")
    if len(set(value)) != len(value):
        raise LakehouseDescriptorError(f"{where}: {field} lists a term twice")
    return tuple(value)


def _profiling(value: object, *, where: str) -> None:
    if not isinstance(value, Mapping) or set(value) - {"enabled"} or not isinstance(value.get("enabled"), bool):
        raise LakehouseDescriptorError(f"{where}: profiling must be an object {{enabled: true|false}}, got {value!r}")


def _terms_of_use(value: object, *, where: str) -> None:
    if not isinstance(value, Mapping) or not value or set(value) - {"allowed", "prohibited"}:
        raise LakehouseDescriptorError(f"{where}: termsOfUse must be an object with `allowed` and/or `prohibited`")
    for key, items in value.items():
        if not isinstance(items, list) or not items or not all(isinstance(i, str) and i.strip() for i in items):
            raise LakehouseDescriptorError(f"{where}: termsOfUse.{key} must be a non-empty array of strings")


def _check_entry(entry: object, *, id_field: str, where: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise LakehouseDescriptorError(f"{where}: must be an object")
    _identifier(entry.get(id_field), field=id_field, source=where)
    where = f"{where[: where.rindex('[')]}[{entry[id_field]}]"
    _non_empty_string(entry.get("displayName"), field="displayName", source=where)
    _optional_string(entry.get("description"), field="description", source=where)
    if "status" in entry:
        raise LakehouseDescriptorError(
            f"{where}: `status` is not part of {GOVERNANCE_API_VERSION!r}; "
            + (
                "use `lifecycle` (" + ", ".join(LIFECYCLES) + ")"
                if id_field == "id"
                else "domains have no lifecycle, so remove it"
            )
        )
    unexpected = sorted(set(entry) - fields - {id_field})
    if unexpected:
        hint = ""
        if "domain" in unexpected:
            hint = "; a product belongs to the domain it is nested under, so `domain` is not declared"
        raise LakehouseDescriptorError(f"{where}: must not contain {unexpected!r}{hint}")
    if "owner" in entry:
        _entity_ref(entry["owner"], field="owner", where=where)
    for field in ("experts", "reviewers"):
        if field in entry:
            _entity_refs(entry[field], field=field, where=where)
    if "classification" in entry:
        _enum(entry["classification"], field="classification", allowed=CLASSIFICATIONS, where=where)
    for field, allowed in (
        ("domainType", DOMAIN_TYPES),
        ("type", PRODUCT_TYPES),
        ("tier", TIERS),
        ("lifecycle", LIFECYCLES),
    ):
        if field in entry:
            _enum(entry[field], field=field, allowed=allowed, where=where)
    if "termsOfUse" in entry:
        _terms_of_use(entry["termsOfUse"], where=where)
    if "profiling" in entry:
        _profiling(entry["profiling"], where=where)
    if "sla" in entry:
        parse_sla(entry["sla"], where=where)
    if "glossaryTerms" in entry:
        parse_term_refs(entry["glossaryTerms"], field="glossaryTerms", where=where)
    return entry


def resolve_product(domain: Mapping[str, Any], product: Mapping[str, Any]) -> dict[str, Any]:
    """The product with `owner`, `experts` and `classification` inherited from its domain unless it sets them.

    `tier`, `lifecycle` and `type` do not inherit: a domain is not a tier.
    """
    inherited = {field: domain[field] for field in _INHERITED if field in domain and field not in product}
    return {**inherited, **product}


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
        entry = _check_entry(domain, id_field="name", where=f"{source}: domains[]", fields=_DOMAIN_FIELDS)
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
            checked = _check_entry(product, id_field="id", where=where, fields=_PRODUCT_FIELDS)
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
            {**domain, "products": [resolve_product(domain, p) for p in domain.get("products", [])]}
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
