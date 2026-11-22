"""Local kind image prefetch.

Port of `scripts/local/cluster/prefetch-images.sh`: pre-pulls heavy images
into every kind node's containerd store so Helm releases don't time out on
first deploy. Catalog images are pulled by each node through CRI; the rest go
through the host's Docker - see `_prefetch_one_image`.
"""

from __future__ import annotations

import platform as _platform
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from olf.common import host_images, log
from olf.common.deployment.context import DeploymentFeatures
from olf.common.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.release._manifest import load_catalog
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.local.images import containerd_name, split_digest_reference

# arch -> (polaris image, polaris-admin-tool image), verbatim from
# scripts/local/cluster/prefetch-images.sh. The fallback branch's Polaris
# image is deliberately left without a digest pin - do not "fix" this.
_POLARIS_IMAGES: dict[str, tuple[str, str]] = {
    "arm64": (
        "apache/polaris:1.9.0@sha256:14bcb2f3f59980cd72dbbdcbc58f51c15753780d1a0d3bf54164fa32e74d91e3",
        "apache/polaris-admin-tool:1.9.0@sha256:d89e2bc14d66511c685e13f7bd75928a22bb7d6712a32e1cc5a369510a3da50d",
    ),
    "aarch64": (
        "apache/polaris:1.9.0@sha256:14bcb2f3f59980cd72dbbdcbc58f51c15753780d1a0d3bf54164fa32e74d91e3",
        "apache/polaris-admin-tool:1.9.0@sha256:d89e2bc14d66511c685e13f7bd75928a22bb7d6712a32e1cc5a369510a3da50d",
    ),
    "amd64": (
        "apache/polaris:1.9.0@sha256:5dabc353c16c7c4acc9de4087b63d85bbf0a40986aa86bd7f393219919834dcd",
        "apache/polaris-admin-tool:1.9.0@sha256:d6adc43ecbfea820006b304a2a71dda82d7c9b7b69beec58fe0b33e038884143",
    ),
    "x86_64": (
        "apache/polaris:1.9.0@sha256:5dabc353c16c7c4acc9de4087b63d85bbf0a40986aa86bd7f393219919834dcd",
        "apache/polaris-admin-tool:1.9.0@sha256:d6adc43ecbfea820006b304a2a71dda82d7c9b7b69beec58fe0b33e038884143",
    ),
}
# ponytail: the OpenMetadata server is not in the component catalog; pin it here until it is.
_OPENMETADATA_SERVER_IMAGE = "docker.io/openmetadata/server:1.13.6"

