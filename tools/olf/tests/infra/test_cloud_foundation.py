from __future__ import annotations

from pathlib import Path

import pytest
from _cloud_support import FakeCloudBackend
from _tooling_support import RecordedCall, RecordingRunner

from olf.common.deployment.context import DeploymentContext
from olf.common.deployment.errors import DeploymentPreconditionError
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.cloud import foundation
from olf.infra.deployment.cloud.aws import AwsBackend
from olf.infra.deployment.cloud.backend import FoundationFacts
from olf.infra.deployment.cloud.config import CloudDeploymentConfig

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm", "aws", "az")


def _config(tmp_path: Path) -> CloudDeploymentConfig:
    context = DeploymentContext.aws(repo_root=tmp_path)
    return CloudDeploymentConfig.from_environment({}, context=context)


def _azure_config(tmp_path: Path, *, var_file: Path | None = None) -> CloudDeploymentConfig:
    context = DeploymentContext.azure(repo_root=tmp_path)
    return CloudDeploymentConfig.from_environment({}, context=context, var_file=var_file)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.common.tooling.docker import Docker
    from olf.common.tooling.helm import Helm
    from olf.common.tooling.kind import Kind
    from olf.common.tooling.kubectl import Kubectl
    from olf.common.tooling.terraform import Terraform

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


def _fail(stderr: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=1, stdout="", stderr=stderr, duration_seconds=0.0)


class _ScriptedRunner(RecordingRunner):
    def __init__(self, rules: list[tuple[callable, CommandResult]], default: CommandResult) -> None:
        super().__init__()
        self._rules = rules
        self._default = default

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        for predicate, result in self._rules:
            if predicate(argv):
                return result
        return self._default


def test_foundation_up_calls_preflight_apply_kubeconfig_and_reachability_in_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws")
    runner = _ScriptedRunner(
        rules=[(lambda argv: "get-contexts" in argv, _ok("fake-cluster\n"))],
        default=_ok(),
    )
    tools = _toolkit(runner)

    facts = foundation.foundation_up(config, tools, backend, environ={}, env={})

    assert facts.kube_context == "fake-cluster"
    assert backend.calls == [
        "preflight",
        "foundation_tfvars_file",
        "foundation_apply_variables",
        "resolve_foundation_facts",
        "update_kubeconfig",
    ]
    assert runner.calls[0].argv[-1] == "init"
    apply_call = next(c for c in runner.calls if c.argv[2:3] == ["apply"])
    assert "-var=cluster_name=fake-cluster" in apply_call.argv
    assert any("get-contexts" in c.argv for c in runner.calls)
    assert any("cluster-info" in c.argv for c in runner.calls)


def test_foundation_up_honors_explicit_var_file_override_for_azure(tmp_path: Path) -> None:
    """Azure's `foundation_tfvars_file` requires the resolved file to exist
    (there is no default sandbox.tfvars written in this test), so this
    proves an explicit `--var-file` bypasses that resolution entirely
    rather than the CLI's own override being silently dropped.
    """
    explicit = tmp_path / "explicit.tfvars"
    explicit.write_text("resource_group = \"rg\"\n")
    config = _azure_config(tmp_path, var_file=explicit)
    backend = FakeCloudBackend(scope="azure")
    runner = _ScriptedRunner(rules=[(lambda argv: "get-contexts" in argv, _ok("fake-cluster\n"))], default=_ok())
    tools = _toolkit(runner)

    foundation.foundation_up(config, tools, backend, environ={}, env={})

    apply_call = next(c for c in runner.calls if c.argv[2:3] == ["apply"])
    assert f"-var-file={explicit}" in apply_call.argv
    assert "foundation_tfvars_file" not in backend.calls


def test_foundation_down_honors_explicit_var_file_override_for_azure(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.tfvars"
    explicit.write_text("resource_group = \"rg\"\n")
    config = _azure_config(tmp_path, var_file=explicit)
    backend = FakeCloudBackend(scope="azure", cluster_reachable_result=False)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: "namespace" in argv and "get" in argv, _fail()),
        ],
        default=_ok(),
    )
    tools = _toolkit(runner)

    foundation.foundation_down(config, tools, backend, environ={}, env={})

    destroy_call = next(c for c in runner.calls if c.argv[2:3] == ["destroy"])
    assert f"-var-file={explicit}" in destroy_call.argv
    assert "foundation_tfvars_file" not in backend.calls


