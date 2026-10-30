"""The promotion contract for source-controlled Superset report bundles.

Pure file checks with no cluster, cloud or Superset access, so `olf report
validate` is a common command and the artifacts phase can run the same checks
before it freezes a revision.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from openlakeforge_services.reports import REPORT_YAML_SUFFIXES, canonical_uuid

from olf.common.profile import StageName


def discover_report_dirs(repo_root: Path) -> list[str]:
    root = repo_root / "lakehouse_code" / "dashboards" / "superset"
    dirs = {str(path.parent.relative_to(repo_root)) for path in root.glob("*/metadata.yaml")}
    return sorted(dirs)


def validate_report_registry(repo_root: Path, declared_report_dirs: Sequence[str]) -> list[str]:
    """Require exact parity between descriptor-declared and mounted bundles."""
    declared = set(declared_report_dirs)
    discovered = set(discover_report_dirs(repo_root))
    if declared != discovered:
        missing = sorted(declared - discovered)
        undeclared = sorted(discovered - declared)
        details: list[str] = []
        if missing:
            details.append("declared but not mounted: " + ", ".join(missing))
        if undeclared:
            details.append("mounted but not declared: " + ", ".join(undeclared))
        raise RuntimeError("Superset dashboard registry mismatch (" + "; ".join(details) + ")")
    return sorted(declared)


# The promotion contract for a source-controlled bundle (#130). Superset
# matches assets across instances by `uuid`, so a bundle whose identities are
# missing, duplicated, or dangling imports into DEV and then silently forks or
# fails in PROD -- the one stage nothing re-exports from. These directories are
# what `unpack_export_bundle` manages and `build_report_bundle` packs.
_MANAGED_ASSET_DIRS: tuple[str, ...] = ("databases", "datasets", "charts", "dashboards")
_ASSET_REFERENCES = {"datasets": ("database_uuid", "databases"), "charts": ("dataset_uuid", "datasets")}
# The database is the one identity several bundles are meant to hold in
# common: every dashboard reads the same Gold through one Trino connection,
# and the reference project's bundles all declare the same
# `openlakeforge_trino` uuid deliberately. Nothing else is shareable --
# `deploy_reports` imports every declared bundle into one Superset instance in
# turn, so a chart, dataset, or dashboard uuid reused across two bundles makes
# the second import overwrite the first asset and leaves the earlier dashboard
# pointing at someone else's chart.
_SHAREABLE_ASSET_DIRS = frozenset({"databases"})
# A promotable bundle is stage-neutral: target connectivity is resolved at
# import time by `build_report_bundle`, so a checked-in stage-bound physical
# name (`lakehouse_<stage>` catalogs, `olf-<stage>` namespaces) would survive
# promotion and point PROD at another stage's data. `ws_<user>_` is a personal
# workspace identity, which #111 excludes from promotable dependencies
# outright. `s3://`, `http://`, and credential literals are already rejected
# for every revision component by olf.infra.project_revision.
_FORBIDDEN_BUNDLE_VALUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bws_[a-z0-9][a-z0-9-]*_", re.IGNORECASE), "a personal workspace identifier"),
    (
        # `\b` after the stage token would be wrong on exactly the case that
        # matters most: `_` is a word character, so `lakehouse_dev` matches
        # while AWS's prefixed schema `lakehouse_dev_order_revenue_gold` --
        # what `build_report_bundle`'s `schema_prefix` produces -- would not.
        # A negative lookahead on letters and digits admits the suffix while
        # still refusing `lakehouse_development`.
        re.compile(
            r"(?<![a-z0-9])(?:lakehouse|olf)[_-](?:"
            + "|".join(stage.value for stage in StageName)
            + r")(?![a-z0-9])",
            re.IGNORECASE,
        ),
        "a stage-bound physical name",
    ),
)


def _yaml_problem(exc: Exception) -> str:
    """A YAML parse error as one line, for a violation a user reads in a list."""
    return " ".join(str(exc).split())


_UNCOMPARED_DATABASE_FIELDS = frozenset(
    {
        # `build_report_bundle` rewrites it to the target stage's own
        # connection while packaging, so the checked-in value is a
        # placeholder rather than part of the shared definition.
        "sqlalchemy_uri",
        # The canonical identity is already this comparison's map key, so the
        # raw spelling adds nothing -- and comparing it would report two
        # identical databases as conflicting whenever they merely differ in
        # case, which `canonical_uuid` exists to treat as the same asset.
        "uuid",
    }
)


def _shared_database_definition(document: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a database document two bundles sharing a uuid must agree on."""
    return {key: value for key, value in document.items() if key not in _UNCOMPARED_DATABASE_FIELDS}


