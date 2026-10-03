"""`CloudBackend` for AWS EKS/ECR.

Port of the AWS-specific branches of `scripts/aws/{foundation,stack,images}/
*.sh`: caller-identity preflight, EKS Terraform variables, `eks update-
kubeconfig`, ECR login, and the AWS Glue Floe profile strategy.
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
from olf.deployment.floe_manifests import generate_aws_manifests
from olf.deployment.portforward import ForwardTarget

# Must match the AWS default in `olf.commands.e2e._default_kube_context` and
# `olf.e2e._runner` - a direct `olf deploy --provider aws` (no
# AWS_CLUSTER_NAME set) creates this cluster; `olf e2e run --env aws` (also
# no AWS_CLUSTER_NAME) must target the same one, or e2e looks for a
# kubeconfig context the deployment never created. The removed
# foundation/up.sh's own bare default was "eks-openlakeforge-poc" (no
# "limited-" prefix), but that default was never actually reachable - the
# Makefile (#222) always passed AWS_CLUSTER_NAME explicitly - so this is a
# rename to the value that was already load-bearing everywhere else.
_DEFAULT_CLUSTER_NAME = "limited-eks-openlakeforge-poc"


class AwsBackend:
    scope = "aws"

    def preflight(self, tools: Toolkit, *, env: Mapping[str, str]) -> None:
        try:
            tools.aws.sts_get_caller_identity(env=env)
        except Exception as exc:
            raise DeploymentPreconditionError(
                f"AWS is not authenticated. Run 'olf auth login --provider aws': {exc}"
            ) from exc

    def foundation_state_resource_addr(self) -> str:
        return "aws_eks_cluster.this"

    def _foundation_variables(self, config: CloudDeploymentConfig, environ: Mapping[str, str]) -> dict[str, str]:
        cluster_name = _env(environ, "AWS_CLUSTER_NAME", _DEFAULT_CLUSTER_NAME)
        instance_types_raw = _env(environ, "AWS_NODE_INSTANCE_TYPES", "m7i.large")
        instance_types = instance_types_raw if instance_types_raw.startswith("[") else f'["{instance_types_raw}"]'
        region = config.context.topology.region or _env(environ, "AWS_REGION", "eu-west-1")
        return {
            "cluster_name": cluster_name,
            "kubeconfig_path": str(config.paths.kubeconfig_path),
            "aws_region": region,
            "node_desired_size": _env(environ, "AWS_NODE_DESIRED_SIZE", "3"),
            "node_min_size": _env(environ, "AWS_NODE_MIN_SIZE", "1"),
            "node_max_size": _env(environ, "AWS_NODE_MAX_SIZE", "4"),
            "node_instance_types": instance_types,
        }

    def foundation_apply_variables(
        self, config: CloudDeploymentConfig, environ: Mapping[str, str]
    ) -> dict[str, str]:
        return self._foundation_variables(config, environ)

    def foundation_destroy_variables(
        self, config: CloudDeploymentConfig, environ: Mapping[str, str]
    ) -> dict[str, str]:
        return self._foundation_variables(config, environ)

    def foundation_tfvars_file(
        self, environ: Mapping[str, str], *, repo_root: Path, foundation_terraform_dir: Path
    ) -> Path | None:
        raw = environ.get("AWS_TFVARS_FILE") or ""
        if raw:
            candidate = Path(raw)
            return candidate if candidate.is_absolute() else repo_root / candidate
        default_tfvars = foundation_terraform_dir / "sandbox.tfvars"
        return default_tfvars if default_tfvars.is_file() else None

    def resolve_foundation_facts(
        self, tools: Toolkit, *, foundation_terraform_dir: Path, env: Mapping[str, str]
    ) -> FoundationFacts:
        """Resolve cluster identity strictly; resolve registry endpoints leniently.

        `cluster_name`/`aws_region` are required by every operation (they
        drive the kubeconfig/kube_context every command needs). The ECR
        repository outputs are only ever consumed as a fallback in
        `resolve_effective_images`, used solely by `platform_up`/
        `artifacts_deploy` - the removed `teardown.sh`/`platform-down`/
        status/forward paths never read them. A foundation apply that left
        the cluster in state but failed before those outputs were recorded
        (or an older/custom foundation that omits them, with explicit image
        overrides supplied instead) must not block recovery commands that
        don't need a registry at all.
        """
        cluster_name = tools.terraform.output_raw(foundation_terraform_dir, "cluster_name", env=env)
        aws_region = tools.terraform.output_raw(foundation_terraform_dir, "aws_region", env=env)
        project_code_repository = output_raw_or_empty(
            tools, foundation_terraform_dir, "project_code_ecr_repository_url", env=env
        )
        superset_repository = output_raw_or_empty(
            tools, foundation_terraform_dir, "superset_ecr_repository_url", env=env
        )
        return FoundationFacts(
            cluster_name=cluster_name,
            kube_context=cluster_name,
            project_code_repository=project_code_repository,
            superset_repository=superset_repository,
            aws_region=aws_region,
        )

    def cluster_reachable(self, tools: Toolkit, facts: FoundationFacts, *, env: Mapping[str, str]) -> bool:
        return tools.aws.eks_describe_cluster(facts.cluster_name, region=_region(facts), env=env, check=False).ok

    def update_kubeconfig(
        self, tools: Toolkit, facts: FoundationFacts, *, kubeconfig_path: Path, env: Mapping[str, str]
    ) -> None:
        tools.aws.eks_update_kubeconfig(
            facts.cluster_name,
            region=_region(facts),
            kubeconfig_path=kubeconfig_path,
            alias=facts.cluster_name,
            env=env,
        )

    def registry_login(
        self, tools: Toolkit, facts: FoundationFacts, *, repository: str, env: Mapping[str, str]
    ) -> None:
        registry = repository.split("/", 1)[0]
        password = tools.aws.ecr_get_login_password(region=_region(facts), env=env)
        tools.docker.login(registry, username="AWS", password=password, env=env)

    def platform_apply_variables(self, config: CloudDeploymentConfig, facts: FoundationFacts) -> dict[str, str]:
        from olf.deployment.cloud.images import resolve_effective_images

        images = resolve_effective_images(config.images, facts)
        return topology_variables(config.context) | {
            "aws_region": facts.aws_region or "",
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
            "aws_region": facts.aws_region or "",
            "kube_context": facts.kube_context,
            "kubeconfig_path": str(config.paths.kubeconfig_path),
            "helm_repository_cache_path": str(config.paths.helm_repository_cache),
            "helm_repository_config_path": str(config.paths.helm_repository_config),
            "foundation_state_path": str(config.paths.foundation_state_path),
        }

    def cleanup_polaris_jobs_before_apply(self) -> bool:
        return False

    def forward_base_targets(self) -> tuple[ForwardTarget, ...]:
        return (ForwardTarget("trino", "svc/trino", 8080, 8080),)

    def artifact_transport(self) -> str:
        return "direct"

    def generate_floe_manifests(
        self,
        config: CloudDeploymentConfig,
        tools: Toolkit,
        *,
        repo_root: Path,
        distribution_root: Path,  # noqa: ARG002 - AWS's Glue profile strategy never reads the checked-in profile
        namespace: str,  # noqa: ARG002 - AWS's Glue profile strategy is namespace-independent
        governance_enabled: bool,  # noqa: ARG002 - AWS's Glue profile strategy is always used
        environ: Mapping[str, str],
        env: Mapping[str, str],
    ) -> list[Path]:
        log.step("Using the AWS Glue Floe profile strategy (one rendered profile per product).")
        return generate_aws_manifests(config.floe, tools, repo_root=repo_root, environ=environ, env=env)


def _region(facts: FoundationFacts) -> str:
    # `FoundationFacts` is shared by every cloud backend, so the field is
    # optional; AWS facts resolved by `resolve_foundation_facts` always set it.
    if not facts.aws_region:
        raise DeploymentPreconditionError("AWS foundation facts carry no aws_region; re-run the foundation apply")
    return facts.aws_region