def test_foundation_up_falls_back_to_backend_resolution_when_no_explicit_var_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tfvars = tmp_path / "backend-resolved.tfvars"
    backend = FakeCloudBackend(scope="aws", tfvars_file=tfvars)
    runner = _ScriptedRunner(rules=[(lambda argv: "get-contexts" in argv, _ok("fake-cluster\n"))], default=_ok())
    tools = _toolkit(runner)

    foundation.foundation_up(config, tools, backend, environ={}, env={})

    apply_call = next(c for c in runner.calls if c.argv[2:3] == ["apply"])
    assert f"-var-file={tfvars}" in apply_call.argv
    assert "foundation_tfvars_file" in backend.calls


def test_resolve_foundation_tfvars_file_uses_the_foundation_roots_own_file_when_platform_root_also_has_one(
    tmp_path: Path,
) -> None:
    """The documented AWS setup instructs creating a `sandbox.tfvars` under
    both the foundation and platform Terraform roots. With no explicit
    `--var-file`/`AWS_TFVARS_FILE`, `config.terraform.var_file` auto-
    discovers the *platform* root's file (used for the platform apply) -
    that must never leak into the foundation apply, which needs its own
    root's file (mandatory tags, foundation-only settings like
    `public_access_cidrs`/`kubernetes_version`).
    """
    context = DeploymentContext.aws(repo_root=tmp_path)
    platform_tfvars = context.paths.platform_terraform_dir / "sandbox.tfvars"
    platform_tfvars.parent.mkdir(parents=True, exist_ok=True)
    platform_tfvars.write_text('project_code_image_tag = "platform"\n')
    foundation_tfvars = context.paths.foundation_terraform_dir / "sandbox.tfvars"
    foundation_tfvars.parent.mkdir(parents=True, exist_ok=True)
    foundation_tfvars.write_text('kubernetes_version = "1.30"\n')
    config = CloudDeploymentConfig.from_environment({}, context=context)
    assert config.terraform.var_file == platform_tfvars, "test setup: platform auto-discovery must have kicked in"

    resolved = foundation._resolve_foundation_tfvars_file(
        config, AwsBackend(), {}, context.paths.foundation_terraform_dir
    )

    assert resolved == foundation_tfvars


def test_foundation_up_includes_tfvars_file_when_backend_resolves_one(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tfvars = tmp_path / "sandbox.tfvars"
    backend = FakeCloudBackend(scope="aws", tfvars_file=tfvars)
    runner = _ScriptedRunner(rules=[(lambda argv: "get-contexts" in argv, _ok("fake-cluster\n"))], default=_ok())
    tools = _toolkit(runner)

    foundation.foundation_up(config, tools, backend, environ={}, env={})

    apply_call = next(c for c in runner.calls if c.argv[2:3] == ["apply"])
    assert f"-var-file={tfvars}" in apply_call.argv


def test_foundation_down_is_idempotent_when_no_state_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws")
    runner = _ScriptedRunner(rules=[(lambda argv: "state" in argv, _fail())], default=_ok())
    tools = _toolkit(runner)

    foundation.foundation_down(config, tools, backend, environ={}, env={})

    assert not any(c.argv[2:3] == ["destroy"] for c in runner.calls)
    assert "resolve_foundation_facts" not in backend.calls
    assert "preflight" not in backend.calls


def test_foundation_down_does_not_require_cloud_login_when_no_state_exists(tmp_path: Path) -> None:
    """A cleanup run against an already-clean checkout (or one whose creation
    failed before writing state) must reach the no-state no-op even with
    expired/missing cloud credentials - the removed teardown scripts never
    required a login just to discover there's nothing to tear down.
    """
    config = _config(tmp_path)

    class _RaisingPreflightBackend(FakeCloudBackend):
        def preflight(self, tools, *, env):  # noqa: ANN001, ARG002
            self.calls.append("preflight")
            raise DeploymentPreconditionError("not authenticated")

    backend = _RaisingPreflightBackend(scope="aws")
    runner = _ScriptedRunner(rules=[(lambda argv: "state" in argv, _fail())], default=_ok())
    tools = _toolkit(runner)

    foundation.foundation_down(config, tools, backend, environ={}, env={})

    assert "preflight" not in backend.calls


def test_foundation_down_calls_preflight_after_confirming_state_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", cluster_reachable_result=False)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: "namespace" in argv and "get" in argv, _fail()),
        ],
        default=_ok(),
    )
    tools = _toolkit(runner)

    foundation.foundation_down(config, tools, backend, environ={}, env={})

    assert backend.calls[0] == "preflight"
    assert backend.calls.index("preflight") < backend.calls.index("foundation_tfvars_file")


def test_foundation_down_refuses_when_namespace_still_present(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", cluster_reachable_result=True)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: "namespace" in argv and "get" in argv, _ok()),
        ],
        default=_ok(),
    )
    tools = _toolkit(runner)

    with pytest.raises(DeploymentPreconditionError, match="still exists"):
        foundation.foundation_down(config, tools, backend, environ={}, env={})


