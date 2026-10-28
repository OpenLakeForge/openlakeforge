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
from olf.release._manifest import load_catalog

# arch -> (polaris image, polaris-admin-tool image), verbatim from
# scripts/local/cluster/prefetch-images.sh. The fallback branch's Polaris
# image is deliberately left without a digest pin - do not "fix" this.
_POLARIS_IMAGES: dict[str, tuple[str, str]] = {
    "arm64": (
        "apache/polaris:1.4.0@sha256:705f7c0294bee6e8f1586276205d5525a714ed134549ab6f945850b6ee85fc94",
        "apache/polaris-admin-tool:1.4.0@sha256:c5313c03a1575bca4e1a85d807417a08de3f213bccb6a0fffd321c8dcef6994f",
    ),
    "aarch64": (
        "apache/polaris:1.4.0@sha256:705f7c0294bee6e8f1586276205d5525a714ed134549ab6f945850b6ee85fc94",
        "apache/polaris-admin-tool:1.4.0@sha256:c5313c03a1575bca4e1a85d807417a08de3f213bccb6a0fffd321c8dcef6994f",
    ),
    "amd64": (
        "apache/polaris:1.4.0@sha256:f4676e56a3a64bfa742c8ace36e6eb9f28697e4a6c2eca46b4869e094edb4d41",
        "apache/polaris-admin-tool:1.4.0@sha256:3b13addc425766cd1e39fc33f1496dbd8512c00e2d87b0a1f5b932f8195cfd2c",
    ),
    "x86_64": (
        "apache/polaris:1.4.0@sha256:f4676e56a3a64bfa742c8ace36e6eb9f28697e4a6c2eca46b4869e094edb4d41",
        "apache/polaris-admin-tool:1.4.0@sha256:3b13addc425766cd1e39fc33f1496dbd8512c00e2d87b0a1f5b932f8195cfd2c",
    ),
}
# ponytail: the OpenMetadata server is not in the component catalog; pin it here until it is.
_OPENMETADATA_SERVER_IMAGE = "docker.getcollate.io/openmetadata/server:1.13.6"

_ALWAYS = (
    "seaweedfs",
    "traefik",
    "cert_manager_controller",
    "cert_manager_webhook",
    "cert_manager_cainjector",
    "postgres",
    "trino",
)


def polaris_images(server_arch: str, catalog_images: Mapping[str, str]) -> tuple[str, str]:
    # kind imports a single-platform image, so known arches use their per-arch digest; the catalog pins the index.
    return _POLARIS_IMAGES.get(server_arch) or (catalog_images["polaris"], catalog_images["polaris_admin_tool"])


def load_catalog_images(config: LocalDeploymentConfig) -> dict[str, str]:
    catalog = load_catalog(config.paths.distribution_root / "release" / "component-catalog.yaml")
    return dict(catalog["components"]["images"])


def selected_images(
    features: DeploymentFeatures,
    *,
    server_arch: str,
    catalog_images: Mapping[str, str],
    perimeter: bool = True,
    dex: bool = False,
) -> tuple[str, ...]:
    names = list(_ALWAYS)
    if features.governance_enabled:
        names += ["opensearch", "openmetadata_ingestion"]
    if features.analytics_enabled:
        names += ["superset_base", "superset_init", "superset_redis"]
    if perimeter:
        names.append("oauth2_proxy")
    if dex:
        names.append("dex")
    images = [catalog_images[name] for name in names]
    images += polaris_images(server_arch, catalog_images)
    if features.governance_enabled:
        images.append(_OPENMETADATA_SERVER_IMAGE)
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
    identity = config.context.topology.identity
    images = selected_images(
        config.features,
        server_arch=server_arch,
        catalog_images=load_catalog_images(config),
        perimeter=identity is not None,
        dex=identity is not None and identity.issuer == "github",
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


def load_image_into_nodes(config: LocalDeploymentConfig, tools: Toolkit, image: str, *, env: Mapping[str, str]) -> None:
    """Prefetch a single image, for callers that run before the prefetch phase."""
    nodes = tools.kind.get_nodes(config.cluster.name, env=env)
    if not nodes:
        raise DeploymentPreconditionError(f"kind cluster '{config.cluster.name}' has no nodes.")
    with tempfile.TemporaryDirectory(prefix="olf-prefetch-") as scratch:
        _prefetch_one_image(image, tools, nodes=nodes, env=env, scratch_dir=Path(scratch), config=config)


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
    # Every node holds its own copy now; keeping the archive until the run ends doubled the disk use.
    archive.unlink(missing_ok=True)
