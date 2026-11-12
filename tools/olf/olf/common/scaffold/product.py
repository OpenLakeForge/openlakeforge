"""`olf product new` -- generate a product's folder of the Gold dbt project and its Dagster
job, referencing existing or newly-declared domain Silver tables.

Never generates a new dlt Bronze loader for a source resource that already
exists: `--input <source>/<resource>` always resolves to the source's own
`load_<source>_entities_to_bronze`, declared once by `olf source new`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from openlakeforge_core import load_transitional_lakehouse_inventory

from olf.common.scaffold import _templates
from olf.common.scaffold import domain as domain_module
from olf.common.scaffold._shared import (
    ScaffoldError,
    ScaffoldFile,
    ScaffoldPlan,
    require_identifier,
    title_case,
    yaml_dq,
)


def _parse_target(target: str) -> tuple[str, str]:
    if "/" not in target:
        raise ScaffoldError(f"product new: target {target!r} must be '<domain>/<product>'")
    domain, _, product = target.partition("/")
    if not domain or not product or "/" in product:
        raise ScaffoldError(f"product new: target {target!r} must be '<domain>/<product>'")
    return domain, product


_GOLD = "lakehouse_code/gold"


def _gold_project_changes(
    repo_root: Path, *, domain: str, product: str, silver_tables: tuple[str, ...]
) -> tuple[list[ScaffoldFile], list[ScaffoldFile]]:
    """The shared Gold dbt project's part of a new product (ADR 0004): new files and edits to existing ones.

    The project file gains the product's group block and the domain's source file the Silver tables it
    does not declare yet. Both are appended as text so a hand-written comment survives, then re-read: a
    file whose last block is not the one appended to is refused rather than silently misplaced.
    """
    files: list[ScaffoldFile] = []
    edits: list[ScaffoldFile] = []
    project_path = f"{_GOLD}/dbt_project.yml"
    project_file = repo_root / project_path
    if not project_file.is_file():
        files.append(ScaffoldFile(project_path, _templates.render_dbt_project_yml(products=(product,))))
        files.append(ScaffoldFile(f"{_GOLD}/packages.yml", _templates.render_dbt_packages_yml()))
    else:
        text = project_file.read_text(encoding="utf-8")
        edited = _append(text, _templates.render_dbt_product_block(product=product))
        models = _load(edited).get("models") or {}
        block = next(iter(models.values()), None) if isinstance(models, dict) and len(models) == 1 else None
        if not isinstance(block, dict) or (block.get(product) or {}).get("+group") != product:
            raise ScaffoldError(
                f"{project_path}: cannot add product {product!r}; its last block must be the project's models "
                f"config. Add `{product}: {{+group: {product}}}` under it by hand, then re-run."
            )
        edits.append(ScaffoldFile(project_path, edited))

    sources_path = f"{_GOLD}/models/_sources/{domain}.yml"
    sources_file = repo_root / sources_path
    descriptions = {name: f"Validated {name.replace('_', ' ')}." for name in silver_tables}
    if not sources_file.is_file():
        tables = tuple(descriptions.items())
        files.append(ScaffoldFile(sources_path, _templates.render_dbt_sources_yml(domain=domain, tables=tables)))
        return files, edits
    text = sources_file.read_text(encoding="utf-8")
    declared = _source_tables(text, domain)
    missing = tuple((name, description) for name, description in descriptions.items() if name not in declared)
    if missing:
        edited = _append(text, _templates.render_dbt_source_tables(missing))
        if not {name for name, _ in missing} <= _source_tables(edited, domain):
            raise ScaffoldError(
                f"{sources_path}: cannot declare {[name for name, _ in missing]}; its last block must be "
                f"source {domain!r}'s tables. Add them there by hand, then re-run."
            )
        edits.append(ScaffoldFile(sources_path, edited))
    return files, edits


def _append(text: str, addition: str) -> str:
    return (text if text.endswith("\n") else text + "\n") + addition


def _load(text: str) -> dict:
    """The document, or nothing when the appended text broke it: either way the caller refuses the edit."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return document if isinstance(document, dict) else {}


def _source_tables(text: str, domain: str) -> set[str]:
    sources = _load(text).get("sources") or []
    source = next((item for item in sources if isinstance(item, dict) and item.get("name") == domain), None) or {}
    return {table.get("name") for table in source.get("tables") or [] if isinstance(table, dict)}


def _render_product_block(*, product: str, display_name: str) -> str:
    return (
        f"- id: {product}\n"
        f"  displayName: {yaml_dq(display_name)}\n"
        f"  description: {yaml_dq(display_name + ' product.')}\n"
        "  status: planned\n"
    )


