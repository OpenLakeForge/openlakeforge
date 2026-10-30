"""Shared runtime settings read from the contract environment."""

from __future__ import annotations

import os
from pathlib import Path

from olf.project import ProjectSpec


def namespace() -> str:
    """The selected stage's namespace, where Dagster and Superset run."""
    return os.environ.get("NAMESPACE") or os.environ.get("OPENLAKEFORGE_KUBE_NAMESPACE") or "olf-dev"


def shared_namespace() -> str:
    """The namespace owning the shared platform services.

    Trino, Polaris, SeaweedFS, PostgreSQL, and OpenMetadata are deployed once
    per cluster; only the stage-scoped services live in `namespace()`. The
    single-namespace cloud POC roots export the stage namespace for both.
    """
    return os.environ.get("OPENLAKEFORGE_SHARED_NAMESPACE") or namespace()


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def repo_root() -> Path:
    return Path(os.environ.get("OPENLAKEFORGE_REPO_ROOT", ".")).resolve()


def distribution_root() -> Path:
    """Return the platform-resource root selected by the active runtime layout."""
    return Path(os.environ.get("OLF_DISTRIBUTION_ROOT", repo_root())).resolve()


def project_root() -> Path:
    """Return the user-project root, defaulting to the active source root."""
    return Path(os.environ.get("OPENLAKEFORGE_PROJECT_ROOT", repo_root())).resolve()


def project_spec() -> ProjectSpec:
    """Resolve the selected writable project and its platform payload."""
    return ProjectSpec(root=project_root(), distribution_root=distribution_root())


def truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "y"}
