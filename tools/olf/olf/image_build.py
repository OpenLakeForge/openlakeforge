"""Local runtime image builds, which need Docker but no cluster or cloud credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping

from olf import config, log
from olf.deployment.context import DeploymentContext, Profile
from olf.deployment.local.config import LocalDeploymentConfig
from olf.deployment.project_code import project_code_build_context
from olf.tooling.toolkit import Toolkit


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


def local_image_config(
    cluster_name: str, *, project_root: str = "", environ: Mapping[str, str] | None = None
) -> tuple[LocalDeploymentConfig, Toolkit]:
    from olf.distribution import runtime_layout

    layout_env = dict(os.environ if environ is None else environ)
    if project_root:
        layout_env["OPENLAKEFORGE_PROJECT_ROOT"] = project_root
    layout = runtime_layout(layout_env)
    context = DeploymentContext.for_provider(
        "local",
        repo_root=layout.project.root,
        distribution_root=layout.project.distribution_root,
        state_root=None if layout.is_source else layout.state_root,
        work_root=None if layout.is_source else layout.work_root,
        cache_root=None if layout.is_source else layout.cache_root,
        namespace=config.namespace(),
        cluster_name=cluster_name,
        profile=Profile.FULL,
    )
    resolved_environ = os.environ if environ is None else environ
    return LocalDeploymentConfig.from_environment(resolved_environ, context=context), Toolkit.default()