def plan_product_new(
    repo_root: Path,
    *,
    target: str,
    display_name: str | None,
    silver_inputs: tuple[str, ...],
    inputs: tuple[tuple[str, str], ...],
    gold_tables: tuple[str, ...],
    with_report: bool,
) -> ScaffoldPlan:
    domain, product = _parse_target(target)
    require_identifier(domain, field="domain")
    require_identifier(product, field="product")
    for name in silver_inputs:
        require_identifier(name, field="silver-input")
    for name in gold_tables:
        require_identifier(name, field="gold-table")
    if not gold_tables:
        raise ScaffoldError("product new: at least one --gold-table is required")
    if len(set(gold_tables)) != len(gold_tables):
        raise ScaffoldError(f"product {product!r}: --gold-table values must be unique")

    inventory = load_transitional_lakehouse_inventory(repo_root)
    if product in {p.id for p in inventory.products}:
        raise ScaffoldError(f"product id {product!r} must be globally unique across the lakehouse")

    for source, resource in inputs:
        domain_module.resolve_input(inventory, source, resource)
    new_table_names = [resource for _source, resource in inputs]

    domain_exists = domain in inventory.domain_names
    governance_text: str | None = None
    extra_files: list[ScaffoldFile] = []
    edits: list[ScaffoldFile] = []

    if not domain_exists:
        if silver_inputs:
            raise ScaffoldError(
                f"product new: domain {domain!r} does not exist yet, so --silver-input {silver_inputs[0]!r} "
                "cannot reference an existing Silver table; use --input <source>/<resource> instead"
            )
        if not inputs:
            raise ScaffoldError(
                f"product new: domain {domain!r} does not exist yet; provide at least one "
                "--input <source>/<resource> so it can be created"
            )
        resolved_domain_display_name = title_case(domain)
        domain_block, domain_files, _table_names = domain_module.build_domain_artifacts(
            repo_root, domain=domain, display_name=resolved_domain_display_name, inputs=inputs
        )
        extra_files.extend(domain_files)
        governance_text = domain_module.add_governance_domain(repo_root, governance_text, domain_block)
    else:
        target_domain = next(d for d in inventory.domains if d.name == domain)
        existing_silver_by_name = {entity.name: entity for entity in inventory.floe_entities(target_domain)}
        for name in silver_inputs:
            if name not in existing_silver_by_name:
                raise ScaffoldError(
                    f"--silver-input {name!r}: domain {domain!r} has no Silver table {name!r}; "
                    f"known tables: {sorted(existing_silver_by_name)!r}"
                )
        for source, resource in inputs:
            if resource in existing_silver_by_name:
                raise ScaffoldError(
                    f"--input {source}/{resource}: domain {domain!r} already has a Silver table named {resource!r}; "
                    f"use --silver-input {resource} instead"
                )

        if inputs:
            extra_files.extend(domain_module.floe_entity_files(repo_root, domain=domain, inputs=inputs))

    all_silver_inputs = tuple(silver_inputs) + tuple(new_table_names)
    if not all_silver_inputs:
        raise ScaffoldError("product new: at least one --silver-input or --input is required")
    if len(set(all_silver_inputs)) != len(all_silver_inputs):
        raise ScaffoldError(f"product {product!r}: silver inputs must be unique, got {all_silver_inputs!r}")

    resolved_display_name = display_name or title_case(product)
    product_block = _render_product_block(product=product, display_name=resolved_display_name)
    governance_text = domain_module.add_governance_product(repo_root, governance_text, domain, product_block)

    models_dir = f"{_GOLD}/models/{product}"
    gold_schema = tuple((name, "Generated Gold mart; describe its grain and metrics here.") for name in gold_tables)

    shared_files, shared_edits = _gold_project_changes(
        repo_root, domain=domain, product=product, silver_tables=all_silver_inputs
    )
    edits.extend(shared_edits)
    product_files = [
        *shared_files,
        ScaffoldFile(
            f"{models_dir}/schema.yml",
            _templates.render_gold_schema_yml(product=product, gold_tables=gold_schema),
        ),
        ScaffoldFile(
            f"lakehouse_code/pipelines/dagster/{product}.py",
            _templates.render_dagster_product_module(domain=domain, product=product),
        ),
    ]
    for mart in gold_tables:
        product_files.append(
            ScaffoldFile(
                f"{models_dir}/{mart}.sql",
                _templates.render_gold_mart_sql(mart=mart, domain=domain, first_silver_input=all_silver_inputs[0]),
            )
        )

    if with_report:
        # No `metadata.yaml`: it is what makes a directory a bundle to
        # `discover_report_dirs`, so leaving it out keeps this directory out of
        # the dashboards until a dashboard is exported into it. Writing it
        # here (#201) would fail its own promotion contract before a
        # dashboard exists (`exports no Superset dashboard`). The database and
        # dataset drafts are still written so authoring in Superset has
        # something to connect. See #205.
        dashboard_dir = f"lakehouse_code/dashboards/superset/{product}"
        product_files.append(
            ScaffoldFile(
                f"{dashboard_dir}/databases/openlakeforge_trino.yaml",
                _templates.render_superset_database_yaml(),
            )
        )
        for mart in gold_tables:
            product_files.append(
                ScaffoldFile(
                    f"{dashboard_dir}/datasets/OpenLakeForge_Trino/{mart}.yaml",
                    _templates.render_superset_dataset_yaml(
                        dashboard=product,
                        mart=mart,
                        gold_namespace=f"{product}_gold",
                        description="Generated Gold mart; describe its grain and metrics here.",
                    ),
                )
            )
        product_files.append(
            ScaffoldFile(
                f"{dashboard_dir}/README.md",
                _templates.render_superset_readme(display_name=resolved_display_name, report_source_dir=dashboard_dir),
            )
        )

    all_files = tuple(extra_files) + tuple(product_files)
    summary = [
        f"Created product {domain}/{product} with {len(gold_tables)} Gold table(s): {', '.join(gold_tables)}.",
    ]
    if not domain_exists:
        summary.insert(0, f"Domain {domain!r} did not exist; created it with {len(inputs)} Silver table(s).")
    elif inputs:
        summary.append(
            f"Extended domain {domain!r} with {len(inputs)} new Silver table(s): {', '.join(new_table_names)}."
        )
    if with_report:
        summary.append(
            f"Generated a draft Superset report skeleton at lakehouse_code/dashboards/superset/{product}/ "
            "(not yet promotable -- author the dashboard in Superset and export it back; "
            "see the bundle's README.md)."
        )

    sources = domain_module.floe_sources_edit(repo_root, inputs) if inputs else None
    if sources is not None:
        edits.append(sources)
    return ScaffoldPlan(files=all_files, governance_yml=governance_text, summary=tuple(summary), edits=tuple(edits))
