"""Local kind image prefetch.

Port of `scripts/local/cluster/prefetch-images.sh`: pre-pulls heavy images
directly into every kind node's containerd store so Helm releases don't time
out on first deploy. Preserves the digest/containerd/CRI handling verbatim -
see `_load_image_into_kind_nodes` for the `docker save`/`ctr import`/
`crictl pull` dance and why it exists.
"""

from __future__ import annotations

import platform as _platform
import tempfile
from collections.abc import Mapping
from pathlib import Path

from olf import log
from olf.deployment.context import DeploymentFeatures
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.local.config import LocalDeploymentConfig

_POLARIS_VERSION = "1.4.0"

# arch -> (polaris image, polaris-admin-tool image), verbatim from
# scripts/local/cluster/prefetch-images.sh. The fallback branch's Polaris
# image is deliberately left without a digest pin - do not "fix" this.
_POLARIS_IMAGES: dict[str, tuple[str, str]] = {
    "arm64": (
        f"apache/polaris:{_POLARIS_VERSION}@sha256:705f7c0294bee6e8f1586276205d5525a714ed134549ab6f945850b6ee85fc94",
        f"apache/polaris-admin-tool:{_POLARIS_VERSION}@sha256:c5313c03a1575bca4e1a85d807417a08de3f213bccb6a0fffd321c8dcef6994f",
    ),
    "aarch64": (
        f"apache/polaris:{_POLARIS_VERSION}@sha256:705f7c0294bee6e8f1586276205d5525a714ed134549ab6f945850b6ee85fc94",
        f"apache/polaris-admin-tool:{_POLARIS_VERSION}@sha256:c5313c03a1575bca4e1a85d807417a08de3f213bccb6a0fffd321c8dcef6994f",
    ),
    "amd64": (
        f"apache/polaris:{_POLARIS_VERSION}@sha256:f4676e56a3a64bfa742c8ace36e6eb9f28697e4a6c2eca46b4869e094edb4d41",
        f"apache/polaris-admin-tool:{_POLARIS_VERSION}@sha256:3b13addc425766cd1e39fc33f1496dbd8512c00e2d87b0a1f5b932f8195cfd2c",
    ),
    "x86_64": (
        f"apache/polaris:{_POLARIS_VERSION}@sha256:f4676e56a3a64bfa742c8ace36e6eb9f28697e4a6c2eca46b4869e094edb4d41",
        f"apache/polaris-admin-tool:{_POLARIS_VERSION}@sha256:3b13addc425766cd1e39fc33f1496dbd8512c00e2d87b0a1f5b932f8195cfd2c",
    ),
}
_POLARIS_IMAGES_FALLBACK = (
    f"apache/polaris:{_POLARIS_VERSION}",
    f"apache/polaris-admin-tool:{_POLARIS_VERSION}@sha256:7ef7557b528964e792caeaef3908434bd99c7d2f994caa654da1d77c6b428a80",
)

_OPENSEARCH_IMAGE = "opensearchproject/opensearch:2.11.0"
_OPENMETADATA_SERVER_IMAGE = "docker.getcollate.io/openmetadata/server:1.13.6"
_OPENMETADATA_INGESTION_IMAGE = "docker.getcollate.io/openmetadata/ingestion-base:1.13.6"
_POSTGRES_IMAGE = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
_KEYCLOAK_IMAGE = (
    "quay.io/keycloak/keycloak:26.6.4@sha256:0aae0de7fca85525f727d3354df17896092de8bb26ae4c12d89c77e5df8cbce4"
)
_KEYCLOAK_CONFIG_CLI_IMAGE = (
    "adorsys/keycloak-config-cli:6.5.1-26.5.5"
    "@sha256:0955d98c8a341898b7aa177477edf8a1e90569ae50bbe7598141c1270b773274"
)
_SUPERSET_DOCKERIZE_IMAGE = "apache/superset:dockerize"
_SUPERSET_IMAGE = "apache/superset:6.1.0@sha256:fb3464528ec7076f91195f0ff7835755aa023e281f1bb78a84782ce7a36b3705"
_REDIS_IMAGE = "docker.io/bitnamilegacy/redis:7.0.10-debian-11-r4"
_FLOE_IMAGE = "ghcr.io/malon64/floe:0.6.11"


