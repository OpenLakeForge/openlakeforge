"""Shared runtime settings read from the contract environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from olf.common.project import ProjectSpec


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def namespace(*, environ: Mapping[str, str] | None = None) -> str:
    """The selected stage's namespace, where Dagster and Superset run."""
    source = _environ(environ)
    return source.get("NAMESPACE") or source.get("OPENLAKEFORGE_KUBE_NAMESPACE") or "olf-dev"


def shared_namespace(*, environ: Mapping[str, str] | None = None) -> str:
    """The namespace owning the shared platform services.

    Trino, Polaris, SeaweedFS, PostgreSQL, and OpenMetadata are deployed once
    per cluster; only the stage-scoped services live in `namespace()`. The
    single-namespace cloud POC roots export the stage namespace for both.
    """
    return _environ(environ).get("OPENLAKEFORGE_SHARED_NAMESPACE") or namespace(environ=environ)


def env(name: str, default: str = "", *, environ: Mapping[str, str] | None = None) -> str:
    value = _environ(environ).get(name)
    return default if value is None or value == "" else value


def repo_root(*, environ: Mapping[str, str] | None = None) -> Path:
    return Path(_environ(environ).get("OPENLAKEFORGE_REPO_ROOT", ".")).resolve()


def distribution_root(*, environ: Mapping[str, str] | None = None) -> Path:
    """Return the platform-resource root selected by the active runtime layout."""
    return Path(_environ(environ).get("OLF_DISTRIBUTION_ROOT", repo_root(environ=environ))).resolve()


def project_root(*, environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-project root, defaulting to the active source root."""
    return Path(_environ(environ).get("OPENLAKEFORGE_PROJECT_ROOT", repo_root(environ=environ))).resolve()


def project_spec(*, environ: Mapping[str, str] | None = None) -> ProjectSpec:
    """Resolve the selected writable project and its platform payload."""
    return ProjectSpec(root=project_root(environ=environ), distribution_root=distribution_root(environ=environ))


def truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "y"}
