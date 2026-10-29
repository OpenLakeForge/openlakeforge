"""Dynamic cloud (AWS/Azure) artifact deployment.

Port of `scripts/{aws,azure}/stack/deploy-artifacts.sh`. Preserves the exact
ordering: reconcile catalog namespaces -> build/push the project-code
image -> deploy optional Superset/OpenMetadata artifacts -> point Dagster
at the image. The optional
layers deliberately run before the Dagster image switch, so a failed
Superset/OpenMetadata deploy leaves the running deployment on its previous
image rather than one whose optional artifacts never landed.

Unlike local (which only builds the project-code image when the tag is
still the `local` placeholder), cloud builds and pushes a freshly tagged
image unless the release-published example image is deployed as it is.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from olf import k8s, log
from olf.deployment import contract_env
from olf.deployment.artifact_steps import deploy_optional_layer_artifacts, sync_catalog_namespaces
from olf.deployment.cloud.backend import CloudBackend, FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.cloud.images import build_and_push_project_code_image, resolve_effective_images
from olf.deployment.engine import Toolkit


@contextmanager
def _applied_authentication_environment(provider: str, environ: Mapping[str, str]):
    """Expose selected auth state to in-process SDK clients for one deploy."""
    from olf.auth import credential_selection_environment

    selected = credential_selection_environment(provider, environ)
    previous = {name: os.environ.get(name) for name in selected}
    os.environ.update(selected)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _resolve_contract_terraform_dir(config: CloudDeploymentConfig, environ: Mapping[str, str] | None = None) -> Path:
    """Honor `OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR`, matching the removed
    `scripts/{aws,azure}/stack/deploy-artifacts.sh` (`CONTRACT_TERRAFORM_DIR=
    "${OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR:-...}"`) and `olf.e2e._runner
    ._contract_dir`, both of which resolve this override directly from the
    process environment rather than a provider's curated command env.
    """
    contract_dir = (environ or os.environ).get(
        "OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", config.paths.platform_terraform_dir
    )
    return Path(contract_dir).resolve()


def artifacts_deploy(
    config: CloudDeploymentConfig,
    tools: Toolkit,
    backend: CloudBackend,
    facts: FoundationFacts,
    *,
    env: Mapping[str, str],
) -> None:
    with _applied_authentication_environment(backend.scope, env), contract_env.applied_contract_environment(
        contract_terraform_dir=_resolve_contract_terraform_dir(config, env),
        repo_root=config.paths.repo_root,
        namespace=config.namespace,
        kube_context=facts.kube_context,
        kubeconfig_path=config.paths.kubeconfig_path,
        port_forward_log_prefix=config.paths.port_forward_log_prefix,
        environ=env,
        topology=config.context.topology,
        stage=config.context.stage,
    ):
        log.step(f"Reconciling catalog namespaces from the domain descriptors for {backend.scope}...")
        sync_catalog_namespaces()

        if config.images.project_code_published:
            log.step("Deploying the release-published example project-code image; nothing to build or push.")
        else:
            build_and_push_project_code_image(config, tools, backend, facts, env=env)

        # Deliberately before the Dagster image switch: the removed
        # deploy-artifacts.sh ran deploy-optional-layers first, so a failed
        # Superset/OpenMetadata deploy leaves the running Dagster deployment
        # unchanged instead of already pointed at the new image.
        os.environ.setdefault("OPENMETADATA_ALLOW_MISSING_ASSETS", "true")
        deploy_optional_layer_artifacts(os.environ)

        project_code_image = resolve_effective_images(config.images, facts).project_code_image
        log.step(f"Pointing Dagster at project-code image {project_code_image}...")
        k8s.set_project_code_image(project_code_image, config.namespace)

        log.step(f"Dynamic OpenLakeForge {backend.scope} artifacts are deployed.")
