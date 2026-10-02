"""`olf check project-code`: load merged Dagster definitions."""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
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


def project_code(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Load the merged Dagster definitions in the project-code dependency set."""
    from olf.project import ProjectSpec

    root = _root(repo_root)
    project = ProjectSpec(root=root, distribution_root=_distribution_root_for(root))
    if sys.version_info[:2] != (3, 12):
        raise typer.Exit(
            code=fail(
                f"project-code check requires Python >=3.12,<3.13; found {sys.version_info[0]}.{sys.version_info[1]}"
            )
        )
    cache = _check_cache_root(project.root, project.distribution_root) / "project-code"
    project_digest = hashlib.sha256(
        (project.distribution_root / "images/project-code/pyproject.toml").read_bytes()
    ).hexdigest()[:16]
    domain_digest = _source_tree_digest(project.distribution_root / "packages/domain-model")
    site = cache / f"py{sys.version_info.major}{sys.version_info.minor}-{project_digest}-{domain_digest}" / "site"
    if not (site / ".complete").is_file():
        site.mkdir(parents=True, exist_ok=True)
        pyproject = tomllib.loads((project.distribution_root / "images/project-code/pyproject.toml").read_text())
        _uv_pip_install(target=site, requirements=pyproject["project"]["dependencies"], cwd=project.root)
        _uv_pip_install(
            target=site,
            requirements=[str(project.distribution_root / "packages/domain-model")],
            cwd=project.root,
        )
        (site / ".complete").touch()
    env = {
        "PATH": f"{site / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": f"{site}{os.pathsep}{project.root}{os.pathsep}{project.distribution_root}",
        "OPENLAKEFORGE_FLOE_MANIFEST_ACCESS_MODE": "remote",
        "OPENLAKEFORGE_OPS_BUCKET_NAME": "openlakeforge-ops",
        "OPENLAKEFORGE_ARTIFACT_BUCKET_NAME": "openlakeforge-ops",
        "OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://openlakeforge-ops/artifacts",
        "OPENLAKEFORGE_FLOE_MANIFEST_BASE_URI": "s3://openlakeforge-ops/floe/manifests",
        "OPENLAKEFORGE_FLOE_REPORT_BASE_URI": "s3://openlakeforge-ops/floe/reports",
        "OPENLAKEFORGE_LOG_BASE_URI": "s3://openlakeforge-ops/logs",
        "OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI": "s3://openlakeforge-ops/runs",
    }
    _run([sys.executable, "-m", "olf.project_code_check", str(project.root)], cwd=project.root, env=env)


def _source_tree_digest(directory: Path) -> str:
    """Hash source paths and contents so editable project dependencies cannot go stale."""
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
