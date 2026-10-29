"""Cloud (AWS/Azure) project-code image build/push.

Port of `scripts/{aws,azure}/images/build-push-project-code.sh`. Unlike the local provider (which loads images into
kind), cloud images are pushed to the provider's registry (ECR/ACR) after
`CloudBackend.registry_login`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from olf import log
from olf.deployment.cloud.backend import CloudBackend, FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig, CloudImageSettings
from olf.deployment.engine import Toolkit
from olf.deployment.project_code import project_code_build_context


def resolve_effective_images(images: CloudImageSettings, facts: FoundationFacts) -> CloudImageSettings:
    """Fill empty repository fields from the foundation's registry.

    Port of the shell scripts' `${PROJECT_CODE_IMAGE_REPOSITORY:-$(terraform
    output ...)}` fallback: an explicit env override always wins; otherwise
    the ECR/ACR repository derived from the foundation's Terraform outputs
    applies.
    """
    return replace(
        images,
        project_code_repository=images.project_code_repository or facts.project_code_repository,
    )


def build_and_push_project_code_image(
    config: CloudDeploymentConfig,
    tools: Toolkit,
    backend: CloudBackend,
    facts: FoundationFacts,
    *,
    env: Mapping[str, str],
) -> str:
    images = resolve_effective_images(config.images, facts)
    log.step(f"Logging in to the {backend.scope} registry...")
    backend.registry_login(tools, facts, repository=images.project_code_repository, env=env)

    log.step(f"Pulling project-code Python base image: {images.project_code_python_base_image}")
    tools.docker.pull(
        images.project_code_python_base_image,
        platform=images.image_platform,
        env=env,
        retry_policy=images.pull_retry,
    )

    log.step(f"Building project-code image: {images.project_code_image}")
    with project_code_build_context(config.paths) as build_context:
        tools.docker.build(
            build_context,
            tag=images.project_code_image,
            file=build_context / "images/project-code/Dockerfile",
            build_args={"PYTHON_BASE_IMAGE": images.project_code_python_base_image},
            extra_args=("--platform", images.image_platform),
            env=env,
            retry_policy=images.build_retry,
        )

    log.step(f"Pushing project-code image: {images.project_code_image}")
    tools.docker.push(images.project_code_image, env=env, retry_policy=images.push_retry)
    return images.project_code_image
