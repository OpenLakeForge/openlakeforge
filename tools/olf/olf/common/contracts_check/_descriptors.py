"""Descriptor and Deployment Profile checks: canonical model plus JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml
from openlakeforge_domain import (
    LakehouseDescriptorError,
    load_governance_descriptor,
    load_source_descriptor,
)

from olf.common.contracts_check._report import CheckResult

_SCHEMA_BY_KIND = {
    "governance": "docs/schema/governance.schema.json",
    "source": "docs/schema/source.schema.json",
}


def descriptor_schema_errors(
    repo_root: Path, *, schema_root: Path | None = None, allow_incomplete: bool = False
) -> list[str]:
    """Validate `lakehouse_code/governance.yml` and every
    `lakehouse_code/bronze/*/source.yaml` against the canonical model and
    versioned JSON Schema. The two validators run independently; neither one
    substitutes for the other. Returns an empty list when everything is
    valid. Shared by `olf contracts check` and the `olf source|domain|product
    new` scaffold engine's pre-commit verification.

    `schema_root` points at the directory holding the versioned JSON Schemas
    when they live outside `repo_root` — an installed distribution keeps them
    in its immutable payload while the descriptors live in the user project.
    `allow_incomplete` accepts an `olf init --empty` project, which has no
    `governance.yml` until its first domain."""
    governance_path = repo_root / "lakehouse_code" / "governance.yml"
    source_paths = sorted((repo_root / "lakehouse_code" / "bronze").glob("*/source.yaml"))
    descriptor_paths = [*([governance_path] if governance_path.is_file() else []), *source_paths]
    if not governance_path.is_file() and not allow_incomplete:
        return [f"no governance.yml found under {repo_root / 'lakehouse_code'}"]

    errors: list[str] = []
    for descriptor_path in descriptor_paths:
        rel_path = descriptor_path.relative_to(repo_root)
        kind = "governance" if descriptor_path == governance_path else "source"
        try:
            if kind == "governance":
                document = load_governance_descriptor(descriptor_path)
            else:
                document = load_source_descriptor(descriptor_path)
        except LakehouseDescriptorError as exc:
            errors.append(f"{rel_path}: canonical model rejected descriptor: {exc}")
            continue

        schema_relpath = _SCHEMA_BY_KIND[kind]
        schema_path = schema_root / Path(schema_relpath).name if schema_root is not None else repo_root / schema_relpath
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        schema_errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
        for error in schema_errors:
            location = "/".join(str(part) for part in error.absolute_path) or "<root>"
            errors.append(f"{rel_path}: schema violation at {location}: {error.message}")

    return errors


def _check_descriptor_schema_conformance(repo_root: Path, *, schema_root: Path | None = None) -> CheckResult:
    """The `lakehouse_code/governance.yml` descriptor and every
    `lakehouse_code/bronze/*/source.yaml` must load via the canonical model
    and conform to its versioned JSON Schema. The two validators run
    independently; neither one substitutes for the other.

    `schema_root` points at the distribution payload's `docs/schema/` for an
    installed project, whose `lakehouse_code/` has no `docs/schema/` of its
    own (ADR 0009)."""
    name = "descriptor_schema_conformance"
    governance_path = repo_root / "lakehouse_code" / "governance.yml"
    source_paths = sorted((repo_root / "lakehouse_code" / "bronze").glob("*/source.yaml"))
    descriptor_count = 1 + len(source_paths) if governance_path.is_file() else 0

    errors = descriptor_schema_errors(repo_root, schema_root=schema_root)
    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail=f"{descriptor_count} descriptor(s) validated")


def profile_schema_errors(
    repo_root: Path,
    *,
    schema_root: Path | None = None,
    profile_path: Path | None = None,
    distribution_root: Path | None = None,
) -> list[str]:
    """Validate a Deployment Profile with the canonical model and JSON Schema.

    `schema_root` may point at an installed distribution's schemas, and
    `profile_path` may select a profile other than the project default.
    """
    from olf.common.profile import DeploymentProfileError, load_deployment_profile

    profile_path = profile_path or repo_root / "openlakeforge.yaml"
    label = profile_path.name
    if not profile_path.is_file():
        return [f"no {label} found under {repo_root}"]

    try:
        document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{label}: {exc}"]

    errors: list[str] = []
    try:
        load_deployment_profile(profile_path, distribution_root=distribution_root)
    except DeploymentProfileError as exc:
        errors.append(f"{label}: canonical model rejected profile: {exc}")

    schema_path = (
        schema_root / "deployment-profile.schema.json"
        if schema_root is not None
        else repo_root / "docs/schema/deployment-profile.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    schema_errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    for error in schema_errors:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{label}: schema violation at {location}: {error.message}")

    return errors


def _check_deployment_profile_schema_conformance(
    repo_root: Path, *, schema_root: Path | None = None, distribution_root: Path | None = None
) -> CheckResult:
    """The project-root `openlakeforge.yaml` must load via the canonical
    `olf.common.profile` model and conform to its versioned JSON Schema."""
    name = "deployment_profile_schema_conformance"
    errors = profile_schema_errors(repo_root, schema_root=schema_root, distribution_root=distribution_root)
    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail="deployment profile validated")
