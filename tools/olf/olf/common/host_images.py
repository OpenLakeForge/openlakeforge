"""Host Docker images that `olf` created for the local provider, and their removal on destroy.

Ownership is never decided by an image's name or label: a user's image built `FROM` one of ours inherits its
labels. An image is ours only when `olf` recorded it in a file in the work directory: at build time for the
images it builds, and in prefetch for the ones it pulled or tagged. An image the user had before is never
recorded, so it is never removed.

The build cache is not pruned: `docker builder prune` filters by age and type, not by label, so any scope would
also reach another project's cache.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from olf.common import log
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.tooling.toolkit import Toolkit


def _record_path(config: LocalDeploymentConfig) -> Path:
    return config.paths.work_root / "olf-host-images.txt"


def record(config: LocalDeploymentConfig, reference: str) -> None:
    path = _record_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(reference + "\n")


def prune(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    """Never fails: it runs after the cluster is gone. `rmi` without force keeps an image a container uses, and
    only untags one that has other tags (a child image keeps its parent's layers)."""
    path = _record_path(config)
    references = dict.fromkeys(path.read_text().split()) if path.is_file() else {}
    for reference in references:
        try:
            result = tools.docker.rmi(reference, env=env)
        except Exception as exc:  # noqa: BLE001 - cleanup must not fail a destroy
            log.warn(f"Could not remove host image {reference}: {exc}")
            continue
        if not result.ok:
            log.warn(f"Kept host image {reference}: {result.stderr.strip()}")
    path.unlink(missing_ok=True)
    log.step("Removed the host Docker images olf created. The next deploy pulls or rebuilds them.")