def report_bundle_errors(
    repo_root: Path, report_dir: str, *, owner_of: dict[str, tuple[str, str, dict[str, Any]]] | None = None
) -> list[str]:
    """Check one source-controlled report bundle against the promotion contract.

    Returns every violation rather than raising on the first: an analyst
    re-exporting a bundle wants the whole list, not one round trip per
    problem.

    `owner_of` maps each claimed uuid to the (kind, file, document) that
    claimed it. Pass one across several calls to check identity uniqueness
    over a whole set of bundles -- see `validate_report_bundles`.
    """
    bundle_dir = repo_root / report_dir
    metadata_path = bundle_dir / "metadata.yaml"
    if not metadata_path.is_file():
        return [f"{report_dir}/metadata.yaml: missing"]
    # Import goes through `/api/v1/assets/import/`, which accepts only `type:
    # assets` -- a dashboard-type export would be refused by the API rather
    # than here. `export_report` rewrites the type for exactly that reason, so
    # a bundle that lost it was hand-edited.
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{report_dir}/metadata.yaml: is not valid YAML ({_yaml_problem(exc)})"]
    if not isinstance(metadata, dict):
        return [f"{report_dir}/metadata.yaml: is not a YAML mapping"]
    if metadata.get("type") != "assets":
        return [f"{report_dir}/metadata.yaml: declares type {metadata.get('type')!r}, not 'assets'"]
    if not isinstance(metadata.get("version"), str) or not metadata["version"]:
        return [f"{report_dir}/metadata.yaml: has no export format version"]

    errors: list[str] = []
    owner_of = {} if owner_of is None else owner_of
    identities: dict[str, set[str]] = {kind: set() for kind in _MANAGED_ASSET_DIRS}
    documents: list[tuple[str, str, dict[str, Any]]] = []
    for kind in _MANAGED_ASSET_DIRS:
        for path in sorted((bundle_dir / kind).rglob("*")):
            if not path.is_file() or path.suffix.lower() not in REPORT_YAML_SUFFIXES:
                continue
            name = f"{report_dir}/{path.relative_to(bundle_dir).as_posix()}"
            # A hand-edit or a merge leaves invalid YAML behind often enough
            # that dying on it would make the validator useless exactly when
            # it is needed: the point is to name the file and keep collecting.
            text = path.read_text(encoding="utf-8")
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                errors.append(f"{name}: is not valid YAML ({_yaml_problem(exc)})")
                continue
            if not isinstance(document, dict):
                errors.append(f"{name}: is not a YAML mapping")
                continue
            documents.append((kind, name, document))
            identity = document.get("uuid")
            if not isinstance(identity, str) or not identity:
                errors.append(f"{name}: has no stable uuid")
                continue
            # Superset's import schema expects a real UUID. An arbitrary
            # string matches its own references and would pass every check
            # here, then fail at import into whichever stage runs first.
            canonical = canonical_uuid(identity)
            if canonical is None:
                errors.append(f"{name}: uuid {identity!r} is not a UUID")
                continue
            identities[kind].add(canonical)
            claimed = owner_of.get(canonical)
            if claimed is None:
                owner_of[canonical] = (kind, name, document)
                continue
            claimed_kind, claimed_name, claimed_document = claimed
            if kind not in _SHAREABLE_ASSET_DIRS or claimed_kind not in _SHAREABLE_ASSET_DIRS:
                errors.append(f"{name}: uuid {canonical} is already used by {claimed_name}")
            elif _shared_database_definition(document) != _shared_database_definition(claimed_document):
                errors.append(
                    f"{name}: uuid {canonical} defines a different database than {claimed_name}; "
                    "a shared connection must be declared identically in every bundle"
                )

    # A bundle carrying only a database and its datasets is what `olf product
    # new --with-report` leaves behind: the scaffold cannot invent a dashboard
    # layout, so the analyst authors one in Superset and exports it back. That
    # state is legitimate on disk and not promotable, and `e2e._assertions`
    # already refuses it at runtime -- accepting it here would publish an
    # incomplete revision and surface the omission only after promotion.
    if not any(kind == "dashboards" for kind, _, _ in documents):
        errors.append(
            f"{report_dir}: exports no Superset dashboard; author it in Superset "
            "and export it back into this bundle before promoting"
        )

    for kind, name, document in documents:
        if kind in _ASSET_REFERENCES:
            field, target = _ASSET_REFERENCES[kind]
            reference = document.get(field)
            if canonical_uuid(reference) not in identities[target]:
                errors.append(f"{name}: {field} {reference!r} does not resolve inside the bundle")
            continue
        if kind != "dashboards":
            continue
        position = document.get("position")
        for key, block in position.items() if isinstance(position, dict) else ():
            if not isinstance(block, dict) or block.get("type") != "CHART":
                continue
            meta = block.get("meta")
            reference = meta.get("uuid") if isinstance(meta, dict) else None
            if canonical_uuid(reference) not in identities["charts"]:
                errors.append(f"{name}: position block {key} references chart {reference!r} the bundle does not define")

    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in REPORT_YAML_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        name = f"{report_dir}/{path.relative_to(bundle_dir).as_posix()}"
        for pattern, detail in _FORBIDDEN_BUNDLE_VALUES:
            match = pattern.search(text)
            if match is not None:
                errors.append(f"{name}: contains {detail} ({match.group(0)!r})")
    return errors


def validate_report_bundles(repo_root: Path, report_dirs: Sequence[str]) -> list[str]:
    """Collect promotion-contract violations across every declared bundle.

    Identity uniqueness is checked over the whole set rather than one bundle
    at a time, because the hazard is per Superset instance rather than per
    bundle: `deploy_reports` imports them all into the same one.
    """
    owner_of: dict[str, tuple[str, str, dict[str, Any]]] = {}
    return [
        error for report_dir in report_dirs for error in report_bundle_errors(repo_root, report_dir, owner_of=owner_of)
    ]