_ALWAYS = (
    "seaweedfs",
    "fluent_bit",
    "traefik",
    "cert_manager_controller",
    "cert_manager_webhook",
    "cert_manager_cainjector",
    "cert_manager_startupapicheck",
    "dagster_busybox",
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
    portal: bool = False,
    dex: bool = False,
    metrics: bool = False,
    build_superset: bool = False,
    project_code: bool = False,
) -> tuple[str, ...]:
    names = list(_ALWAYS)
    if project_code:
        names.append("project_code")
    if features.governance_enabled:
        names += ["opensearch", "openmetadata_ingestion"]
    if features.analytics_enabled:
        # A locally built Superset is loaded by the platform phase, not pulled.
        names += ["superset_init", "superset_redis"] + ([] if build_superset else ["superset"])
    if portal:
        names.append("portal")
    if perimeter:
        names.append("oauth2_proxy")
    if dex:
        names.append("dex")
    # ADR 0024: Prometheus and kube-state-metrics are deployed only when the profile enables metrics.
    if metrics:
        names += ["prometheus", "kube_state_metrics"]
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
    catalog_images = load_catalog_images(config)
    images = selected_images(
        config.features,
        server_arch=server_arch,
        catalog_images=catalog_images,
        perimeter=identity is not None,
        portal=config.context.topology.access is not None,
        dex=identity is not None and identity.issuer == "github",
        metrics=config.context.topology.observability.metrics_enabled,
        build_superset=config.images.builds_superset(server_arch),
        project_code=config.images.project_code_published,
    )

    nodes = tools.kind.get_nodes(config.cluster.name, env=env)
    if not nodes:
        raise DeploymentPreconditionError(f"kind cluster '{config.cluster.name}' has no nodes.")
    # Only Traefik schedules on the tainted control plane (infra/helm/values/local/traefik.yaml); a cluster
    # without workers (a custom CLUSTER_CONFIG) runs everything on its control plane.
    control_plane = f"{config.cluster.name}-control-plane"
    workers = [node for node in nodes if not node.startswith(control_plane)] or nodes
    targets = {image: nodes if image == catalog_images["traefik"] else workers for image in images}
    # The project-code image may sit in a private registry whose credentials only the host's Docker holds.
    host_pulled = catalog_images.get("project_code")

    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        for image in images:
            _prefetch_one_image(
                image,
                tools,
                nodes=targets[image],
                env=env,
                scratch_dir=work_dir,
                config=config,
                in_node=image != host_pulled,
            )
    else:
        with tempfile.TemporaryDirectory(prefix="olf-prefetch-") as scratch:
            for image in images:
                _prefetch_one_image(
                    image,
                    tools,
                    nodes=targets[image],
                    env=env,
                    scratch_dir=Path(scratch),
                    config=config,
                    in_node=image != host_pulled,
                )

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
    in_node: bool = False,
) -> None:
    if image_present_on_all_nodes(tools.docker, image, nodes, env=env):
        log.step(f"Image already present on every kind node: {image}")
        return

    # An image the host's Docker already holds needs no network at all, so it takes the host path.
    if in_node and not tools.docker.image_exists(image, env=env):
        # A CRI pull keeps the compressed layer blobs and, with the node image's `discard_unpacked_layers`, drops
        # them once unpacked. `ctr images import` of a `docker save` archive stores uncompressed blobs and keeps
        # them, so each worker held every image twice (#1373). The pull is anonymous and uses neither the host's
        # registry mirrors, proxy nor `docker login` (#757), so a failure (a Docker Hub 429, a node with no
        # egress) falls back to the host path, which has the full pull retry policy.
        try:
            _run_on_nodes(nodes, lambda node: _pull_in_node(image, tools, node, env=env))
            return
        except DeploymentPreconditionError as exc:
            log.warn(f"{exc} Pulling {image} through the host's Docker instead.")

    if tools.docker.image_exists(image, env=env):
        log.step(f"Using existing local image {image}...")
    else:
        log.step(f"Pulling {image}...")
        tools.docker.pull(image, env=env, retry_policy=config.prefetch.pull_retry)
        host_images.record(config, image)

    archive_image = archive_reference(image)
    if archive_image != image:
        if not tools.docker.image_exists(archive_image, env=env):
            host_images.record(config, archive_image)
        tools.docker.tag(image, archive_image, env=env)

    archive = scratch_dir / archive_filename(image)
    log.step(f"Saving {archive_image}...")
    # Docker before 29.9 with the containerd image store skips a layer's blob on pull when a snapshot with the
    # same diff ID is already unpacked, and `docker save` then leaves the blob out of the archive. kube-state-metrics
    # shares its distroless base layers with cert-manager under other blob digests, so on the CI runner's Docker
    # 29.4.2 its archive lacked the amd64 content and `ctr import` failed "content digest ...: not found" (#21).
    # Another pull cannot repair the store, so the nodes pull such an image at its pin themselves.
    try:
        tools.docker.save(archive_image, output_path=archive, env=env)
    except CommandExecutionError as exc:
        archive.unlink(missing_ok=True)
        for node in nodes:
            _pull_on_node(image, tools, node, cause=exc, env=env)
        return

    if archive_image != image:
        repository, digest = split_digest_reference(image)
        # `docker save` of a reference with no tag names the image `:latest`, as `docker tag` did.
        tagged = archive_image if ":" in archive_image.rsplit("/", 1)[-1] else f"{archive_image}:latest"
        digest_names = [containerd_name(tagged), containerd_name(f"{repository}@{digest}")]

    def load(node: str) -> None:
        log.step(f"Loading {image} into kind node '{node}'...")
        try:
            tools.docker.exec_(
                node,
                ["ctr", "--namespace=k8s.io", "images", "import", "--snapshotter=overlayfs", "-"],
                privileged=True,
                interactive=True,
                stdin_path=archive,
                env=env,
            )
        except CommandExecutionError as exc:
            _pull_on_node(image, tools, node, cause=exc, env=env)
            return
        if archive_image != image:
            # The import names only the tag. `crictl pull` would add the digest name too, but it resolves
            # the digest against the registry first, which fails on a node with no egress (#757).
            tools.docker.exec_(node, ["ctr", "--namespace=k8s.io", "images", "tag", "--force", *digest_names], env=env)

    _run_on_nodes(nodes, load)
    # Every node holds its own copy now; keeping the archive until the run ends doubled the disk use.
    archive.unlink(missing_ok=True)


def _run_on_nodes(nodes: list[str], fn: Callable[[str], None]) -> None:
    errors: list[BaseException] = []

    def guarded(node: str) -> None:
        try:
            fn(node)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
            errors.append(exc)

    # Each node unpacks into its own containerd, so loading them together is ~1.7x faster than one by one.
    # Daemon threads joined by hand, not an executor: a signal deadline (`olf smoke`) interrupts `join` on
    # the main thread, where an executor's exit would first wait out the blocked `docker exec` calls.
    threads = [threading.Thread(target=guarded, args=(node,), daemon=True) for node in nodes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


def _pull_in_node(image: str, tools: Toolkit, node: str, *, env: Mapping[str, str]) -> None:
    log.step(f"Pulling {image} in kind node '{node}'...")
    result = tools.docker.exec_(node, ["crictl", "pull", image], env=env, check=False)
    if not result.ok:
        raise DeploymentPreconditionError(f"kind node '{node}' could not pull {image}: {result.stderr.strip()}.")


def _pull_on_node(
    image: str, tools: Toolkit, node: str, *, cause: CommandExecutionError, env: Mapping[str, str]
) -> None:
    """Have the node's containerd pull `image` itself, for when Docker could not hand it a complete archive."""
    log.warn(f"Docker could not provide a complete archive of {image}; kind node '{node}' pulls it instead.")
    result = tools.docker.exec_(node, ["crictl", "pull", image], env=env, check=False)
    if not result.ok:
        raise DeploymentPreconditionError(
            f"Could not load {image} into kind node '{node}'. Docker could not archive it ({cause}), and the "
            f"node could not pull it: {result.stderr.strip()}. Docker Engine 29.9 or later archives it; a node "
            "without registry access needs that."
        ) from cause
