"""`olf check dbt`: parse and compile the Gold dbt project and check its relation contract."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import typer

from olf.common.commands.checks._shared import (
    _check_cache_root,
    _distribution_root_for,
    _refuse_per_product_dbt_projects,
    _root,
    _run,
    _uv_pip_install,
)
from olf.common.log import fail


def dbt(repo_root: str = typer.Option("", "--repo-root", help="Checkout or project root to validate.")) -> None:
    """Render, resolve, parse, and compile the Gold dbt project."""
    from olf.common.project import ProjectSpec

    root = _root(repo_root)
    project_spec = ProjectSpec(root=root, distribution_root=_distribution_root_for(root))
    from openlakeforge_runtime.dbt.render_profiles import dbt_environment, gold_project_dir, write_profile

    _refuse_per_product_dbt_projects(project_spec.gold_root)
    project = gold_project_dir(project_spec.gold_root)
    if project is None:
        raise typer.Exit(code=fail(f"no Gold dbt project at {project_spec.gold_root / 'dbt_project.yml'}"))
    cache = _check_cache_root(project_spec.root, project_spec.distribution_root) / "dbt"
    dependency_key = hashlib.sha256(b"dbt-trino==1.10.2\nopenlineage-dbt==1.45.0").hexdigest()[:16]
    site = cache / f"py{sys.version_info.major}{sys.version_info.minor}-{dependency_key}" / "site"
    if not (site / ".complete").is_file():
        site.mkdir(parents=True, exist_ok=True)
        _uv_pip_install(
            target=site,
            requirements=["dbt-trino==1.10.2", "openlineage-dbt==1.45.0"],
            cwd=project_spec.root,
        )
        (site / ".complete").touch()
    dbt_bin = str(site / "bin/dbt")
    env = {
        "PATH": f"{site / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": f"{site}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "AWS_ACCESS_KEY_ID": "openlakeforge",
        "AWS_SECRET_ACCESS_KEY": "openlakeforge",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ENDPOINT_URL_S3": "http://seaweedfs-s3:8333",
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino",
        "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
        "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "iceberg",
        "OPENLAKEFORGE_DBT_TRINO_USER": "openlakeforge-dbt",
        "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
        **dbt_environment(),
    }
    # Rendered outside the checkout so the check never leaves a
    # profiles.yml behind for the next `olf infra project build` (#546).
    with tempfile.TemporaryDirectory(prefix="olf-dbt-profiles-") as profiles_dir:
        write_profile(project, output_dir=Path(profiles_dir))
        _check_project(dbt_bin, project, profiles_dir=profiles_dir, cwd=project_spec.root, env=env)
    _validate_dbt_relation_contract(project, root=project_spec.root, catalog=env["OPENLAKEFORGE_CATALOG_NAME"])
    typer.echo("Gold dbt project compiled and its relation contract is valid.")


def _check_project(dbt_bin: str, project: Path, *, profiles_dir: str, cwd: Path, env: dict[str, str]) -> None:
    _run([dbt_bin, "deps", "--project-dir", str(project)], cwd=cwd, env=env)
    _run([dbt_bin, "parse", "--project-dir", str(project), "--profiles-dir", profiles_dir], cwd=cwd, env=env)
    # `is_incremental()` asks the adapter whether `{{ this }}` exists, and this
    # check has no warehouse: compiling an incremental model fails with
    # "connection never acquired for thread" (#752). Those models are covered
    # by `parse` (Jinja, refs, sources) but their SQL is never rendered here.
    _run(
        [
            dbt_bin,
            "compile",
            "--project-dir",
            str(project),
            "--profiles-dir",
            profiles_dir,
            "--no-introspect",
            "--no-populate-cache",
            "--exclude",
            "config.materialized:incremental",
        ],
        cwd=cwd,
        env=env,
    )


def _validate_dbt_relation_contract(project: Path, *, root: Path, catalog: str) -> None:
    """Assert each product's models land in its Gold schema and read only its own domain's Silver."""
    from openlakeforge_core import load_lakehouse_inventory

    manifest_path = project / "target/manifest.json"
    if not manifest_path.is_file():
        raise typer.Exit(code=fail(f"dbt did not create {manifest_path}"))
    inventory = load_lakehouse_inventory(root / "lakehouse_code")
    product_domains = {product.id: product.domain_name for product in inventory.products}
    domains = {domain.name for domain in inventory.domains}
    manifest = json.loads(manifest_path.read_text())
    project_name = manifest.get("metadata", {}).get("project_name")
    nodes = manifest.get("nodes", {})
    sources = manifest.get("sources", {})
    details: list[str] = []
    for node in nodes.values():
        if node.get("resource_type") != "model" or node.get("package_name") != project_name:
            continue
        name = node.get("name")
        fqn = node.get("fqn", [])
        product = fqn[1] if len(fqn) > 2 else None
        if product not in product_domains:
            details.append(f"Gold model {name} is not in a governance.yml product's folder under models/")
            continue
        relation = (node.get("database"), node.get("schema"))
        if relation != (catalog, f"{product}_gold"):
            details.append(f"Gold models must use {catalog}.{product}_gold: {name}: {relation[0]}.{relation[1]}")
        # Polaris re-reads a definer view without the caller's token, which breaks a keyless catalog (#967).
        if node.get("config", {}).get("view_security") != "invoker":
            details.append(f"Gold models must set view_security: invoker: {name}")
        details.extend(
            f"product {product} of domain {product_domains[product]} must not read "
            f"{sources[parent]['source_name']}.{sources[parent]['name']}: {name}"
            for parent in node.get("depends_on", {}).get("nodes", [])
            if parent in sources and sources[parent].get("source_name") != product_domains[product]
        )
    for source in sources.values():
        domain = source.get("source_name")
        if domain not in domains:
            details.append(f"Silver source {domain!r} is not a governance.yml domain: {source.get('name')}")
        elif (source.get("database"), source.get("schema")) != (catalog, f"{domain}_silver"):
            details.append(
                f"Silver sources must use {catalog}.{domain}_silver: "
                f"{source.get('name')}: {source.get('database')}.{source.get('schema')}"
            )
    if details:
        raise typer.Exit(code=fail("\n".join(details)))
