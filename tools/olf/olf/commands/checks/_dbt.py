"""`olf check dbt`: compile every dbt product and check its relation contract."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import typer

from olf.commands._shared import fail
from olf.commands.checks._shared import (
    _check_cache_root,
    _distribution_root_for,
    _root,
    _run,
    _uv_pip_install,
)


def dbt(repo_root: str = typer.Option("", "--repo-root", help="Checkout or project root to validate.")) -> None:
    """Render, resolve, parse, and compile every discovered dbt product."""
    from olf.project import ProjectSpec

    root = _root(repo_root)
    # `libs` is distribution-owned, not project-owned (ADR 0009): an
    # installed project's root has only `lakehouse_code/`, so the
    # distribution root must be on sys.path too, not just the project root.
    # Mirrors `olf dbt parse`'s identical fix. `_distribution_root_for`
    # prefers `root` itself when `--repo-root` selects a complete, separate
    # checkout, so that checkout's own `libs/dbt/render_profiles` wins.
    project_spec = ProjectSpec(root=root, distribution_root=_distribution_root_for(root))
    for path in (str(project_spec.root), str(project_spec.distribution_root)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from libs.dbt.render_profiles import discover_project_dirs, write_profile

    projects = discover_project_dirs(project_spec.gold_root)
    if not projects:
        raise typer.Exit(code=fail("no product dbt projects found"))
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
        "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
    }
    for project in projects:
        write_profile(project, environment="local")
        _run([dbt_bin, "deps", "--project-dir", str(project)], cwd=project_spec.root, env=env)
        _run(
            [dbt_bin, "parse", "--project-dir", str(project), "--profiles-dir", str(project), "--target", "local"],
            cwd=project_spec.root,
            env=env,
        )
        _run(
            [
                dbt_bin,
                "compile",
                "--project-dir",
                str(project),
                "--profiles-dir",
                str(project),
                "--target",
                "local",
                "--no-introspect",
                "--no-populate-cache",
            ],
            cwd=project_spec.root,
            env=env,
        )
        _validate_dbt_relation_contract(project, root=project_spec.root, catalog=env["OPENLAKEFORGE_CATALOG_NAME"])
    typer.echo("dbt projects compiled and relation contracts are valid.")


def _validate_dbt_relation_contract(project: Path, *, root: Path, catalog: str) -> None:
    """Assert compiled Gold models consume only their descriptor-owned Silver schema."""
    from openlakeforge_domain import load_lakehouse_inventory

    manifest_path = project / "target/manifest.json"
    if not manifest_path.is_file():
        raise typer.Exit(code=fail(f"dbt did not create {manifest_path}"))
    try:
        product = project.relative_to(root / "lakehouse_code/gold").parts[0]
    except (ValueError, IndexError) as exc:
        raise typer.Exit(code=fail(f"cannot derive product from dbt path: {project}")) from exc
    inventory = load_lakehouse_inventory(root / "lakehouse_code")
    descriptor = next((item for item in inventory.products if item.id == product), None)
    if descriptor is None:
        raise typer.Exit(code=fail(f"cannot resolve dbt product {product!r} from lakehouse.yaml"))
    manifest = json.loads(manifest_path.read_text())
    expected_gold = f"{product}_gold"
    expected_silver = f"{descriptor.domain_name}_silver"
    models = [
        f"{node.get('name')}: {node.get('database')}.{node.get('schema')}"
        for node in manifest.get("nodes", {}).values()
        if node.get("resource_type") == "model"
        and (node.get("database"), node.get("schema")) != (catalog, expected_gold)
    ]
    sources = [
        f"{source.get('name')}: {source.get('database')}.{source.get('schema')}"
        for source in manifest.get("sources", {}).values()
        if (source.get("database"), source.get("schema")) != (catalog, expected_silver)
    ]
    if models or sources:
        details = [
            *(f"Gold models must use {catalog}.{expected_gold}: {item}" for item in models),
            *(f"Silver sources must use {catalog}.{expected_silver}: {item}" for item in sources),
        ]
        raise typer.Exit(code=fail("\n".join(details)))
