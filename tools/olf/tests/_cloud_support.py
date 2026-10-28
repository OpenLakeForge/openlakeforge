"""Test-only `CloudBackend` fake shared by `cloud/*` lifecycle tests.

Deliberately not `conftest.py` for the same reason `_tooling_support.py`
isn't: this only serves the cloud deployment tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.engine import Toolkit
from olf.deployment.portforward import ForwardTarget


@dataclass
class FakeCloudBackend:
    scope: str = "fake"
    calls: list[str] = field(default_factory=list)
    tfvars_file: Path | None = None
    cluster_reachable_result: bool = True
    cleanup_polaris: bool = False
    transport: str = "port-forward"
    facts: FoundationFacts | None = None
    manifest_paths: list[Path] = field(default_factory=list)

    def preflight(self, tools: Toolkit, *, env: Mapping[str, str]) -> None:  # noqa: ARG002
        self.calls.append("preflight")

    def foundation_state_resource_addr(self) -> str:
        return "fake_cluster.this"

    def foundation_apply_variables(
        self, config: CloudDeploymentConfig, environ: Mapping[str, str]  # noqa: ARG002
    ) -> dict[str, str]:
        self.calls.append("foundation_apply_variables")
        return {"cluster_name": "fake-cluster"}

    def foundation_destroy_variables(
        self, config: CloudDeploymentConfig, environ: Mapping[str, str]  # noqa: ARG002
    ) -> dict[str, str]:
        self.calls.append("foundation_destroy_variables")
        return {"cluster_name": "fake-cluster"}

    def foundation_tfvars_file(
        self,
        environ: Mapping[str, str],  # noqa: ARG002
        *,
        repo_root: Path,  # noqa: ARG002
        foundation_terraform_dir: Path,  # noqa: ARG002
    ) -> Path | None:
        self.calls.append("foundation_tfvars_file")
        return self.tfvars_file

    def resolve_foundation_facts(
        self,
        tools: Toolkit,  # noqa: ARG002
        *,
        foundation_terraform_dir: Path,  # noqa: ARG002
        env: Mapping[str, str],  # noqa: ARG002
    ) -> FoundationFacts:
        self.calls.append("resolve_foundation_facts")
        return self.facts or FoundationFacts(
            cluster_name="fake-cluster",
            kube_context="fake-cluster",
            project_code_repository="registry/project-code",
            superset_repository="registry/superset",
        )

    def cluster_reachable(
        self, tools: Toolkit, facts: FoundationFacts, *, env: Mapping[str, str]  # noqa: ARG002
    ) -> bool:
        self.calls.append("cluster_reachable")
        return self.cluster_reachable_result

    def update_kubeconfig(
        self,
        tools: Toolkit,  # noqa: ARG002
        facts: FoundationFacts,  # noqa: ARG002
        *,
        kubeconfig_path: Path,  # noqa: ARG002
        env: Mapping[str, str],  # noqa: ARG002
    ) -> None:
        self.calls.append("update_kubeconfig")

    def registry_login(
        self,
        tools: Toolkit,  # noqa: ARG002
        facts: FoundationFacts,  # noqa: ARG002
        *,
        repository: str,  # noqa: ARG002
        env: Mapping[str, str],  # noqa: ARG002
    ) -> None:
        self.calls.append("registry_login")

    def platform_apply_variables(self, config: CloudDeploymentConfig, facts: FoundationFacts) -> dict[str, str]:  # noqa: ARG002
        self.calls.append("platform_apply_variables")
        return {"namespace": config.namespace}

    def platform_destroy_variables(self, config: CloudDeploymentConfig, facts: FoundationFacts) -> dict[str, str]:  # noqa: ARG002
        self.calls.append("platform_destroy_variables")
        return {"namespace": config.namespace}

    def cleanup_polaris_jobs_before_apply(self) -> bool:
        return self.cleanup_polaris

    def forward_base_targets(self) -> tuple[ForwardTarget, ...]:
        return (ForwardTarget("trino", "svc/trino", 8080, 8080),)

    def artifact_transport(self) -> str:
        return self.transport
