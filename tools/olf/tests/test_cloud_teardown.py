from __future__ import annotations

from pathlib import Path

from _cloud_support import FakeCloudBackend
from _tooling_support import RecordingRunner

from olf.deployment.cloud import teardown
from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.context import DeploymentContext
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver
from olf.tooling.toolkit import Toolkit

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm", "aws", "az")
_FACTS = FoundationFacts(
    cluster_name="eks-openlakeforge-poc",
    kube_context="eks-openlakeforge-poc",
    project_code_repository="123.dkr.ecr.eu-west-1.amazonaws.com/project-code",
    aws_region="eu-west-1",
)


def _config(tmp_path: Path) -> CloudDeploymentConfig:
    context = DeploymentContext.aws(repo_root=tmp_path)
    return CloudDeploymentConfig.from_environment({}, context=context)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.tooling.docker import Docker
    from olf.tooling.helm import Helm
    from olf.tooling.kind import Kind
    from olf.tooling.kubectl import Kubectl
    from olf.tooling.terraform import Terraform

    resolver = PathExecutableResolver(overrides={tool: Path(tool) for tool in _TOOLS})
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
    )


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


def test_platform_down_deletes_superset_job_destroys_and_deletes_namespace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws")
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)

    teardown.platform_down(config, tools, backend, _FACTS, env={})

    assert "platform_destroy_variables" in backend.calls
    job_delete = next(c for c in runner.calls if "superset-init-db" in c.argv)
    assert "--wait=true" in job_delete.argv
    destroy_call = next(c for c in runner.calls if c.argv[0] == "terraform" and c.argv[2:3] == ["destroy"])
    assert "-var=namespace=olf-dev" in destroy_call.argv
    namespace_delete = next(
        c for c in runner.calls if c.argv[0] == "kubectl" and "delete" in c.argv and "namespace" in c.argv
    )
    assert "olf-dev" in namespace_delete.argv


def test_platform_down_uses_facts_kube_context_not_config_kube_context(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws")
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)

    teardown.platform_down(config, tools, backend, _FACTS, env={})

    assert all(
        c.argv[1:3] != ["--context", ""] for c in runner.calls if c.argv and c.argv[0] == "kubectl"
    )
    context_calls = [c for c in runner.calls if c.argv[0] == "kubectl" and "--context" in c.argv]
    assert context_calls
    for call in context_calls:
        idx = call.argv.index("--context")
        assert call.argv[idx + 1] == _FACTS.kube_context
