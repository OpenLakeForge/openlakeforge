"""`olf check project-code`: load merged Dagster definitions and run the libs tests."""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from pathlib import Path

import typer

from olf.common.commands.checks._shared import _check_cache_root, _distribution_root_for, _root, _run, _uv_pip_install
from olf.common.log import fail


def project_code(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Load the merged Dagster definitions in the project-code dependency set."""
    from olf.common.project import ProjectSpec

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
        # The stage binding libs.floe_profile renders the Floe profile from;
        # the check only builds definitions, so none of these is contacted.
        "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "lakehouse-dev-bronze",
        "OPENLAKEFORGE_STORAGE_SILVER_BUCKET": "lakehouse-dev-silver",
        "OPENLAKEFORGE_STORAGE_REGION": "us-east-1",
        "OPENLAKEFORGE_CATALOG_REST_URI": "http://polaris.olf-system:8181/api/catalog",
        "OPENLAKEFORGE_CATALOG_TOKEN_URI": "http://polaris.olf-system:8181/api/catalog/v1/oauth/tokens",
        "OPENLAKEFORGE_CATALOG_WAREHOUSE": "lakehouse_dev",
        "OPENLAKEFORGE_GOVERNANCE_ENABLED": "false",
        # The domain Jobs' binding: rendered into the profile, never submitted.
        "OPENLAKEFORGE_PROJECT_CODE_IMAGE": "ghcr.io/openlakeforge/project-code@sha256:" + "0" * 64,
        "OPENLAKEFORGE_KUBE_NAMESPACE": "olf-dev",
        "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME": "polaris-dev-floe-creds",
        "OPENLAKEFORGE_OPS_BUCKET_NAME": "openlakeforge-ops",
        "OPENLAKEFORGE_ARTIFACT_BUCKET_NAME": "openlakeforge-ops",
        "OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://openlakeforge-ops/artifacts",
        "OPENLAKEFORGE_LOG_BASE_URI": "s3://openlakeforge-ops/logs",
        "OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI": "s3://openlakeforge-ops/runs",
    }
    _run([sys.executable, "-m", "olf.common.project_code_check", str(project.root)], cwd=project.root, env=env)
    # The libs tests need Dagster, dlt and kubernetes, which only this
    # environment has; under the plain `pytest tools/olf/tests` gate they skip.
    libs_tests = project.distribution_root / "tools/olf/tests/libs"
    if libs_tests.is_dir():
        pytest = [sys.executable, "-m", "pytest", str(libs_tests), "-q", "-p", "no:cacheprovider"]
        _run(pytest, cwd=project.root, env=env)


def _source_tree_digest(directory: Path) -> str:
    """Hash source paths and contents so editable project dependencies cannot go stale."""
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
