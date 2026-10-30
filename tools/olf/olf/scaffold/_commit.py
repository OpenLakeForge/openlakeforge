"""Verify-then-commit for a `ScaffoldPlan`.

Nothing is written to the real repository until the plan has been proven
valid in an isolated temp tree: the canonical descriptor model plus both
JSON Schemas, exactly as `olf contracts check` validates the committed
descriptors. On any failure, zero files are written and `governance.yml` is
left byte-identical.

Concurrent `olf ... new` invocations against the same repo checkout are not
supported: no cross-process lock guards the check/verify/write sequence, so
two commands that both finish verification before either writes can race --
the second write silently drops the first command's `governance.yml`
addition while leaving its files on disk. This is a deliberate scope
boundary, not a silent-corruption risk: like `rails generate`, `cargo new`,
or `ng generate`, this tool assumes one command runs against a given
checkout at a time, and a lost race is immediately caught by the very next
`olf contracts check` (or scaffold command), which fails loudly on files
undeclared in the descriptor -- not accepted unnoticed.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import yaml
from openlakeforge_domain import (
    LakehouseDescriptorError,
    LakehouseInventory,
    load_lakehouse_inventory,
    load_transitional_lakehouse_inventory,
)

from olf.contracts_check import descriptor_schema_errors
from olf.scaffold._shared import ScaffoldError, ScaffoldFile, ScaffoldPlan

_SOURCE_YAML_SUFFIX = "source.yaml"
_BRONZE_PREFIX = "lakehouse_code/bronze/"
_YAML_SUFFIXES = (".yaml", ".yml")

# Matches the owned root directory of a brand-new source/domain/product/
# dashboard: `lakehouse_code/bronze/<source>/...`,
# `lakehouse_code/silver/<domain>/...`, `lakehouse_code/gold/<product>/...`,
# `lakehouse_code/dashboards/superset/<name>/...`. A path that isn't nested
# under one of these (e.g. a Dagster module added beside existing ones) has
# no such root and is not checked here -- only genuinely new ownership
# subtrees are.
_OWNERSHIP_ROOT = re.compile(r"^lakehouse_code/(?:bronze|silver|gold|dashboards/superset)/[a-z][a-z0-9_]*")
# One entity per file: extending an existing domain adds a file beside its
# `_domain.yml`, inside an ownership root that already has content.
_FLOE_GLOB = "silver/*/contracts/floe/*.yml"
_FLOE_ENTITY = re.compile(r"^lakehouse_code/silver/[a-z][a-z0-9_]*/contracts/floe/[^/]+\.yml$")


def _extends_existing_domain(repo_root: Path, relative_path: str) -> bool:
    return bool(_FLOE_ENTITY.match(relative_path)) and (
        (repo_root / relative_path).parent / "_domain.yml"
    ).is_file()


def check_no_existing_targets(repo_root: Path, files: tuple[ScaffoldFile, ...]) -> None:
    existing = sorted(f.relative_path for f in files if (repo_root / f.relative_path).exists())
    if existing:
        raise ScaffoldError("refusing to overwrite existing file(s): " + ", ".join(existing))

    # A brand-new ownership directory should not already exist with
    # unrelated content: a stray file left in a would-be new domain would be
    # read as one of its Floe entities, and that collision wouldn't be caught
    # above if the stray file's name happens not to match any file this plan
    # writes.
    roots = sorted(
        {
            match.group(0)
            for f in files
            if (match := _OWNERSHIP_ROOT.match(f.relative_path))
            and not _extends_existing_domain(repo_root, f.relative_path)
        }
    )
    for root in roots:
        root_path = repo_root / root
        if not root_path.is_dir():
            continue
        # Check for files, not just directory entries: a rolled-back partial
        # write (see _write()) unlinks the files it created but leaves the
        # now-empty parent directories it made behind, and those must not
        # look like "unexpected content" on the next retry.
        if any(p.is_file() for p in root_path.rglob("*")):
            raise ScaffoldError(
                f"refusing to scaffold into {root}: the directory already exists with unexpected content "
                "(expected a brand-new source/domain/product/dashboard directory)"
            )


def _check_yaml_well_formed(files: tuple[ScaffoldFile, ...]) -> None:
    """Every generated/edited YAML file must at least parse. `governance.yml`
    and `source.yaml` are already covered (more strictly) by the canonical
    model and JSON Schema; this catches malformed YAML in the rest -- Floe
    contracts, Superset bundles -- that those don't touch."""
    for scaffold_file in files:
        if not scaffold_file.relative_path.endswith(_YAML_SUFFIXES):
            continue
        try:
            yaml.safe_load(scaffold_file.content)
        except yaml.YAMLError as exc:
            raise ScaffoldError(f"{scaffold_file.relative_path}: generated content is not valid YAML: {exc}") from exc


def _check_asset_key_collisions(inventory: LakehouseInventory) -> None:
    """Dagster's `lakehouse_code/definitions.py` merges every source's Bronze
    assets (keyed `[source, resource]`), every domain's Silver assets (keyed
    `[domain, silver_table]`), and every product's Gold assets (keyed
    `[product, gold_table]`) into one code location. Two of those three key
    families colliding -- e.g. a domain named after an existing source,
    consuming a table named after one of that source's resources -- makes
    two distinct assets claim the same key, and the code location fails to
    load. The canonical model has no opinion on this (asset keys aren't part
    of the descriptor), so the scaffold checks it directly.
    """
    owners: dict[tuple[str, str], str] = {}
    collisions: list[str] = []

    def register(key: tuple[str, str], owner: str) -> None:
        existing = owners.get(key)
        if existing is not None and existing != owner:
            collisions.append(f"asset key {key!r} is claimed by both {existing} and {owner}")
        else:
            owners[key] = owner

    for source in inventory.sources:
        for resource in source.resources:
            register((source.name, resource.name), f"Bronze resource {source.name}/{resource.name}")
    for domain in inventory.domains:
        for entity in inventory.floe_entities(domain):
            register((domain.name, entity.name), f"Silver table {domain.name}/{entity.name}")

    if collisions:
        raise ScaffoldError("; ".join(collisions))


