"""Local Superset and project-code image build/load.

Port of `scripts/local/images/{build,load}-{superset,project-code}.sh`. The
`kind`-less Docker-node fallback in the removed shell `load-*.sh` scripts is
not ported here: the local provider already hard-requires `kind` (kubeconfig
export, `kind get nodes` during prefetch).
"""

from __future__ import annotations

import json
import platform
from collections.abc import Mapping

from olf import log
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.local.config import LocalDeploymentConfig
from olf.deployment.project_code import project_code_build_context


def build_superset_image(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> str:
    images = config.images
    log.step(f"Pulling Superset base image: {images.superset_base_image}")
    tools.docker.pull(images.superset_base_image, env=env, retry_policy=images.pull_retry)

    log.step(f"Building Superset image: {images.superset_image}")
    tools.docker.build(
        config.paths.distribution_root / "images/superset",
        tag=images.superset_image,
        file=config.paths.distribution_root / "images/superset/Dockerfile",
        build_args={"SUPERSET_BASE_IMAGE": images.superset_base_image},
        env=env,
        retry_policy=images.build_retry,
    )
    return images.superset_image


def build_project_code_image(
    config: LocalDeploymentConfig,
    tools: Toolkit,
    *,
    env: Mapping[str, str],
) -> str:
    images = config.images
    log.step(f"Pulling project-code Python base image: {images.project_code_python_base_image}")
    tools.docker.pull(images.project_code_python_base_image, env=env, retry_policy=images.pull_retry)

    log.step(f"Building project-code image: {images.project_code_build_image}")
    with project_code_build_context(config.paths) as build_context:
        tools.docker.build(
            build_context,
            tag=images.project_code_build_image,
            file=build_context / "images/project-code/Dockerfile",
            build_args={"PYTHON_BASE_IMAGE": images.project_code_python_base_image},
            env=env,
            retry_policy=images.build_retry,
        )
    return images.project_code_build_image


def load_image_into_kind(image: str, config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    if not tools.docker.image_exists(image, env=env):
        raise DeploymentPreconditionError(f"local image '{image}' does not exist. Build it first.")
    if config.cluster.name not in tools.kind.get_clusters(env=env):
        raise DeploymentPreconditionError(
            f"kind cluster '{config.cluster.name}' does not exist. Run the foundation phase first."
        )
    log.step(f"Loading {image} into kind cluster '{config.cluster.name}'")
    tools.kind.load_docker_image(image, cluster_name=config.cluster.name, env=env)
    log.step(f"Loaded {image}")


def _containerd_name(reference: str) -> str:
    """The fully qualified name containerd and the CRI record for a Docker reference."""
    head, _, rest = reference.partition("/")
    if not rest:
        return f"docker.io/library/{reference}"
    if head == "docker.io" and "/" not in rest:
        return f"docker.io/library/{rest}"
    if "." in head or ":" in head or head == "localhost":
        return reference
    return f"docker.io/{reference}"


def load_digest_image_into_kind(
    image: str, config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]
) -> None:
    """Load a digest-pinned image so kubelet finds it under that digest reference.

    `docker save` of a `repo@sha256:...` reference writes no name, and the
    manifest it writes is not the registry's, so after a plain `kind load` the
    node holds the layers but nothing called `repo@sha256:...`. Kubelet then
    pulls from the registry with no credentials: a public image hides this,
    a private one fails `unauthorized` (#280). Loading under a tag and naming
    the result after the digest in each node's containerd closes that gap
    without handing registry credentials to the cluster.
    """
    repository, digest = image.split("@", 1)
    prefix, _, name = repository.rpartition("/")
    repository = f"{prefix}/{name.split(':', 1)[0]}" if prefix else name.split(":", 1)[0]
    tagged = f"{repository}:{digest.replace(':', '-')}"
    tools.docker.tag(image, tagged, env=env)
    load_image_into_kind(tagged, config, tools, env=env)
    source, target = _containerd_name(tagged), _containerd_name(f"{repository}@{digest}")
    for node in tools.kind.get_nodes(config.cluster.name, env=env):
        tools.docker.exec_(
            node, ["ctr", "--namespace=k8s.io", "images", "tag", "--force", source, target], env=env
        )


def prepare_superset_image(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> str | None:
    """Build `images/superset` and load it into kind where the release-pinned image cannot be used.

    That is a maintainer opt-in (`BUILD_SUPERSET_IMAGE`) or a non-amd64 Docker engine. Returns the content tag to
    deploy instead of the pin, or None. `:local` never changes, so a rebuilt image would leave the pod spec
    identical and Helm would not roll the pods; re-tagging by image ID makes a changed image a changed spec.
    """
    if not config.features.analytics_enabled:
        return None
    if not config.images.builds_superset(tools.docker.server_arch(env=env) or platform.machine()):
        return None
    log.step("Building local Superset platform image...")
    image = build_superset_image(config, tools, env=env)
    image_id = json.loads(tools.docker.image_inspect(image, env=env, check=True).stdout)[0]["Id"]
    tagged = f"{config.images.superset_repository}:local-{image_id.removeprefix('sha256:')[:12]}"
    tools.docker.tag(image, tagged, env=env)
    log.step("Ensuring local Superset platform image is available to kind...")
    load_image_into_kind(tagged, config, tools, env=env)
    return tagged.rsplit(":", 1)[1]


def prepare_project_code_image(
    config: LocalDeploymentConfig,
    tools: Toolkit,
    *,
    env: Mapping[str, str],
) -> None:
    if config.images.project_code_published:
        # An artifacts-only run may follow a deploy of a different image, so nothing else has put this one on the nodes.
        log.step("Ensuring the published project-code image is available to kind...")
        from olf.deployment.local.prefetch import load_image_into_nodes

        load_image_into_nodes(config, tools, config.images.project_code_image, env=env)
        return
    if config.images.project_code_tag != "local":
        return
    log.step("Building local project-code image...")
    image = build_project_code_image(config, tools, env=env)
    log.step("Ensuring local project-code image is available to kind...")
    load_image_into_kind(image, config, tools, env=env)
