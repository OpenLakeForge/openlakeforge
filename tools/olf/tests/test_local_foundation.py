from __future__ import annotations

import dataclasses
import socket
from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.context import DeploymentContext
from olf.deployment.engine import Toolkit
from olf.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.deployment.local import foundation
from olf.deployment.local.config import LocalDeploymentConfig
from olf.profile import AccessSpec
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm")
_REAL_LISTENING = foundation._listening


@pytest.fixture(autouse=True)
def _host_ports_free(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Host ports 80/443 as these tests see them, independent of what this machine runs."""
    busy: set[int] = set()
    monkeypatch.setattr(foundation, "_listening", lambda port: port in busy)
    return busy


def _config(tmp_path: Path) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path)
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit_with_runner(runner: RecordingRunner) -> Toolkit:
    resolver = PathExecutableResolver(overrides={tool: Path(tool) for tool in _TOOLS})
    from olf.tooling.aws import AwsCli
    from olf.tooling.azure import AzureCli
    from olf.tooling.docker import Docker
    from olf.tooling.helm import Helm
    from olf.tooling.kind import Kind
    from olf.tooling.kubectl import Kubectl
    from olf.tooling.terraform import Terraform

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


class _ScriptedRunner(RecordingRunner):
    """Returns a scripted result based on a predicate over the argv."""

    def __init__(self, rules: list[tuple[callable, CommandResult]], default: CommandResult) -> None:
        super().__init__()
        self._rules = rules
        self._default = default

    def run(self, command, **kwargs):  # type: ignore[override]
        from _tooling_support import RecordedCall

        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        for predicate, result in self._rules:
            if predicate(argv):
                return result
        return self._default


def test_foundation_apply_variables_exact_order_and_content(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = _toolkit_with_runner(RecordingRunner())

    variables = foundation.foundation_apply_variables(config, tools)

    assert list(variables.keys()) == [
        "cluster_name",
        "cluster_config_path",
        "kubeconfig_path",
        "kind_wait_timeout",
        "reset_existing_cluster",
        "kind_executable_path",
        "kubectl_executable_path",
    ]
    assert variables["cluster_name"] == "openlakeforge-local"
    assert variables["reset_existing_cluster"] == "false"
    assert variables["kind_executable_path"] == "kind"
    assert variables["kubectl_executable_path"] == "kubectl"


def test_foundation_destroy_variables_are_the_four_var_subset(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = _toolkit_with_runner(RecordingRunner())

    variables = foundation.foundation_destroy_variables(config, tools)

    assert list(variables.keys()) == ["cluster_name", "cluster_config_path", "kubeconfig_path", "kind_executable_path"]
    assert variables["kind_executable_path"] == "kind"


def test_foundation_up_applies_exports_kubeconfig_and_checks_reachability(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "get-contexts" in argv, _ok("kind-openlakeforge-local\n")),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    foundation.foundation_up(config, tools, env={})

    assert runner.calls[0].argv[:2] == ["docker", "version"]
    assert ["terraform", "-chdir=" + str(config.paths.foundation_terraform_dir), "init"] == runner.calls[1].argv
    assert runner.calls[2].argv[2] == "apply"
    export_call = next(c for c in runner.calls if "export" in c.argv)
    assert export_call.argv[:3] == ["kind", "export", "kubeconfig"]
    assert any("get-contexts" in c.argv for c in runner.calls)
    assert any("cluster-info" in c.argv for c in runner.calls)


def test_foundation_up_raises_actionable_error_when_docker_is_unreachable(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class _DockerUnreachableRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if argv[:2] == ["docker", "version"]:
                raise CommandExecutionError(argv, 1, stderr="Cannot connect to the Docker daemon")
            return _ok()

    runner = _DockerUnreachableRunner()
    tools = _toolkit_with_runner(runner)

    with pytest.raises(DeploymentPreconditionError, match="Docker is not reachable"):
        foundation.foundation_up(config, tools, env={})

    assert not any(c.argv[0] == "terraform" for c in runner.calls)


def test_foundation_down_is_idempotent_when_nothing_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _fail()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("")),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    foundation.foundation_down(config, tools, env={})

    assert not any(c.argv[1:2] == ["destroy"] for c in runner.calls)


def test_foundation_down_refuses_to_destroy_unmanaged_cluster(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _fail()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    with pytest.raises(DeploymentPreconditionError, match="does not own it"):
        foundation.foundation_down(config, tools, env={})


def test_foundation_down_refuses_when_namespace_still_present(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "namespace" in argv and "get" in argv, _ok()),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    with pytest.raises(DeploymentPreconditionError, match="still exist"):
        foundation.foundation_down(config, tools, env={})


def test_foundation_down_force_overrides_namespace_check(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "namespace" in argv and "get" in argv, _ok()),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    foundation.foundation_down(config, tools, env={}, force=True)

    assert any(c.argv[2:3] == ["destroy"] for c in runner.calls if c.argv[0] == "terraform")


def test_foundation_down_destroys_when_no_platform_resources_remain(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "namespace" in argv and "get" in argv and "-l" in argv, _ok("")),
            (lambda argv: "namespace" in argv and "get" in argv, _fail()),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    foundation.foundation_down(config, tools, env={})

    destroy_calls = [c for c in runner.calls if c.argv[0] == "terraform" and c.argv[2] == "destroy"]
    assert len(destroy_calls) == 1
    assert "-var=cluster_name=openlakeforge-local" in destroy_calls[0].argv
    assert not any("kind_wait_timeout" in arg for arg in destroy_calls[0].argv)


def test_foundation_down_refuses_while_only_the_shared_namespace_remains(tmp_path: Path) -> None:
    """The shared namespace holds PostgreSQL and its PVC. Checking only the
    selected stage would let a partially torn-down platform take the cluster
    and the metadata with it."""
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "namespace" in argv and "olf-system" in argv, _ok()),
            (lambda argv: "namespace" in argv and "olf-dev" in argv, _fail()),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    with pytest.raises(DeploymentPreconditionError, match="olf-system"):
        foundation.foundation_down(config, tools, env={})


def test_foundation_down_refuses_while_a_retired_stage_namespace_remains(tmp_path: Path) -> None:
    """A stage disabled in the profile drops out of the topology but keeps its
    namespace and its workloads. Checking only what the profile names now
    would let this delete the cluster out from under it."""
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "namespace" in argv and "get" in argv and "-l" in argv, _ok("olf-prod\n")),
            (lambda argv: "namespace" in argv and "get" in argv, _fail()),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    with pytest.raises(DeploymentPreconditionError, match="olf-prod"):
        foundation.foundation_down(config, tools, env={})


def test_foundation_down_fails_closed_when_namespace_discovery_errors(tmp_path: Path) -> None:
    """A label query matching nothing succeeds, so a failure means the cluster
    cannot be inspected -- and destroying it would be irreversible. `--force`
    stays the way past a cluster whose API server is gone."""
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "namespace" in argv and "get" in argv and "-l" in argv, _fail("connection refused")),
            (lambda argv: "namespace" in argv and "get" in argv, _fail()),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    with pytest.raises(DeploymentPreconditionError, match="connection refused"):
        foundation.foundation_down(config, tools, env={})

    assert not any(c.argv[0] == "terraform" and "destroy" in c.argv for c in runner.calls)


def test_foundation_down_force_skips_namespace_discovery_entirely(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: "state" in argv, _ok()),
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "namespace" in argv and "get" in argv and "-l" in argv, _fail("connection refused")),
        ],
        default=_ok(),
    )
    tools = _toolkit_with_runner(runner)

    foundation.foundation_down(config, tools, env={}, force=True)

    assert any(c.argv[2:3] == ["destroy"] for c in runner.calls if c.argv[0] == "terraform")


def test_foundation_up_rejects_an_issuer_the_local_provider_does_not_ship(tmp_path: Path) -> None:
    default = DeploymentContext.local(repo_root=tmp_path).topology
    context = DeploymentContext.local(
        repo_root=tmp_path, topology=dataclasses.replace(default, access=AccessSpec(issuer="letsencrypt"))
    )
    config = LocalDeploymentConfig.from_environment({}, context=context)
    runner = RecordingRunner(_ok())

    with pytest.raises(DeploymentPreconditionError, match="ships only the 'local-ca' issuer"):
        foundation.foundation_up(config, _toolkit_with_runner(runner), env={})

    assert not any(c.argv[0] == "terraform" for c in runner.calls)


def test_foundation_up_names_a_taken_ingress_port_before_kind_runs(tmp_path: Path, _host_ports_free: set[int]) -> None:
    _host_ports_free.add(443)
    runner = _ScriptedRunner(rules=[(lambda argv: argv[:2] == ["kind", "get"], _ok("other-cluster\n"))], default=_ok())

    with pytest.raises(DeploymentPreconditionError, match=r"127\.0\.0\.1:443 already in use"):
        foundation.foundation_up(_config(tmp_path), _toolkit_with_runner(runner), env={})

    assert not any(c.argv[0] == "terraform" for c in runner.calls)


def test_foundation_up_accepts_ports_held_by_its_own_cluster(tmp_path: Path, _host_ports_free: set[int]) -> None:
    _host_ports_free.update({80, 443})
    runner = _ScriptedRunner(
        rules=[
            (lambda argv: argv[:2] == ["kind", "get"], _ok("openlakeforge-local\n")),
            (lambda argv: "get-contexts" in argv, _ok("kind-openlakeforge-local\n")),
        ],
        default=_ok(),
    )

    foundation.foundation_up(_config(tmp_path), _toolkit_with_runner(runner), env={})

    assert any(c.argv[2:3] == ["apply"] for c in runner.calls if c.argv[0] == "terraform")


def test_listening_detects_a_bound_loopback_port() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert _REAL_LISTENING(port)
    assert not _REAL_LISTENING(port)