def test_foundation_down_force_overrides_namespace_check(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", cluster_reachable_result=True)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: "namespace" in argv and "get" in argv, _ok()),
        ],
        default=_ok(),
    )
    tools = _toolkit(runner)

    foundation.foundation_down(config, tools, backend, environ={}, env={}, force=True)

    assert any(c.argv[2:3] == ["destroy"] for c in runner.calls if c.argv and c.argv[0] == "terraform")


def test_foundation_down_skips_kubeconfig_refresh_but_still_checks_namespace_when_cluster_unreachable(
    tmp_path: Path,
) -> None:
    """`update_kubeconfig` is skipped when the cloud probe says the cluster
    is unreachable, but the namespace safety check still runs independently
    (against whatever kubeconfig already exists) - it only waives the guard
    when kubectl itself cannot reach the namespace, matching the removed
    teardown scripts.
    """
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", cluster_reachable_result=False)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: "namespace" in argv and "get" in argv, _fail()),
        ],
        default=_ok(),
    )
    tools = _toolkit(runner)

    foundation.foundation_down(config, tools, backend, environ={}, env={})

    assert "update_kubeconfig" not in backend.calls
    assert any(c.argv and "namespace" in c.argv and "get" in c.argv for c in runner.calls)
    assert any(c.argv[2:3] == ["destroy"] for c in runner.calls if c.argv and c.argv[0] == "terraform")


def test_foundation_down_refuses_when_cloud_probe_fails_but_namespace_is_still_reachable(
    tmp_path: Path,
) -> None:
    """P1: a failed/transient `eks describe-cluster`/`aks show` probe must
    not silently waive the namespace safety check - if kubectl (using
    whatever kubeconfig already exists) can still reach the namespace, the
    guard must fire exactly as if the probe had succeeded.
    """
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws", cluster_reachable_result=False)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: "namespace" in argv and "get" in argv, _ok()),
        ],
        default=_ok(),
    )
    tools = _toolkit(runner)

    with pytest.raises(DeploymentPreconditionError, match="still exists"):
        foundation.foundation_down(config, tools, backend, environ={}, env={})

    assert "update_kubeconfig" not in backend.calls
    assert not any(c.argv[2:3] == ["destroy"] for c in runner.calls if c.argv and c.argv[0] == "terraform")


def test_require_foundation_facts_raises_when_state_file_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = FakeCloudBackend(scope="aws")
    tools = _toolkit(RecordingRunner())

    with pytest.raises(DeploymentPreconditionError, match="foundation Terraform state is missing"):
        foundation.require_foundation_facts(config, tools, backend, env={})


def test_require_foundation_facts_raises_when_context_unreachable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    backend = FakeCloudBackend(scope="aws")
    runner = _ScriptedRunner(rules=[(lambda argv: "get-contexts" in argv, _ok(""))], default=_ok())
    tools = _toolkit(runner)

    with pytest.raises(DeploymentPreconditionError, match="is not reachable"):
        foundation.require_foundation_facts(config, tools, backend, env={})


def test_require_foundation_facts_returns_facts_when_reachable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    facts = FoundationFacts(
        cluster_name="eks-openlakeforge-poc",
        kube_context="eks-openlakeforge-poc",
        project_code_repository="123.dkr.ecr.eu-west-1.amazonaws.com/project-code",
        aws_region="eu-west-1",
    )
    backend = FakeCloudBackend(scope="aws", facts=facts)
    runner = _ScriptedRunner(
        rules=[(lambda argv: "get-contexts" in argv, _ok("eks-openlakeforge-poc\n"))], default=_ok()
    )
    tools = _toolkit(runner)

    result = foundation.require_foundation_facts(config, tools, backend, env={})

    assert result == facts


def test_require_foundation_facts_refreshes_kubeconfig_before_reachability_check(tmp_path: Path) -> None:
    """A missing/stale local kubeconfig (fresh checkout, or a granular phase run in a
    separate process from `foundation_up`) must not fail here - the removed shell paths
    always ran `aws eks update-kubeconfig`/`az aks get-credentials` before `kubectl
    cluster-info`.
    """
    config = _config(tmp_path)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    backend = FakeCloudBackend(scope="aws")
    runner = _ScriptedRunner(rules=[(lambda argv: "get-contexts" in argv, _ok("fake-cluster\n"))], default=_ok())
    tools = _toolkit(runner)

    foundation.require_foundation_facts(config, tools, backend, env={})

    assert backend.calls == ["resolve_foundation_facts", "update_kubeconfig"]
