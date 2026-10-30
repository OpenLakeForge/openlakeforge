"""`olf domain new` -- generate a domain-owned Silver package.

A domain may be created (and validated) before any product consumes its
Silver tables -- see the plan's "product-less domains" section and the
canonical-model relaxation in `packages/domain-model`.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from openlakeforge_domain import LakehouseInventory, load_transitional_lakehouse_inventory

from olf.scaffold import _templates, _yaml_edit
from olf.scaffold._csv import infer_columns
from olf.scaffold._shared import ScaffoldError, ScaffoldFile, ScaffoldPlan, require_identifier, title_case, yaml_dq
from olf.scaffold._templates import FloeEntitySpec


def resolve_input(inventory: LakehouseInventory, source: str, resource: str) -> None:
    """Raise unless `source/resource` names a declared source resource."""
    matching_source = next((candidate for candidate in inventory.sources if candidate.name == source), None)
    if matching_source is None:
        raise ScaffoldError(f"--input {source}/{resource}: unknown source {source!r}")
    if resource not in {r.name for r in matching_source.resources}:
        raise ScaffoldError(f"--input {source}/{resource}: source {source!r} has no resource {resource!r}")


def example_csv_text(repo_root: Path, source: str, resource: str) -> str | None:
    path = repo_root / "lakehouse_code" / "bronze" / source / "examples" / f"{resource}.csv"
    return path.read_text(encoding="utf-8") if path.is_file() else None


_FLOE_SOURCES = "lakehouse_code/silver/sources.yml"


def floe_entity_files(
    repo_root: Path, *, domain: str, inputs: tuple[tuple[str, str], ...]
) -> tuple[ScaffoldFile, ...]:
    """One Floe entity file per `source/resource` input, columns inferred from its example CSV."""
    files = []
    for source, resource in inputs:
        csv_text = example_csv_text(repo_root, source, resource)
        columns = infer_columns(csv_text) if csv_text is not None else ()
        files.append(
            ScaffoldFile(
                f"lakehouse_code/silver/{domain}/contracts/floe/{resource}.yml",
                _templates.render_floe_entity(
                    FloeEntitySpec(table=resource, source=source, resource=resource, domain=domain, columns=columns)
                ),
            )
        )
    return tuple(files)


def floe_sources_edit(repo_root: Path, inputs: tuple[tuple[str, str], ...]) -> ScaffoldFile | None:
    """Declare each input's Bronze source in the Floe project the first time an entity references it."""
    path = repo_root / _FLOE_SOURCES
    if not path.is_file():
        raise ScaffoldError(f"{_FLOE_SOURCES}: expected the Floe project's named sources")
    text = path.read_text(encoding="utf-8")
    declared = {source["name"] for source in (yaml.safe_load(text) or {}).get("sources") or []}
    missing = sorted({source for source, _resource in inputs} - declared)
    if not missing:
        return None
    addition = "".join(_templates.render_floe_source(source=source) for source in missing)
    return ScaffoldFile(
        _FLOE_SOURCES,
        _yaml_edit.append_to_top_level_list(text, "sources", addition, source_label=_FLOE_SOURCES),
    )


_GOVERNANCE = "lakehouse_code/governance.yml"
_NEW_GOVERNANCE = "apiVersion: openlakeforge.io/v1beta1\nkind: Governance\ndomains: []\nproducts: []\n"


def add_governance_entry(repo_root: Path, text: str | None, key: str, entry: str) -> str:
    """Append `entry` to governance.yml's `key:` list; `text` is the file as already edited, if it was."""
    if text is None:
        path = repo_root / _GOVERNANCE
        text = path.read_text(encoding="utf-8") if path.is_file() else _NEW_GOVERNANCE
    return _yaml_edit.append_to_top_level_list(text, key, entry, source_label="governance.yml")


def build_domain_artifacts(
    repo_root: Path,
    *,
    domain: str,
    display_name: str,
    inputs: tuple[tuple[str, str], ...],
) -> tuple[str, tuple[ScaffoldFile, ...], tuple[str, ...]]:
    """Build a new domain's `governance.yml` entry and its on-disk files.

    Returns (domain_block_text, files, silver_table_names). Shared by
    `plan_domain_new` and `plan_product_new` (which creates the domain
    inline when `<domain>/<product>` names a domain that does not exist
    yet).
    """
    table_names = [resource for _source, resource in inputs]
    if len(set(table_names)) != len(table_names):
        raise ScaffoldError(
            f"domain {domain!r}: --input resources produce duplicate Silver table names {table_names!r}"
        )

    domain_block = (
        f"  - name: {domain}\n"
        f"    displayName: {yaml_dq(display_name)}\n"
        f"    description: {yaml_dq(display_name + ' domain.')}\n"
        "    status: planned\n"
    )

    silver_dir = f"lakehouse_code/silver/{domain}"
    files = (
        ScaffoldFile(f"{silver_dir}/__init__.py", ""),
        ScaffoldFile(f"{silver_dir}/contracts/floe/_domain.yml", _templates.render_floe_domain(domain=domain)),
        *floe_entity_files(repo_root, domain=domain, inputs=inputs),
        ScaffoldFile(
            f"{silver_dir}/README.md",
            _templates.render_domain_readme(domain=domain, display_name=display_name),
        ),
    )
    return domain_block, files, tuple(table_names)


def plan_domain_new(
    repo_root: Path,
    *,
    domain: str,
    display_name: str | None,
    inputs: tuple[tuple[str, str], ...],
) -> ScaffoldPlan:
    require_identifier(domain, field="domain")
    if not inputs:
        raise ScaffoldError("domain new: at least one --input <source>/<resource> is required")

    inventory = load_transitional_lakehouse_inventory(repo_root)
    if domain in inventory.domain_names:
        raise ScaffoldError(f"domain {domain!r} already exists in governance.yml")

    for source, resource in inputs:
        resolve_input(inventory, source, resource)

    resolved_display_name = display_name or title_case(domain)
    domain_block, files, table_names = build_domain_artifacts(
        repo_root, domain=domain, display_name=resolved_display_name, inputs=inputs
    )

    governance_text = add_governance_entry(repo_root, None, "domains", domain_block)

    summary = (
        f"Created domain {domain!r} with {len(inputs)} Silver table(s): {', '.join(table_names)}.",
        f"Domain {domain!r} has no products yet; run `olf product new {domain}/<product>` to add one.",
    )
    sources = floe_sources_edit(repo_root, inputs)
    return ScaffoldPlan(
        files=files, governance_yml=governance_text, summary=summary, edits=(sources,) if sources else ()
    )
