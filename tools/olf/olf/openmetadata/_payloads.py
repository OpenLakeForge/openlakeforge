"""Domain and data-product payload construction for OpenMetadata governance
metadata upserts.
"""

from __future__ import annotations

from collections.abc import Iterator

from olf.clients.openmetadata import OpenMetadataError


def display_name_from_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("-", " ").replace("_", " ").split())


def domain_description(domain: dict) -> str:
    parts = []
    if domain.get("description"):
        parts.append(str(domain["description"]))
    if domain.get("status"):
        parts.append(f"Status: {domain['status']}")
    medallion = domain.get("medallion")
    if isinstance(medallion, dict) and medallion:
        parts.append("Medallion layers:")
        for layer, config in medallion.items():
            if isinstance(config, dict):
                owner = config.get("owner", "unknown")
                description = config.get("description", "")
                parts.append(f"- {layer}: {description} Owner: {owner}.")
    return "\n".join(parts)


def domain_payload(domain: dict) -> dict:
    name = domain.get("name")
    if not name:
        raise OpenMetadataError("OpenMetadata domain metadata is missing required field 'name'.")
    payload = {
        "name": name,
        "displayName": domain.get("displayName") or domain.get("display_name") or display_name_from_name(name),
        "domainType": domain.get("domainType") or domain.get("domain_type") or "Source-aligned",
        "description": domain_description(domain),
    }
    for optional in ["owners", "experts", "reviewers", "tags", "style", "extension"]:
        if domain.get(optional):
            payload[optional] = domain[optional]
    return payload


def product_payload(product: dict) -> dict:
    name = product.get("name")
    if not name:
        raise OpenMetadataError("OpenMetadata data-product metadata is missing required field 'name'.")
    domains = product.get("domains")
    if domains is None and product.get("domain"):
        domains = [product["domain"]]
    if not domains:
        raise OpenMetadataError(f"OpenMetadata data product '{name}' must define 'domain' or 'domains'.")
    payload = {
        "name": name,
        "displayName": product.get("displayName") or product.get("display_name") or display_name_from_name(name),
        "description": product.get("description") or "",
        "domains": domains,
    }
    for optional in ["owners", "experts", "reviewers", "tags", "style", "extension"]:
        if product.get(optional):
            payload[optional] = product[optional]
    return payload


def product_entries(domain: dict) -> Iterator[dict]:
    products = domain.get("data_products") or []
    if not isinstance(products, list):
        raise OpenMetadataError(f"Domain '{domain.get('name', '<unknown>')}' data_products must be a list.")
    for product in products:
        if not isinstance(product, dict):
            raise OpenMetadataError(f"Unsupported data product entry in domain '{domain.get('name')}': {product!r}")
        normalized = dict(product)
        if not normalized.get("name"):
            normalized["name"] = f"{domain['name']}_{normalized.get('id', '')}".rstrip("_")
        if not normalized.get("domain") and not normalized.get("domains"):
            normalized["domain"] = domain["name"]
        yield normalized


def product_contract_key(product: dict) -> str:
    return product.get("asset_prefix") or product.get("name") or product.get("id") or ""
