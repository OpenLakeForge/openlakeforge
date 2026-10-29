from __future__ import annotations

from pathlib import Path

import pytest
from _cloud_support import FakeCloudBackend
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.cloud import platform
from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.context import DeploymentContext, Profile, Provider
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentPreconditionError
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm", "aws", "az")
_FACTS = FoundationFacts(
    cluster_name="eks-openlakeforge-poc",
    kube_context="eks-openlakeforge-poc",
    project_code_repository="123.dkr.ecr.eu-west-1.amazonaws.com/project-code",
    aws_region="eu-west-1",
)


def _config(
    tmp_path: Path, *, enable_analytics: str = "true", topology=None, provider: Provider = Provider.AWS  # noqa: ANN001
) -> CloudDeploymentConfig:
    profile = Profile.FULL if enable_analytics == "true" else Profile.SLIM
    context_factory = DeploymentContext.aws if provider is Provider.AWS else DeploymentContext.azure
    context = context_factory(repo_root=tmp_path, profile=profile, topology=topology)
    return CloudDeploymentConfig.from_environment({}, context=context)


def _topology(*, dev_analytics: bool, prod_analytics: bool):  # noqa: ANN202
    from olf.profile import (
        DeploymentProfile,
        Preset,
        ProviderSpec,
        StageCapabilities,
        StageName,
        StageSpec,
        resolve_topology,
    )

    return resolve_topology(
        DeploymentProfile(
            name="acme-data",
            provider=ProviderSpec(type=Provider.AWS),
            preset=Preset.SLIM,
            stages=(
                StageSpec(
                    name=StageName.DEV,
                    capabilities=StageCapabilities(analytics=dev_analytics, governance=False),
                ),
                StageSpec(
                    name=StageName.PROD,
                    capabilities=StageCapabilities(analytics=prod_analytics, governance=False),
                ),
            ),
        )
    )


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.tooling.aws import AwsCli
    from olf.tooling.azure import AzureCli
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
        aws=AwsCli(runner, resolver),
        azure=AzureCli(runner, resolver),
    )


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


def _fail(stderr: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=1, stdout="", stderr=stderr, duration_seconds=0.0)


class _PlatformScriptedRunner(RecordingRunner):
    """Chart cache hits (`helm show chart` succeeds) so tests exercise orchestration, not chart caching."""

    def __init__(
        self,
        *,
        namespace_exists: bool = False,
        legacy_namespace_exists: bool = False,
        applied_stages: str | None = None,
    ) -> None:
        super().__init__()
        self._namespace_exists = namespace_exists
        self._legacy_namespace_exists = legacy_namespace_exists
        self._applied_stages = applied_stages

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = [str(part) for part in (command.argv if hasattr(command, "argv") else command)]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if argv[0] == "helm" and argv[1:3] == ["show", "chart"]:
            return _ok()
        if any(str(argument) == "stage_names" for argument in argv):
            return _ok(self._applied_stages or "[]")
        if "namespace" in argv and "-l" in argv:
            return _ok()
        if argv[0] == "terraform" and "state" in argv:
            return _ok() if self._namespace_exists else _fail()
        if argv[0] == "kubectl" and "namespace" in argv and "get" in argv:
            if "lakehouse" in argv:
                return _ok() if self._legacy_namespace_exists else _fail()
            return _ok() if self._namespace_exists else _fail()
        return _ok()


def test_platform_up_does_not_log_in_to_a_registry_for_superset(tmp_path: Path) -> None:
    config = _config(tmp_path, enable_analytics="true")
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    config.charts["trino"].package_path.write_text("cached")
    config.charts["dagster"].package_path.write_text("cached")
    backend = FakeCloudBackend(scope="aws")
    tools = _toolkit(_PlatformScriptedRunner())

    platform.platform_up(config, tools, backend, _FACTS, env={})

    assert "registry_login" not in backend.calls


@pytest.mark.parametrize(("scope", "provider"), [("aws", Provider.AWS), ("azure", Provider.AZURE)])
def test_platform_up_refuses_removing_an_applied_stage(tmp_path: Path, scope: str, provider: Provider) -> None:
    config = _config(tmp_path, enable_analytics="false", provider=provider)
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    config.charts["trino"].package_path.write_text("cached")
    config.charts["dagster"].package_path.write_text("cached")
    backend = FakeCloudBackend(scope=scope)
    tools = _toolkit(_PlatformScriptedRunner(applied_stages='["dev", "prod"]'))

    with pytest.raises(DeploymentPreconditionError, match="prod"):
        platform.platform_up(config, tools, backend, _FACTS, env={})


