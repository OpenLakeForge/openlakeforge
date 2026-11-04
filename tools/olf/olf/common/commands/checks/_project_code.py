"""`olf check project-code`: load merged Dagster definitions and run the runtime tests."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import typer

from olf.common.commands.checks._shared import (
    _check_cache_root,
    _distribution_root_for,
    _refuse_legacy_dbt_packages,
    _root,
    _run,
    _uv_pip_install,
)
from olf.common.deployment.errors import DeploymentError
from olf.common.deployment.project_dependencies import check_project_requirements, platform_constraints
from olf.common.log import fail
from olf.common.tooling.toolkit import Toolkit


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
    _refuse_legacy_dbt_packages(project.gold_root)
    cache = _check_cache_root(project.root, project.distribution_root) / "project-code"
    project_digest = hashlib.sha256(
        (project.distribution_root / "images/project-code/pyproject.toml").read_bytes()
    ).hexdigest()[:16]
    requirements = project.code_root / "requirements.txt"
    if requirements.is_file():
        try:
            check_project_requirements(
                requirements,
                project.distribution_root / "images/project-code/requirements.lock",
                tools=Toolkit.default(),
            )
        except DeploymentError as exc:
            raise typer.Exit(code=fail(str(exc))) from exc
        project_digest = hashlib.sha256((project_digest + requirements.read_text()).encode()).hexdigest()[:16]
    domain_digest = _source_tree_digest(project.distribution_root / "packages/core")
    site = cache / f"py{sys.version_info.major}{sys.version_info.minor}-{project_digest}-{domain_digest}" / "site"
    if not (site / ".complete").is_file():
        site.mkdir(parents=True, exist_ok=True)
        pyproject = tomllib.loads((project.distribution_root / "images/project-code/pyproject.toml").read_text())
        _uv_pip_install(target=site, requirements=pyproject["project"]["dependencies"], cwd=project.root)
        if requirements.is_file():
            # Held to the platform pins, as the image build does, so the check runs the image's versions.
            constraints = site.parent / "platform-constraints.txt"
            lock = project.distribution_root / "images/project-code/requirements.lock"
            constraints.write_text(platform_constraints(lock.read_text()))
            _uv_pip_install(
                target=site, requirements=["-r", str(requirements), "-c", str(constraints)], cwd=project.root
            )
        _install_core(project.distribution_root / "packages/core", site, cwd=project.root)
        (site / ".complete").touch()
    # The distribution's baked profiles name only the products it was built
    # with; loading the definitions renders this project's own here instead
    # (#627). Emptied each run so a renamed or added product gets its profile.
    dbt_profiles = cache / "dbt-profiles"
    shutil.rmtree(dbt_profiles, ignore_errors=True)
    env = {
        "PATH": f"{site / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": f"{site}{os.pathsep}{project.root}{os.pathsep}{project.distribution_root / 'packages/runtime'}",
        # The stage binding openlakeforge_runtime.floe_profile renders the Floe profile from;
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
        "OPENLAKEFORGE_DBT_PROFILES_DIR": str(dbt_profiles),
        "OPENLAKEFORGE_DBT_PACKAGES_DIR": str(project.distribution_root / "packages/runtime/openlakeforge_runtime/dbt"),
    }
    _run([sys.executable, "-m", "olf.common.project_code_check", str(project.root)], cwd=project.root, env=env)
    # The image build runs the same two commands (#725). The install is the
    # only step with network; the verify loads every extension with
    # downloads disabled. Cached per DuckDB version, which is in the path.
    extensions = str(site.parent / "duckdb-extensions")
    for action in ("install", "verify"):
        argv = [sys.executable, "-m", "openlakeforge_runtime.duckdb_extensions", action, extensions]
        _run(argv, cwd=project.root, env=env)
    # The runtime tests need Dagster, dlt and kubernetes, which only this
    # environment has; under the plain `pytest tools/olf/tests` gate they skip.
    runtime_tests = project.distribution_root / "tools/olf/tests/runtime"
    if runtime_tests.is_dir():
        pytest = [sys.executable, "-m", "pytest", str(runtime_tests), "-q", "-p", "no:cacheprovider"]
        _run(pytest, cwd=project.root, env=env)


def _install_core(source: Path, site: Path, *, cwd: Path) -> None:
    """Install the domain model from a writable copy of `source` (#626).

    setuptools writes `egg-info` into the directory it builds, and an installed
    `olf` payload under `OLF_HOME` is read-only (ADR 0009). Copying also keeps
    build output out of a source checkout, where it would change the cache digest.
    """
    with tempfile.TemporaryDirectory(prefix="olf-core-") as work:
        copy = Path(work) / source.name
        # copyfile, not copy2: the payload's read-only modes must not carry over.
        shutil.copytree(
            source,
            copy,
            copy_function=shutil.copyfile,
            ignore=shutil.ignore_patterns("*.egg-info", "build", "__pycache__"),
        )
        for directory in (copy, *(path for path in copy.rglob("*") if path.is_dir())):
            directory.chmod(0o755)
        _uv_pip_install(target=site, requirements=[str(copy)], cwd=cwd)


def _source_tree_digest(directory: Path) -> str:
    """Hash source paths and contents so editable project dependencies cannot go stale."""
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
