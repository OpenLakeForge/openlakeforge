"""`olf source new` -- generate a Source-owned Bronze ingestion skeleton."""

from __future__ import annotations

from pathlib import Path

from openlakeforge_core import load_transitional_lakehouse_inventory

from olf.common.scaffold import _templates
from olf.common.scaffold._csv import placeholder_example_csv
from olf.common.scaffold._shared import ScaffoldError, ScaffoldFile, ScaffoldPlan, require_identifier, title_case


def plan_source_new(
    repo_root: Path,
    *,
    source: str,
    display_name: str | None,
    resources: tuple[str, ...],
    kind: str = "csv",
    cursor: str | None = None,
    primary_key: str = "id",
) -> ScaffoldPlan:
    """`csv` writes the example-file loader; `api` and `sql` write an incremental dlt
    connector on `libs.bronze_incremental`, one `load: incremental` resource each."""
    require_identifier(source, field="source")
    if not resources:
        raise ScaffoldError("source new: at least one --resource is required")
    for resource in resources:
        require_identifier(resource, field="resource")
    if len(set(resources)) != len(resources):
        raise ScaffoldError(f"source {source!r}: --resource values must be unique")
    if kind not in ("csv", "api", "sql"):
        raise ScaffoldError(f"source new: --kind must be csv, api or sql, not {kind!r}")
    load = None
    if kind != "csv":
        load = _templates.IncrementalLoad(
            kind=kind, cursor=cursor or ("updated_at" if kind == "api" else primary_key), primary_key=primary_key
        )
        require_identifier(load.cursor, field="cursor")
        require_identifier(load.primary_key, field="primary-key")
    elif cursor is not None:
        raise ScaffoldError("source new: --cursor applies to --kind api or sql; a CSV source is loaded in full")

    inventory = load_transitional_lakehouse_inventory(repo_root)
    if source in inventory.source_names:
        raise ScaffoldError(f"source {source!r} already exists")

    resolved_display_name = display_name or title_case(source)
    bronze_dir = f"lakehouse_code/bronze/{source}"

    label = {"csv": "Raw CSV", "api": "API", "sql": "Table"}[kind]
    resource_entries = tuple((name, f"{label} {name.replace('_', ' ')}.") for name in resources)
    connector = (
        _templates.render_dlt_loader(source=source, resources=resources)
        if load is None
        else _templates.render_incremental_connector(
            source=source, display_name=resolved_display_name, resources=resources, load=load
        )
    )

    files = [
        ScaffoldFile(
            f"{bronze_dir}/source.yaml",
            _templates.render_source_yaml(
                source=source,
                display_name=resolved_display_name,
                description=f"{resolved_display_name} source system.",
                resources=resource_entries,
                load=load,
            ),
        ),
        ScaffoldFile(f"{bronze_dir}/dlt/__init__.py", ""),
        ScaffoldFile(f"{bronze_dir}/dlt/{source}.py", connector),
        ScaffoldFile(
            f"{bronze_dir}/README.md",
            _templates.render_bronze_readme(source=source, display_name=resolved_display_name, kind=kind),
        ),
    ]
    for resource in resources:
        example = placeholder_example_csv(resource) if load is None else _templates.incremental_example_csv(load)
        files.append(ScaffoldFile(f"{bronze_dir}/examples/{resource}.csv", example))

    summary = (f"Created source {source!r} with {len(resources)} resource(s): {', '.join(resources)}.",)
    return ScaffoldPlan(files=tuple(files), governance_yml=None, summary=summary)
