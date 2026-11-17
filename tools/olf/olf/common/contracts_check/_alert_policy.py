"""`lakehouse_code/alerts.yml` against its JSON Schema, the typed model and `governance.yml`."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml
from openlakeforge_core import (
    LakehouseDescriptorError,
    load_alert_policy,
    load_governance_descriptor,
)

from olf.common.contracts_check._report import CheckResult


def _check_alert_policy_conformance(repo_root: Path, *, schema_root: Path | None = None) -> CheckResult:
    """A project without `alerts.yml` is valid: nothing alerts unless declared. Receiver names are not
    checked, because only activation has the profile; stage names are checked against the vocabulary."""
    name = "alert_policy_conformance"
    code = repo_root / "lakehouse_code"
    path = code / "alerts.yml"
    if not path.is_file():
        return CheckResult(name, ok=True, detail="no alerts.yml")
    schema_path = (schema_root or repo_root / "docs/schema") / "alert-policy.schema.json"
    validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return CheckResult(name, ok=False, detail=f"alerts.yml: {exc}")
    errors = [
        f"schema violation at {'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(document)
    ]
    # The model assumes the document's shape, so it only runs on one the schema accepted.
    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    governance_path = code / "governance.yml"
    try:
        governance = load_governance_descriptor(governance_path) if governance_path.is_file() else None
        load_alert_policy(path, governance)
    except LakehouseDescriptorError as exc:
        return CheckResult(name, ok=False, detail=str(exc))
    return CheckResult(name, ok=True, detail="alerts.yml valid")