def _verify(
    repo_root: Path,
    plan: ScaffoldPlan,
    *,
    schema_root: Path | None = None,
    allow_transitional: bool = False,
) -> None:
    _check_yaml_well_formed(plan.files)
    _check_yaml_well_formed(plan.edits)

    lakehouse_src = repo_root / "lakehouse_code"
    with tempfile.TemporaryDirectory(prefix="olf-scaffold-verify-") as tmp:
        verify_lakehouse = Path(tmp) / "lakehouse_code"
        (verify_lakehouse / "bronze").mkdir(parents=True)
        resolved_schema_root = schema_root or repo_root / "docs" / "schema"
        for descriptor in sorted((*lakehouse_src.glob("bronze/*/source.yaml"), *lakehouse_src.glob(_FLOE_GLOB))):
            destination = verify_lakehouse / descriptor.relative_to(lakehouse_src)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(descriptor, destination)
        for scaffold_file in plan.files:
            is_source = scaffold_file.relative_path.startswith(_BRONZE_PREFIX) and scaffold_file.relative_path.endswith(
                _SOURCE_YAML_SUFFIX
            )
            if not (is_source or _FLOE_ENTITY.match(scaffold_file.relative_path)):
                continue
            destination = Path(tmp) / scaffold_file.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(scaffold_file.content, encoding="utf-8")
        governance = lakehouse_src / "governance.yml"
        if plan.governance_yml is not None:
            (verify_lakehouse / "governance.yml").write_text(plan.governance_yml, encoding="utf-8")
        elif governance.is_file():
            shutil.copy(governance, verify_lakehouse / "governance.yml")

        errors = descriptor_schema_errors(
            Path(tmp), schema_root=resolved_schema_root, allow_incomplete=allow_transitional
        )
        if errors:
            raise ScaffoldError("; ".join(errors))
        load = load_transitional_lakehouse_inventory if allow_transitional else load_lakehouse_inventory
        try:
            inventory = load(Path(tmp))
        except LakehouseDescriptorError as exc:
            raise ScaffoldError(str(exc)) from exc
        _check_asset_key_collisions(inventory)


def _write(repo_root: Path, plan: ScaffoldPlan) -> None:
    """Write every file in `plan`. `_verify` already proved the plan valid,
    so failure here means a filesystem-level problem (permission denied,
    disk full, a target that changed underneath us) that pre-checking
    `.exists()` can't catch in advance. On any such failure, undo every
    write already made this call -- delete each brand-new file
    (`plan.files`), restore the pre-existing content of each edited file
    (`plan.edits`) -- so a partial write never strands the repository in a
    state a retry can't cleanly build on top of.
    """
    governance_target = repo_root / "lakehouse_code" / "governance.yml"
    created: list[Path] = []
    # governance.yml is an edit to a pre-existing file (or a new one) -- its
    # original content must be saved and restorable exactly like the other
    # edits (e.g. a Floe contract), not treated as a special last step that
    # a failure partway through its own write would leave corrupted.
    original_edit_content: dict[Path, str] = {}
    if plan.governance_yml is not None and governance_target.is_file():
        original_edit_content[governance_target] = governance_target.read_text(encoding="utf-8")
    try:
        for scaffold_file in plan.edits:
            target = repo_root / scaffold_file.relative_path
            original_edit_content[target] = target.read_text(encoding="utf-8")
        for scaffold_file in plan.files:
            target = repo_root / scaffold_file.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            # Recorded before writing, not after: write_text() can create or
            # truncate the target and then still raise partway through (a
            # disk-full error doesn't necessarily fail atomically before
            # touching the file), so waiting for success to record it would
            # miss exactly the file whose write failed.
            created.append(target)
            target.write_text(scaffold_file.content, encoding="utf-8")
        for scaffold_file in plan.edits:
            target = repo_root / scaffold_file.relative_path
            target.write_text(scaffold_file.content, encoding="utf-8")
        if plan.governance_yml is not None:
            if not governance_target.is_file():
                created.append(governance_target)
            governance_target.write_text(plan.governance_yml, encoding="utf-8")
    except OSError:
        for target in created:
            target.unlink(missing_ok=True)
        for target, content in original_edit_content.items():
            target.write_text(content, encoding="utf-8")
        raise


def commit_plan(
    repo_root: Path,
    plan: ScaffoldPlan,
    *,
    schema_root: Path | None = None,
    allow_transitional: bool = False,
) -> None:
    """Verify `plan` against the canonical model in an isolated temp tree,
    then write every file for real. Writes nothing on failure.

    `allow_transitional` is for the steps that legitimately leave an
    `olf init --empty` project short of a runnable product -- `source new` and
    `domain new`. `product new` always verifies strictly, so the first product
    is what converts a transitional project into a schema-valid v1alpha3 one."""
    check_no_existing_targets(repo_root, plan.files)
    _verify(repo_root, plan, schema_root=schema_root, allow_transitional=allow_transitional)
    _write(repo_root, plan)
