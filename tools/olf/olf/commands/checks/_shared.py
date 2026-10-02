"""Helpers shared by the `olf check` targets."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import typer

from olf import config
from olf.commands._shared import fail
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentError


def _root(repo_root: str) -> Path:
    return Path(repo_root or config.repo_root()).resolve()


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    try:
        Toolkit.default().runner.run(argv, cwd=cwd, env=env, stream_output=True)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def _uv_pip_install(*, target: Path, requirements: list[str], cwd: Path) -> None:
    """Install a check-only dependency set without requiring pip in uv's venv."""
    uv = str(Toolkit.default().resolver.resolve("uv"))
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(target),
            "--no-compile",
            *requirements,
        ],
        cwd=cwd,
    )


def _distribution_root_for(root: Path) -> Path:
    """Resolve the distribution root to validate/import alongside `root`.

    An explicit `--repo-root` can point at a complete, separate checkout —
    one with its own `infra/terraform` — and that checkout's own Terraform,
    Helm, schema, and `libs/` must be what gets checked, not the executing
    `olf`'s. Only an installed-project-shaped root (`lakehouse_code/` only,
    no `infra/terraform` of its own) falls back to the runtime payload.
    """
    from olf.distribution import runtime_layout

    if (root / "infra" / "terraform").is_dir():
        return root
    return runtime_layout().distribution_root


def _check_cache_root(project_root: Path, distribution_root: Path) -> Path:
    """Keep check caches outside a selected external data project."""
    if project_root == distribution_root:
        return project_root / ".cache"
    from olf.distribution import runtime_layout

    project_key = hashlib.sha256(str(project_root).encode()).hexdigest()[:12]
    return runtime_layout().cache_root / "checks" / project_key
