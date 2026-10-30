"""`lakehouse_code/governance.yml` against the Silver and Gold code it governs."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml
from openlakeforge_domain import LakehouseDescriptorError, load_governance_descriptor

from olf.common.contracts_check._report import CheckResult


def _dirs(path: Path) -> set[str]:
    if not path.is_dir():
        return set()
    return {p.name for p in path.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}


def _out_of_schema_models(project: Path, product: str) -> list[str]:
    """Models dbt itself resolved outside `<product>_gold` (`dbt parse` writes the manifest)."""
    manifest = project / "target" / "manifest.json"
    if not manifest.is_file():
        return []
    nodes = json.loads(manifest.read_text(encoding="utf-8")).get("nodes", {}).values()
    return [
        f"dbt model {node['name']!r} of product {product!r} lands in {node['schema']!r}, not {product}_gold"
        for node in nodes
        if node["resource_type"] == "model"
        and node["config"]["materialized"] != "ephemeral"
        and node["schema"] != f"{product}_gold"
    ]


def _check_governance_conformance(repo_root: Path, *, schema_root: Path | None = None) -> CheckResult:
    """When `governance.yml` exists, it must match its JSON Schema and the code:
    one governance domain per `silver/<domain>/`, one product per `gold/<product>/`,
    and no dbt model outside `<product>_gold`. Offline; nothing reads a cluster."""
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
        ("product", {p["id"] for p in document["products"]}, _dirs(code / "gold")),
    ):
        errors += [f"{kind} {n!r} has no lakehouse_code directory" for n in sorted(declared - found)]
        errors += [f"{kind} {n!r} has a directory but is not in governance.yml" for n in sorted(found - declared)]
    for product in sorted(_dirs(code / "gold")):
        errors += _out_of_schema_models(code / "gold" / product / "dbt", product)
    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail="governance.yml matches project code")
