"""`CloudBackend` for Azure AKS/ACR.

Port of the Azure-specific branches of `scripts/azure/{foundation,stack,
images}/*.sh`: subscription-login preflight, AKS Terraform variables,
`az aks get-credentials`, ACR login, and the rendered (local-k8s.yml) Floe
profile strategy - Azure hits the same profile branches local does.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from olf import log
from olf.deployment.charts import TERRAFORM_VARIABLE_KEY
from olf.deployment.cloud.backend import FoundationFacts, output_raw_or_empty
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.context import topology_variables
from olf.deployment.engine import Toolkit
from olf.deployment.env_settings import env as _env
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.floe_manifests import generate_local_manifests
from olf.deployment.portforward import ForwardTarget
from olf.tooling.azure import ACR_USERNAME

_DEFAULT_CLUSTER_NAME = "aks-openlakeforge-poc"


class AzureBackend:
    scope = "azure"

    def preflight(self, tools: Toolkit, *, env: Mapping[str, str]) -> None:
        try:
            tools.azure.account_show(env=env)
        except Exception as exc:
            raise DeploymentPreconditionError(
                f"Azure is not authenticated. Run 'olf auth login --provider azure': {exc}"
            ) from exc

    def foundation_state_resource_addr(self) -> str:
        return "azurerm_kubernetes_cluster.this"

    def _foundation_variables(self, config: CloudDeploymentConfig, environ: Mapping[str, str]) -> dict[str, str]:
        return {
            "cluster_name": _env(environ, "AZURE_CLUSTER_NAME", _DEFAULT_CLUSTER_NAME),
            "node_count": _env(environ, "AZURE_NODE_COUNT", "3"),
            "acr_name_prefix": _env(environ, "AZURE_ACR_NAME_PREFIX", "openlakeforgepoc"),
            "kubeconfig_path": str(config.paths.kubeconfig_path),
        }

    def foundation_apply_variables(self, config: CloudDeploymentConfig, environ: Mapping[str, str]) -> dict[str, str]:
        return self._foundation_variables(config, environ)

    def foundation_destroy_variables(self, config: CloudDeploymentConfig, environ: Mapping[str, str]) -> dict[str, str]:
        return self._foundation_variables(config, environ)

    def foundation_tfvars_file(
        self, environ: Mapping[str, str], *, repo_root: Path, foundation_terraform_dir: Path
    ) -> Path:
        raw = environ.get("AZURE_TFVARS_FILE") or ""
        if raw:
            candidate = Path(raw)
            resolved = candidate if candidate.is_absolute() else repo_root / candidate
        else:
            resolved = foundation_terraform_dir / "sandbox.tfvars"

        if not resolved.is_file():
            raise DeploymentPreconditionError(
                f"Azure foundation configuration not found: {resolved}. Restore the tfvars used to create "
                "this foundation, set AZURE_TFVARS_FILE to its path, or - for a first-time apply - copy "
                f"{foundation_terraform_dir}/sandbox.tfvars.example to {resolved} and configure your "
                "resource group."
            )
        return resolved

    def resolve_foundation_facts(
        self, tools: Toolkit, *, foundation_terraform_dir: Path, env: Mapping[str, str]
    ) -> FoundationFacts:
        """Resolve cluster identity strictly; resolve registry endpoints leniently.

        `resource_group_name`/`cluster_name` are required by every
        operation (they drive the kubeconfig/kube_context every command
        needs). `acr_login_server`/`acr_name` are only ever consumed as a
        fallback in `resolve_effective_images`, used solely by
        `platform_up`/`artifacts_deploy` - the removed `teardown.sh`/
        `platform-down`/status/forward paths never read them, so a
        foundation missing (or not yet recording) that output must not
        block recovery commands that don't need a registry at all.
        """
        resource_group = tools.terraform.output_raw(foundation_terraform_dir, "resource_group_name", env=env)
        cluster_name = tools.terraform.output_raw(foundation_terraform_dir, "cluster_name", env=env)
        acr_login_server = output_raw_or_empty(tools, foundation_terraform_dir, "acr_login_server", env=env)
        acr_name = output_raw_or_empty(tools, foundation_terraform_dir, "acr_name", env=env)
        if not acr_name:
            acr_name = acr_login_server.split(".", 1)[0]

        return FoundationFacts(
            cluster_name=cluster_name,
            kube_context=cluster_name,
            project_code_repository=f"{acr_login_server}/openlakeforge/project-code",
            superset_repository=f"{acr_login_server}/openlakeforge/superset",
            azure_resource_group=resource_group,
            azure_acr_name=acr_name,
        )

    def cluster_reachable(self, tools: Toolkit, facts: FoundationFacts, *, env: Mapping[str, str]) -> bool:
        return tools.azure.aks_show(
            facts.cluster_name, resource_group=_resource_group(facts), env=env, check=False
        ).ok

    def update_kubeconfig(
        self, tools: Toolkit, facts: FoundationFacts, *, kubeconfig_path: Path, env: Mapping[str, str]
    ) -> None:
        tools.azure.aks_get_credentials(
            facts.cluster_name,
            resource_group=_resource_group(facts),
            kubeconfig_path=kubeconfig_path,
            overwrite=True,
            env=env,
        )

    def registry_login(
        self,
        tools: Toolkit,
        facts: FoundationFacts,  # noqa: ARG002 - the ACR name is derived from `repository`, not the foundation facts
        *,
        repository: str,
        env: Mapping[str, str],
    ) -> None:
        """Authenticate to the ACR that hosts `repository`.

        `az acr login --name` wants the ACR *short name*, not a full login
        server or repository path - derive it from `repository`'s registry
        host (`<name>.azurecr.io/...` -> `<name>`) rather than always using
        `facts.azure_acr_name`. `repository` is the *effective* repository
        (after `AZURE_PROJECT_CODE_IMAGE_REPOSITORY`/
        `AZURE_SUPERSET_IMAGE_REPOSITORY` overrides), matching the AWS
        backend's equivalent fix: an override pointed at a different ACR
        must still authenticate against *that* registry, not the
        foundation's default - and must not depend on `facts.azure_acr_name`
        being resolvable at all when an override is supplied.
        """
        registry_host = repository.split("/", 1)[0]
        acr_name = registry_host.split(".", 1)[0] if registry_host else ""
        password = tools.azure.acr_login(acr_name, env=env)
        tools.docker.login(registry_host, username=ACR_USERNAME, password=password, env=env)

    def platform_apply_variables(self, config: CloudDeploymentConfig, facts: FoundationFacts) -> dict[str, str]:
        from olf.deployment.cloud.images import resolve_effective_images

        images = resolve_effective_images(config.images, facts)
        return topology_variables(config.context) | {
            "kube_context": facts.kube_context,
            "kubeconfig_path": str(config.paths.kubeconfig_path),
            # See local/platform.py::platform_apply_variables: the Terraform
            # helm provider's own repository cache/config default lives
            # beneath the Terraform root, which for an installed
            # distribution is the read-only payload.
            "helm_repository_cache_path": str(config.paths.helm_repository_cache),
            "helm_repository_config_path": str(config.paths.helm_repository_config),
            "foundation_state_path": str(config.paths.foundation_state_path),
            "project_code_image_repository": images.project_code_repository,
            "project_code_image_tag": images.project_code_tag,
            "project_code_image_pull_policy": images.project_code_pull_policy,
            "project_code_image_revision": images.project_code_revision,
            "superset_image_repository": images.superset_repository,
            "superset_image_tag": images.superset_tag,
            "superset_image_pull_policy": images.superset_pull_policy,
        } | {TERRAFORM_VARIABLE_KEY[setting.name]: str(setting.package_path) for setting in config.charts.values()}

    def platform_destroy_variables(self, config: CloudDeploymentConfig, facts: FoundationFacts) -> dict[str, str]:
        return topology_variables(config.context) | {
            "kube_context": facts.kube_context,
            "kubeconfig_path": str(config.paths.kubeconfig_path),
            "helm_repository_cache_path": str(config.paths.helm_repository_cache),
            "helm_repository_config_path": str(config.paths.helm_repository_config),
            "foundation_state_path": str(config.paths.foundation_state_path),
        }

    def cleanup_polaris_jobs_before_apply(self) -> bool:
        return True

    def forward_base_targets(self) -> tuple[ForwardTarget, ...]:
        return (
            ForwardTarget("seaweedfs-s3", "svc/seaweedfs-s3", 9000, 8333),
            ForwardTarget("polaris", "svc/polaris", 8181, 8181),
            ForwardTarget("trino", "svc/trino", 8080, 8080),
        )

    def artifact_transport(self) -> str:
        return "port-forward"

    def generate_floe_manifests(
        self,
        config: CloudDeploymentConfig,
        tools: Toolkit,
        *,
        repo_root: Path,
        distribution_root: Path,
        namespace: str,
        governance_enabled: bool,
        environ: Mapping[str, str],
        env: Mapping[str, str],
    ) -> list[Path]:
        log.step("Using the rendered (local-k8s.yml) Floe profile strategy.")
        return generate_local_manifests(
            config.floe,
            tools,
            repo_root=repo_root,
            distribution_root=distribution_root,
            namespace=namespace,
            governance_enabled=governance_enabled,
            environ=environ,
            env=env,
        )


def _resource_group(facts: FoundationFacts) -> str:
    # `FoundationFacts` is shared by every cloud backend, so the field is
    # optional; Azure facts resolved by `resolve_foundation_facts` always set it.
    if not facts.azure_resource_group:
        raise DeploymentPreconditionError(
            "Azure foundation facts carry no resource group; re-run the foundation apply"
        )
    return facts.azure_resource_group
