"""Project-root resolution shared by the writable user-facing commands."""

from __future__ import annotations

import os
from pathlib import Path

from olf.distribution import DistributionError, RuntimeLayout, runtime_layout


def writable_project_root(explicit_root: str) -> Path:
    """Resolve a writable project, defaulting installed mode to the cwd."""
    return writable_project_layout(explicit_root).project_root


def writable_project_layout(explicit_root: str) -> RuntimeLayout:
    """Resolve the project together with the platform payload that owns its schemas.

    An installed distribution's payload is immutable, so the project defaults
    to the current directory -- the one `olf init` wrote. A source checkout
    keeps the checkout itself as the contributor default. Either way an
    explicit `--repo-root`/`--project-root` wins."""
    environ = dict(os.environ)
    if explicit_root:
        environ["OPENLAKEFORGE_PROJECT_ROOT"] = explicit_root
    try:
        return runtime_layout(environ)
    except DistributionError as exc:
        raise RuntimeError(str(exc)) from exc
