"""The `CloudBackend` seam: everything genuinely different between AWS and Azure.

`cloud/foundation.py`, `cloud/platform.py`, `cloud/images.py`,
`cloud/artifacts.py`, `cloud/teardown.py`, and `cloud/forward.py` implement
the shared lifecycle - identical Terraform init/apply/import/destroy,
namespace adoption, chart caching, and artifact-deployment ordering between
AWS and Azure - and call out to a `CloudBackend` for the handful of things
that are not shared: foundation Terraform variables, kubeconfig population,
registry login, and the default project-code image repository. `cloud/aws.py`/`cloud/azure.py` provide the two
concrete implementations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.engine import Toolkit
from olf.deployment.errors import CommandExecutionError
from olf.deployment.portforward import ForwardTarget


def output_raw_or_empty(tools: Toolkit, terraform_dir: Path, name: str, *, env: Mapping[str, str]) -> str:
    """`terraform output -raw`, treating a missing/unset output as absent rather than fatal.

    Used only for registry-related foundation outputs (ECR repository URLs,
    Azure's `acr_login_server`) - facts that `resolve_effective_images`
    treats as an optional fallback behind explicit repository overrides,
    and that recovery commands (teardown, status, forward, platform-down)
    never need at all. Cluster-identity outputs (`cluster_name`,
    `aws_region`, `resource_group_name`) stay required via the plain
    `output_raw` call - every command needs them to reach the cluster.
    """
    try:
        return tools.terraform.output_raw(terraform_dir, name, env=env)
    except CommandExecutionError:
        return ""


@dataclass(frozen=True)
class FoundationFacts:
    """Cluster identity and registry endpoints, known only after `foundation_up`.

    `DeploymentContext.kube_context` is empty for a freshly-built cloud
    context (unlike local's static `kind-<cluster>`); `CloudProvider`
    resolves this once by reading Terraform foundation outputs and uses it
    to derive the effective `kube_context` and default image repositories.
    """

    cluster_name: str
    kube_context: str
    project_code_repository: str
    aws_region: str | None = None
    azure_resource_group: str | None = None
    azure_acr_name: str | None = None


class CloudBackend(Protocol):
    scope: str

    def preflight(self, tools: Toolkit, *, env: Mapping[str, str]) -> None:
        """Verify the CLI is authenticated before any Terraform runs."""

    def foundation_state_resource_addr(self) -> str: ...

    def foundation_apply_variables(
        self, config: CloudDeploymentConfig, environ: Mapping[str, str]
    ) -> dict[str, str]: ...

    def foundation_destroy_variables(
        self, config: CloudDeploymentConfig, environ: Mapping[str, str]
    ) -> dict[str, str]: ...

    def foundation_tfvars_file(
        self, environ: Mapping[str, str], *, repo_root: Path, foundation_terraform_dir: Path
    ) -> Path | None:
        """Resolve the foundation tfvars file, or raise if the provider requires one."""

    def resolve_foundation_facts(
        self, tools: Toolkit, *, foundation_terraform_dir: Path, env: Mapping[str, str]
    ) -> FoundationFacts: ...

    def cluster_reachable(self, tools: Toolkit, facts: FoundationFacts, *, env: Mapping[str, str]) -> bool:
        """Cheap existence probe (`eks describe-cluster` / `aks show`) used by foundation-down."""

    def update_kubeconfig(
        self, tools: Toolkit, facts: FoundationFacts, *, kubeconfig_path: Path, env: Mapping[str, str]
    ) -> None: ...

    def registry_login(
        self, tools: Toolkit, facts: FoundationFacts, *, repository: str, env: Mapping[str, str]
    ) -> None:
        """Authenticate against the registry that hosts `repository`.

        `repository` is the *effective* image repository being pushed to
        (after `PROJECT_CODE_IMAGE_REPOSITORY`
        override is applied), not necessarily the foundation's default -
        an override pointed at a different ECR registry must still get
        credentials for that registry, matching the removed shell scripts
        (`registry="${PROJECT_CODE_IMAGE_REPOSITORY%%/*}"`).
        """

    def platform_apply_variables(self, config: CloudDeploymentConfig, facts: FoundationFacts) -> dict[str, str]: ...

    def platform_destroy_variables(self, config: CloudDeploymentConfig, facts: FoundationFacts) -> dict[str, str]:
        """A strict subset of `platform_apply_variables`: namespace/context/kubeconfig/foundation-state
        (plus `aws_region` for AWS) - the shell `teardown.sh` scripts never pass image or chart variables
        to `terraform destroy`.
        """

    def cleanup_polaris_jobs_before_apply(self) -> bool:
        """Whether to delete failed `polaris-*-bootstrap-*` jobs before each apply retry.

        Azure's `stack/platform-up.sh` does this; AWS's does not. Preserved
        as-is rather than unified - see the PR description.
        """

    def forward_base_targets(self) -> tuple[ForwardTarget, ...]:
        """Infra port-forward targets present unconditionally (trino, plus Azure's seaweedfs/polaris)."""

    def artifact_transport(self) -> str:
        """`--via` mode for artifact revision activation/upload: 'direct' or 'port-forward'."""