def polaris_images(server_arch: str) -> tuple[str, str]:
    return _POLARIS_IMAGES.get(server_arch, _POLARIS_IMAGES_FALLBACK)


def selected_images(features: DeploymentFeatures, *, server_arch: str, keycloak: bool = True) -> tuple[str, ...]:
    polaris_image, polaris_admin_image = polaris_images(server_arch)
    images: list[str] = []
    if features.governance_enabled:
        images.append(_OPENSEARCH_IMAGE)
    images += [polaris_image, polaris_admin_image]
    if features.governance_enabled:
        images += [_OPENMETADATA_SERVER_IMAGE, _OPENMETADATA_INGESTION_IMAGE]
    images.append(_POSTGRES_IMAGE)
    if keycloak:
        images += [_KEYCLOAK_IMAGE, _KEYCLOAK_CONFIG_CLI_IMAGE]
    if features.analytics_enabled:
        images += [_SUPERSET_DOCKERIZE_IMAGE, _SUPERSET_IMAGE, _REDIS_IMAGE]
    images.append(_FLOE_IMAGE)
    return tuple(images)


def archive_reference(image: str) -> str:
    """Strip a trailing `@sha256:...` digest, leaving the plain tag reference."""
    if "@sha256:" not in image:
        return image
    return image.split("@sha256:")[0]


def archive_filename(image: str) -> str:
    translated = image
    for char in "/:@":
        translated = translated.replace(char, "_")
    return f"{translated}.tar"


def image_present_on_all_nodes(docker, image: str, nodes: list[str], *, env: Mapping[str, str]) -> bool:  # noqa: ANN001
    for node in nodes:
        result = docker.exec_(node, ["crictl", "inspecti", image], env=env, check=False)
        if not result.ok:
            return False
    return True


def prefetch_images(
    config: LocalDeploymentConfig,
    tools: Toolkit,
    *,
    env: Mapping[str, str],
    work_dir: Path | None = None,
) -> None:
    server_arch = tools.docker.server_arch(env=env) or _platform.machine()
    images = selected_images(
        config.features, server_arch=server_arch, keycloak=config.context.topology.identity.issuer == "keycloak"
    )

    nodes = tools.kind.get_nodes(config.cluster.name, env=env)
    if not nodes:
        raise DeploymentPreconditionError(f"kind cluster '{config.cluster.name}' has no nodes.")

    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        for image in images:
            _prefetch_one_image(image, tools, nodes=nodes, env=env, scratch_dir=work_dir, config=config)
    else:
        with tempfile.TemporaryDirectory(prefix="olf-prefetch-") as scratch:
            for image in images:
                _prefetch_one_image(image, tools, nodes=nodes, env=env, scratch_dir=Path(scratch), config=config)

    log.step("All images pre-loaded.")


def _prefetch_one_image(
    image: str,
    tools: Toolkit,
    *,
    nodes: list[str],
    env: Mapping[str, str],
    scratch_dir: Path,
    config: LocalDeploymentConfig,
) -> None:
    if image_present_on_all_nodes(tools.docker, image, nodes, env=env):
        log.step(f"Image already present on every kind node: {image}")
        return

    if tools.docker.image_exists(image, env=env):
        log.step(f"Using existing local image {image}...")
    else:
        log.step(f"Pulling {image}...")
        tools.docker.pull(image, env=env, retry_policy=config.prefetch.pull_retry)

    archive_image = archive_reference(image)
    if archive_image != image:
        tools.docker.tag(image, archive_image, env=env)

    archive = scratch_dir / archive_filename(image)
    log.step(f"Saving {archive_image}...")
    tools.docker.save(archive_image, output_path=archive, env=env)

    for node in nodes:
        log.step(f"Loading {image} into kind node '{node}'...")
        tools.docker.exec_(
            node,
            ["ctr", "--namespace=k8s.io", "images", "import", "--snapshotter=overlayfs", "-"],
            privileged=True,
            interactive=True,
            stdin_path=archive,
            env=env,
        )
        if archive_image != image:
            tools.docker.exec_(node, ["crictl", "pull", image], env=env)
