"""`olf product new` -- generate a product-owned Gold dbt project and Dagster
job, referencing existing or newly-declared domain Silver tables.

Never generates a new dlt Bronze loader for a source resource that already
exists: `--input <source>/<resource>` always resolves to the source's own
`load_<source>_entities_to_bronze`, declared once by `olf source new`.
"""

from __future__ import annotations

from pathlib import Path

from openlakeforge_domain import load_transitional_lakehouse_inventory

from olf.scaffold import _templates
from olf.scaffold import domain as domain_module
from olf.scaffold._shared import ScaffoldError, ScaffoldFile, ScaffoldPlan, require_identifier, title_case, yaml_dq


def _parse_target(target: str) -> tuple[str, str]:
    if "/" not in target:
        raise ScaffoldError(f"product new: target {target!r} must be '<domain>/<product>'")
    domain, _, product = target.partition("/")
    if not domain or not product or "/" in product:
        raise ScaffoldError(f"product new: target {target!r} must be '<domain>/<product>'")
    return domain, product


def _render_product_block(*, product: str, domain: str, display_name: str) -> str:
    return (
        f"  - id: {product}\n"
        f"    domain: {domain}\n"
        f"    displayName: {yaml_dq(display_name)}\n"
        f"    description: {yaml_dq(display_name + ' product.')}\n"
        "    status: planned\n"
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
    bronze_inputs_by_table: dict[str, tuple[str, str]] = {}

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
        governance_text = domain_module.add_governance_entry(repo_root, governance_text, "domains", domain_block)
        for source, resource in inputs:
            bronze_inputs_by_table[resource] = (source, resource)
    else:
        target_domain = next(d for d in inventory.domains if d.name == domain)
        existing_silver_by_name = {entity.name: entity for entity in inventory.floe_entities(target_domain)}
        for name in silver_inputs:
            if name not in existing_silver_by_name:
                raise ScaffoldError(
                    f"--silver-input {name!r}: domain {domain!r} has no Silver table {name!r}; "
                    f"known tables: {sorted(existing_silver_by_name)!r}"
                )
            entity = existing_silver_by_name[name]
            bronze_inputs_by_table[name] = (entity.source, entity.resource)
        for source, resource in inputs:
            if resource in existing_silver_by_name:
                raise ScaffoldError(
                    f"--input {source}/{resource}: domain {domain!r} already has a Silver table named {resource!r}; "
                    f"use --silver-input {resource} instead"
                )
            bronze_inputs_by_table[resource] = (source, resource)

        if inputs:
            extra_files.extend(domain_module.floe_entity_files(repo_root, domain=domain, inputs=inputs))

    all_silver_inputs = tuple(silver_inputs) + tuple(new_table_names)
    if not all_silver_inputs:
        raise ScaffoldError("product new: at least one --silver-input or --input is required")
    if len(set(all_silver_inputs)) != len(all_silver_inputs):
        raise ScaffoldError(f"product {product!r}: silver inputs must be unique, got {all_silver_inputs!r}")

    resolved_display_name = display_name or title_case(product)
    product_block = _render_product_block(product=product, domain=domain, display_name=resolved_display_name)
    governance_text = domain_module.add_governance_entry(repo_root, governance_text, "products", product_block)

    gold_dir = f"lakehouse_code/gold/{product}"
    dbt_sources = tuple((name, f"Validated {name.replace('_', ' ')}.") for name in all_silver_inputs)
    gold_schema = tuple(
        (name, "Generated Gold mart; describe its grain and metrics here.") for name in gold_tables
    )
    bronze_inputs = tuple(bronze_inputs_by_table[name] for name in all_silver_inputs)

    product_files = [
        ScaffoldFile(f"{gold_dir}/__init__.py", ""),
        ScaffoldFile(
            f"{gold_dir}/dbt/dbt_project.yml",
            _templates.render_dbt_project_yml(domain=domain, product=product),
        ),
        ScaffoldFile(f"{gold_dir}/dbt/packages.yml", _templates.render_dbt_packages_yml()),
        ScaffoldFile(
            f"{gold_dir}/dbt/models/sources.yml",
            _templates.render_dbt_sources_yml(domain=domain, tables=dbt_sources),
        ),
        ScaffoldFile(
            f"{gold_dir}/dbt/models/gold/schema.yml",
            _templates.render_gold_schema_yml(gold_tables=gold_schema),
        ),
        ScaffoldFile(
            f"lakehouse_code/pipelines/dagster/{product}.py",
            _templates.render_dagster_product_module(
                domain=domain,
                product=product,
                silver_inputs=all_silver_inputs,
                bronze_inputs=bronze_inputs,
                gold_assets=gold_tables,
            ),
        ),
    ]
    for mart in gold_tables:
        product_files.append(
            ScaffoldFile(
                f"{gold_dir}/dbt/models/gold/{mart}.sql",
                _templates.render_gold_mart_sql(mart=mart, first_silver_input=all_silver_inputs[0]),
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
                _templates.render_superset_readme(
                    display_name=resolved_display_name, report_source_dir=dashboard_dir
                ),
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
