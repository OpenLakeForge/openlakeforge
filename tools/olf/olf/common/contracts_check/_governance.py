"""`lakehouse_code/governance.yml` against the Silver and Gold code it governs."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml
from openlakeforge_core import (
    LakehouseDescriptorError,
    governance_products,
    load_governance_descriptor,
    scan_gold_roles,
    scan_product_dependencies,
)
from openlakeforge_core.governance import resolve_product

from olf.common.contracts_check._report import CheckResult


def _dirs(path: Path) -> set[str]:
    if not path.is_dir():
        return set()
    return {p.name for p in path.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}


def _out_of_schema_models(project: Path) -> list[str]:
    """Models dbt itself resolved outside their folder's `<product>_gold` (`dbt parse` writes the manifest)."""
    manifest_path = project / "target" / "manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    project_name = manifest.get("metadata", {}).get("project_name")
    return [
        f"dbt model {node['name']!r} of product {node['fqn'][1]!r} lands in {node['schema']!r}, "
        f"not {node['fqn'][1]}_gold"
        for node in manifest.get("nodes", {}).values()
        if node["resource_type"] == "model"
        and node.get("package_name") == project_name
        and len(node["fqn"]) > 2
        and node["config"]["materialized"] != "ephemeral"
        and node["schema"] != f"{node['fqn'][1]}_gold"
    ]


def _check_governance_conformance(
    repo_root: Path, *, schema_root: Path | None = None, strict: bool = False
) -> CheckResult:
    """When `governance.yml` exists, it must match its JSON Schema and the code:
    one governance domain per `silver/<domain>/`, one product per `gold/models/<product>/`,
    and no dbt model outside `<product>_gold`. With `strict`, every product must also resolve an owner (its own or
    its domain's), a tier and a lifecycle. Offline; nothing reads a cluster."""
    name = "governance_conformance"
    code = repo_root / "lakehouse_code"
    path = code / "governance.yml"
    if not path.is_file():
        return CheckResult(name, ok=True, detail="no governance.yml")
    schema_path = (schema_root or repo_root / "docs/schema") / "governance.schema.json"
    validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    errors = [
        f"schema violation at {'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(yaml.safe_load(path.read_text(encoding="utf-8")))
    ]
    try:
        document = load_governance_descriptor(path)
    except LakehouseDescriptorError as exc:
        return CheckResult(name, ok=False, detail="; ".join([*errors, str(exc)]))
    for kind, declared, found in (
        ("domain", {d["name"] for d in document["domains"]}, _dirs(code / "silver")),
        ("product", {p["id"] for p in governance_products(document)}, _dirs(code / "gold" / "models")),
    ):
        errors += [f"{kind} {n!r} has no lakehouse_code directory" for n in sorted(declared - found)]
        errors += [f"{kind} {n!r} has a directory but is not in governance.yml" for n in sorted(found - declared)]
    if strict:
        for domain in document["domains"]:
            for product in domain.get("products", []):
                resolved = resolve_product(domain, product)
                missing = [f for f in ("owner", "tier", "lifecycle") if f not in resolved]
                if missing:
                    errors.append(f"product {product['id']!r} is missing {', '.join(missing)} (--strict)")
    errors += _out_of_schema_models(code / "gold")
    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail="governance.yml matches project code")


def _check_product_dependencies(repo_root: Path) -> CheckResult:
    """A `ref()` across products must reach a model with `access: public`; the products it joins are reported.

    A cycle between products is refused too: every product's job reads its producers' tables, so a cycle has no
    run order. Offline, from the SQL and `schema.yml` files.
    """
    name = "product_dependencies"
    code = repo_root / "lakehouse_code"
    path = code / "governance.yml"
    if not path.is_file():
        return CheckResult(name, ok=True, detail="no governance.yml")
    try:
        products = [p["id"] for p in governance_products(load_governance_descriptor(path))]
    except LakehouseDescriptorError:
        # The governance check reports the broken descriptor.
        return CheckResult(name, ok=True, detail="governance.yml unreadable; see governance_conformance")
    found = scan_product_dependencies(code / "gold", products)
    if found.violations:
        return CheckResult(name, ok=False, detail="; ".join(found.violations))
    if not found.dependencies:
        return CheckResult(name, ok=True, detail="no product reads another product's models")
    edges = ", ".join(f"{d.consumer} -> {d.producer} ({', '.join(d.models)})" for d in found.dependencies)
    return CheckResult(name, ok=True, detail=f"product dependencies: {edges}")


def _check_gold_roles(repo_root: Path) -> CheckResult:
    """Every Gold model resolves a role (`fct_`, `dim_`, `mart_` or `meta.openlakeforge.role`): the deploy tags
    each Gold table with it, and agents and the catalog tell facts and dimensions from presentation marts by it."""
    name = "gold_roles"
    code = repo_root / "lakehouse_code"
    path = code / "governance.yml"
    if not path.is_file():
        return CheckResult(name, ok=True, detail="no governance.yml")
    try:
        products = [p["id"] for p in governance_products(load_governance_descriptor(path))]
    except LakehouseDescriptorError:
        return CheckResult(name, ok=True, detail="governance.yml unreadable; see governance_conformance")
    roles, problems = scan_gold_roles(code / "gold", products)
    if problems:
        return CheckResult(name, ok=False, detail="; ".join(problems))
    return CheckResult(name, ok=True, detail=f"{sum(len(models) for models in roles.values())} Gold models have a role")
