"""Floe and Helm checks: the Floe project layout and parsed values files."""

from __future__ import annotations

from pathlib import Path

import yaml

from olf.common.contracts_check._report import CheckResult

ENTITIES_GLOB = "contracts/*.yml"


def _iceberg_target(document: dict) -> dict:
    return ((document.get("sink") or {}).get("accepted") or {}).get("iceberg") or {}


def _check_floe_contract_structure(repo_root: Path) -> CheckResult:
    """The Floe project under `lakehouse_code/silver/` writes each domain to
    its own `<domain>_silver` namespace through the logical `iceberg_catalog`
    alias (never a shared namespace, per ADR 0004, nor a provider name).

    A domain sets both in its `domain.yml` defaults; an entity file may
    repeat them but not point elsewhere. The domain file sits at the domain
    root and loads its entities from `contracts/` through Floe's `entities`
    glob, the one place olf's inventory and scaffold read them from.
    """
    name = "floe_contract_structure"
    errors: list[str] = []
    project = repo_root / "lakehouse_code" / "silver" / "floe.yml"
    if not project.is_file():
        return CheckResult(name, ok=False, detail="lakehouse_code/silver/floe.yml not found")
    then = f'; set `entities: "{ENTITIES_GLOB}"` in each domain.yml and include `*/domain.yml` in silver/floe.yml'
    before_contracts = sorted(repo_root.glob("lakehouse_code/silver/*/contracts/floe/_domain.yml"))
    if before_contracts:
        moved = ", ".join(str(path.parent.relative_to(repo_root)) for path in before_contracts)
        return CheckResult(
            name,
            ok=False,
            detail=(
                f"Floe contracts sit in {moved}; move each domain's entity files up to `contracts/` and its "
                "`_domain.yml` to `silver/<domain>/domain.yml`" + then
            ),
        )
    # An entity named `domain` lives at contracts/domain.yml too, but beside a domain file at the root.
    in_contracts = sorted(
        path
        for path in repo_root.glob("lakehouse_code/silver/*/contracts/domain.yml")
        if not (path.parents[1] / "domain.yml").is_file()
    )
    if in_contracts:
        moved = ", ".join(str(path.relative_to(repo_root)) for path in in_contracts)
        return CheckResult(
            name,
            ok=False,
            detail=f"the domain file sits in contracts/ at {moved}; move it to `silver/<domain>/domain.yml`" + then,
        )
    domain_files = sorted(repo_root.glob("lakehouse_code/silver/*/domain.yml"))
    if not domain_files:
        return CheckResult(name, ok=False, detail="no lakehouse_code/silver/*/domain.yml files found")

    checked = 0
    for domain_file in domain_files:
        folder = domain_file.parent.name
        expected_namespace = f"{folder}_silver"
        domain = yaml.safe_load(domain_file.read_text(encoding="utf-8")) or {}
        rel_domain = domain_file.relative_to(repo_root)
        if domain.get("name") != folder:
            errors.append(f"{rel_domain}: name must be the domain folder {folder!r}, got {domain.get('name')!r}")
        if domain.get("entities") != ENTITIES_GLOB:
            errors.append(f'{rel_domain}: entities must be "{ENTITIES_GLOB}", got {domain.get("entities")!r}')
        defaults = _iceberg_target(domain.get("defaults") or {})
        for path in [domain_file, *sorted((domain_file.parent / "contracts").glob("*.yml"))]:
            document = domain if path == domain_file else yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            target = {**defaults, **_iceberg_target(document)} if path != domain_file else defaults
            rel_path = path.relative_to(repo_root)
            checked += 1
            if target.get("catalog") != "iceberg_catalog":
                errors.append(
                    f"{rel_path}: sink.accepted.iceberg.catalog must be the logical 'iceberg_catalog' alias, "
                    f"got {target.get('catalog')!r}"
                )
            if target.get("namespace") != expected_namespace:
                errors.append(
                    f"{rel_path}: sink.accepted.iceberg.namespace must be domain-scoped "
                    f"{expected_namespace!r}, got {target.get('namespace')!r}"
                )

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail=f"{checked} Floe domain and entity file(s) validated")


# `append` is safe here because a `bronze` full refresh also runs Floe with
# `--full-refresh` (`floe/full_refresh`), which rebuilds an appended table from
# every Bronze file instead of appending every row again.
_INCREMENTAL_WRITE_MODES = ("merge_scd1", "merge_scd2", "append")


def _check_silver_load_modes(repo_root: Path) -> CheckResult:
    """Each Silver entity's Floe load mode fits the Bronze resource it reads.

    An incremental resource lands only its new rows as new files, so its
    entity must read new files only (`incremental_mode: file`) and merge or
    append them onto the rows it already has. A full resource lands its whole extract on
    every run, so appending it would duplicate every row.
    """
    from openlakeforge_core import LakehouseDescriptorError, load_lakehouse_inventory

    name = "silver_load_modes"
    try:
        inventory = load_lakehouse_inventory(repo_root / "lakehouse_code")
    except LakehouseDescriptorError as exc:
        return CheckResult(name, ok=False, detail=str(exc))
    loads = {(source.name, table.name): table.load for source in inventory.sources for table in source.resources}
    errors: list[str] = []
    checked = 0
    for domain_file in sorted(repo_root.glob("lakehouse_code/silver/*/domain.yml")):
        defaults = (yaml.safe_load(domain_file.read_text(encoding="utf-8")) or {}).get("defaults") or {}
        for path in sorted((domain_file.parent / "contracts").glob("*.yml")):
            entity = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            source = entity.get("source") or {}
            load = loads.get((str(source.get("ref")), str(source.get("resource"))))
            if load is None:
                continue
            checked += 1
            mode = entity.get("incremental_mode", defaults.get("incremental_mode", "none"))
            write_mode = (entity.get("sink") or {}).get("write_mode") or (defaults.get("sink") or {}).get(
                "write_mode", "overwrite"
            )
            rel_path = path.relative_to(repo_root)
            resource = f"{source['ref']}/{source['resource']}"
            if load.incremental and (mode != "file" or write_mode not in _INCREMENTAL_WRITE_MODES):
                errors.append(
                    f"{rel_path}: reads incremental resource {resource}, so it needs incremental_mode 'file' and "
                    f"write_mode one of {list(_INCREMENTAL_WRITE_MODES)!r}; got {mode!r} and {write_mode!r}"
                )
            elif not load.incremental and write_mode == "append":
                errors.append(
                    f"{rel_path}: appends full resource {resource}, which would add every row again on each run; "
                    "use overwrite or a merge"
                )

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail=f"{checked} Silver entity load mode(s) validated")


def _check_helm_values_as_data(repo_root: Path) -> CheckResult:
    """Parse the static Dagster Helm values input as data. Does not invoke
    `helm template` -- that remains `check-infra.sh`'s job."""
    name = "helm_values_as_data"
    values_path = repo_root / "infra/helm/values/local/dagster.yaml"
    if not values_path.is_file():
        return CheckResult(name, ok=False, detail=f"missing {values_path}")

    document = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    compute_log_manager = document.get("computeLogManager", {})
    errors: list[str] = []
    if compute_log_manager.get("type") != "S3ComputeLogManager":
        errors.append("computeLogManager.type must be 'S3ComputeLogManager'")
    s3_config = compute_log_manager.get("config", {}).get("s3ComputeLogManager", {})
    if not s3_config.get("bucket"):
        errors.append("computeLogManager.config.s3ComputeLogManager.bucket must be set")

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail="infra/helm/values/local/dagster.yaml validated as data")
