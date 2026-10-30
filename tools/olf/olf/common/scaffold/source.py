"""`olf source new` -- generate a Source-owned Bronze ingestion skeleton."""

from __future__ import annotations

from pathlib import Path

from openlakeforge_domain import load_transitional_lakehouse_inventory

from olf.common.scaffold import _templates
from olf.common.scaffold._csv import placeholder_example_csv
from olf.common.scaffold._shared import ScaffoldError, ScaffoldFile, ScaffoldPlan, require_identifier, title_case


def plan_source_new(
    repo_root: Path,
    *,
    source: str,
    display_name: str | None,
    resources: tuple[str, ...],
) -> ScaffoldPlan:
    require_identifier(source, field="source")
    if not resources:
        raise ScaffoldError("source new: at least one --resource is required")
    for resource in resources:
        require_identifier(resource, field="resource")
    if len(set(resources)) != len(resources):
        raise ScaffoldError(f"source {source!r}: --resource values must be unique")

    inventory = load_transitional_lakehouse_inventory(repo_root)
    if source in inventory.source_names:
        raise ScaffoldError(f"source {source!r} already exists")

    resolved_display_name = display_name or title_case(source)
    bronze_dir = f"lakehouse_code/bronze/{source}"

    resource_entries = tuple((name, f"Raw CSV {name.replace('_', ' ')}.") for name in resources)

    files = [
        ScaffoldFile(
            f"{bronze_dir}/source.yaml",
            _templates.render_source_yaml(
                source=source,
                display_name=resolved_display_name,
                description=f"{resolved_display_name} source system.",
                resources=resource_entries,
            ),
        ),
        ScaffoldFile(f"{bronze_dir}/dlt/__init__.py", ""),
        ScaffoldFile(
            f"{bronze_dir}/dlt/{source}.py",
            _templates.render_dlt_loader(source=source, resources=resources),
        ),
        ScaffoldFile(
            f"{bronze_dir}/README.md",
            _templates.render_bronze_readme(source=source, display_name=resolved_display_name),
        ),
    ]
    for resource in resources:
        files.append(ScaffoldFile(f"{bronze_dir}/examples/{resource}.csv", placeholder_example_csv(resource)))

    summary = (
        f"Created source {source!r} with {len(resources)} resource(s): {', '.join(resources)}.",
    )
    return ScaffoldPlan(files=tuple(files), governance_yml=None, summary=summary)
