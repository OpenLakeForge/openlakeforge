"""Docker build contexts for the project-code image."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import yaml

from olf.deployment.context import DeploymentContext, DeploymentPaths
from olf.deployment.errors import DeploymentPreconditionError
from olf.profile import resolve_declared_project


@contextmanager
def project_code_build_context(paths: DeploymentPaths) -> Iterator[Path]:
    """Yield a context with platform build assets and the selected project.

    The immutable distribution supplies the Dockerfile, dependency lock and
    shared libraries.  A writable ``--project-root`` supplies only product
    code, so its ``lakehouse_code`` must replace the bundled demo before the
    image is built.  Source mode has one tree already and needs no staging.
    """
    if paths.project.root == paths.project.distribution_root:
        yield paths.distribution_root
        return

    paths.work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="project-code-context-", dir=paths.work_root) as temporary:
        root = Path(temporary)
        for relative in ("images/project-code", "packages/domain-model", "libs"):
            shutil.copytree(paths.distribution_root / relative, root / relative)
        shutil.copytree(paths.project.code_root, root / "lakehouse_code")
        yield root


_GENERATED_PARTS = frozenset({"__pycache__", "target", "dbt_packages", "logs"})


def _code_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not _GENERATED_PARTS.intersection(path.relative_to(root).parts)
    }


def split_published_image(published: str) -> tuple[str, str]:
    """`repository:version@sha256:...` -> (repository, `version@sha256:...`); a pin without a digest is an error."""
    if "@sha256:" not in published:
        raise DeploymentPreconditionError(f"the pinned example project image {published!r} has no digest.")
    name, _, digest = published.partition("@")
    repository, _, version = name.rpartition(":")
    return repository, f"{version}@{digest}"


def published_example_image(context: DeploymentContext, environ: Mapping[str, str]) -> str | None:
    """The release-pinned example project image, only when the project is the unmodified example.

    The catalog of a source checkout does not carry the pin: a release stamps its own digest in when it publishes the
    image (`release.yml`), so that image and the bundled `lakehouse_code/` come from the same commit. A checkout
    without one builds the example instead. The same goes for a declared project, an image override, an explicit
    `PROJECT_CODE_IMAGE_TAG` and a writable `lakehouse_code/` that differs from the bundled one: `olf init` writes no
    `spec.project`, and a user's edits must reach the cluster, never be replaced by the demo.
    """
    topology = context.topology
    if resolve_declared_project(topology.project, topology.provider, environ).source != "public-example":
        return None
    if environ.get("PROJECT_CODE_IMAGE_TAG"):
        return None
    catalog_path = context.paths.distribution_root / "release/component-catalog.yaml"
    if not catalog_path.is_file():
        return None
    components = (yaml.safe_load(catalog_path.read_text()) or {}).get("components") or {}
    pinned = (components.get("images") or {}).get("project_code")
    project_code = context.paths.repo_root / "lakehouse_code"
    bundled = context.paths.distribution_root / "lakehouse_code"
    if pinned and project_code != bundled and _code_tree(project_code) != _code_tree(bundled):
        return None
    return pinned