def test_azure_platform_up_refuses_to_replace_the_legacy_shared_namespace(tmp_path: Path) -> None:
    config = _config(tmp_path, enable_analytics="false", provider=Provider.AZURE)
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    config.charts["trino"].package_path.write_text("cached")
    config.charts["dagster"].package_path.write_text("cached")
    backend = FakeCloudBackend(scope="azure")
    tools = _toolkit(_PlatformScriptedRunner(legacy_namespace_exists=True))

    with pytest.raises(DeploymentPreconditionError, match="lakehouse"):
        platform.platform_up(config, tools, backend, _FACTS, env={})


def test_platform_up_uses_backend_apply_variables_and_var_file(tmp_path: Path) -> None:
    config = _config(tmp_path, enable_analytics="false")
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    config.charts["trino"].package_path.write_text("cached")
    config.charts["dagster"].package_path.write_text("cached")
    backend = FakeCloudBackend(scope="aws")
    runner = _PlatformScriptedRunner()
    tools = _toolkit(runner)

    platform.platform_up(config, tools, backend, _FACTS, env={})

    assert "platform_apply_variables" in backend.calls
    apply_call = next(c for c in runner.calls if c.argv[0] == "terraform" and c.argv[2:3] == ["apply"])
    assert "-var=namespace=olf-dev" in apply_call.argv


def test_platform_guards_use_the_foundation_kube_context(tmp_path: Path) -> None:
    config = _config(tmp_path, enable_analytics="false")
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    config.charts["trino"].package_path.write_text("cached")
    config.charts["dagster"].package_path.write_text("cached")
    runner = _PlatformScriptedRunner()

    platform.platform_up(config, _toolkit(runner), FakeCloudBackend(scope="aws"), _FACTS, env={})

    guard_calls = [
        call
        for call in runner.calls
        if call.argv[0] == "kubectl" and ("-l" in call.argv or "lakehouse" in call.argv)
    ]
    assert guard_calls
    assert all(call.argv[1:3] == ["--context", _FACTS.kube_context] for call in guard_calls)


def test_platform_up_never_passes_var_file_to_azure_apply_even_with_explicit_override(tmp_path: Path) -> None:
    """P1 regression: an explicit `--var-file` for a combined `olf deploy
    --provider azure --var-file <foundation.tfvars>` run must reach the
    foundation apply but never the platform apply - Azure's platform
    Terraform root declares none of the foundation-only variables a
    foundation tfvars file sets and rejects `-var-file` entirely (ADR
    0027).
    """
    explicit = tmp_path / "explicit.tfvars"
    explicit.write_text("resource_group = \"rg\"\n")
    context = DeploymentContext.azure(repo_root=tmp_path, profile=Profile.SLIM)
    config = CloudDeploymentConfig.from_environment({}, context=context, var_file=explicit)
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    config.charts["trino"].package_path.write_text("cached")
    config.charts["dagster"].package_path.write_text("cached")
    backend = FakeCloudBackend(scope="azure")
    runner = _PlatformScriptedRunner()
    tools = _toolkit(runner)

    platform.platform_up(config, tools, backend, _FACTS, env={})

    apply_call = next(c for c in runner.calls if c.argv[0] == "terraform" and c.argv[2:3] == ["apply"])
    assert not any(arg.startswith("-var-file=") for arg in apply_call.argv)


def test_platform_up_cleans_up_polaris_jobs_only_when_backend_requests_it(tmp_path: Path) -> None:
    config = _config(tmp_path, enable_analytics="false")
    config.paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
    config.charts["trino"].package_path.write_text("cached")
    config.charts["dagster"].package_path.write_text("cached")

    backend_no_cleanup = FakeCloudBackend(scope="aws", cleanup_polaris=False)
    tools = _toolkit(_PlatformScriptedRunner())
    platform.platform_up(config, tools, backend_no_cleanup, _FACTS, env={})
    assert not any("get" in c.argv and "jobs" in c.argv for c in tools.runner.calls)

    backend_cleanup = FakeCloudBackend(scope="azure", cleanup_polaris=True)
    tools2 = _toolkit(_PlatformScriptedRunner())
    platform.platform_up(config, tools2, backend_cleanup, _FACTS, env={})
    jobs_calls = [c for c in tools2.runner.calls if "get" in c.argv and "jobs" in c.argv]
    assert jobs_calls
    # Polaris runs in the shared namespace ("olf-system"), not the selected
    # stage's ("olf-dev") - scanning the stage namespace here never finds a
    # failed Polaris bootstrap job to clean up before retrying.
    assert all("-n" in c.argv and c.argv[c.argv.index("-n") + 1] == "olf-system" for c in jobs_calls)
