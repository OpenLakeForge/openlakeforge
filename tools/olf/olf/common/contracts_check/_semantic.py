"""`lakehouse_code/semantic/<product>/*.ossie.yaml` against the Ossie schema, its subset, and the Gold dbt code."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml
from openlakeforge_core import LakehouseDescriptorError, governance_products, load_governance_descriptor
from openlakeforge_core import scan_product_dependencies as scan_dependencies
from openlakeforge_core.semantic import SEMANTIC_SUFFIX, load_ossie_document
from openlakeforge_core.semantic_validate import load_semantic_inventory

from olf.common.contracts_check._report import CheckResult

SCHEMA_FILE = "ossie-0.1.1.schema.json"


def _schema_errors(schema_root: Path, code: Path) -> list[str]:
    """JSON Schema violations of the vendored Ossie 0.1.1 schema, each with the line it sits on."""
    validator = jsonschema.Draft202012Validator(json.loads((schema_root / SCHEMA_FILE).read_text(encoding="utf-8")))
    errors: list[str] = []
    for path in sorted((code / "semantic").glob(f"*/*{SEMANTIC_SUFFIX}")):
        try:
            document, lines, _ = load_ossie_document(path)
        except yaml.YAMLError:
            continue  # reported by the parser with its own line
        shown = path.relative_to(code.parent).as_posix()
        for error in validator.iter_errors(document):
            where = tuple(error.absolute_path)
            line = next((lines[where[:end]] for end in range(len(where), -1, -1) if where[:end] in lines), 1)
            at = "/".join(str(part) for part in where) or "<root>"
            errors.append(f"{shown}:{line}: [schema] {at}: {error.message}")
    return errors


def _check_semantic_models(repo_root: Path, *, schema_root: Path | None = None) -> CheckResult:
    """Ossie models must match their schema, the subset rules and the product's dbt properties. Offline."""
    name = "semantic_models"
    code = repo_root / "lakehouse_code"
    if not (code / "semantic").is_dir():
        return CheckResult(name, ok=True, detail="no lakehouse_code/semantic")
    try:
        products = [p["id"] for p in governance_products(load_governance_descriptor(code / "governance.yml"))]
    except (LakehouseDescriptorError, OSError):
        return CheckResult(name, ok=True, detail="governance.yml unreadable; see governance_conformance")
    errors = _schema_errors(schema_root or repo_root / "docs/schema", code)
    inventory = load_semantic_inventory(code, products, scan_dependencies(code / "gold", products))
    errors += [str(d) for d in inventory.diagnostics if d.severity == "error"]
    warnings = [str(d) for d in inventory.diagnostics if d.severity == "warning"]
    if errors:
        return CheckResult(name, ok=False, detail="; ".join([*errors, *warnings]))
    models = ", ".join(f"{m.product}/{m.name}" for m in inventory.models) or "none"
    detail = f"semantic models: {models}"
    return CheckResult(name, ok=True, detail="; ".join([detail, *(f"warning {w}" for w in warnings)]))
