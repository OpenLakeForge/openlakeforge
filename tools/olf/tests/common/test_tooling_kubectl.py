from __future__ import annotations

from pathlib import Path

import pytest
from _tooling_support import RecordingRunner

from olf.common.deployment.errors import CommandExecutionError
from olf.common.tooling.kubectl import KubeContextUnreachableError, Kubectl
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver


def _kubectl(result: CommandResult | None = None) -> tuple[Kubectl, RecordingRunner]:
    runner = RecordingRunner(result)
    resolver = PathExecutableResolver(overrides={"kubectl": Path("kubectl")})
    return Kubectl(runner, resolver), runner


def test_get_builds_exact_argv() -> None:
    kubectl, runner = _kubectl()

    kubectl.get("pods", namespace="lakehouse", context="kind-openlakeforge-local")

    assert runner.last_call.argv == [
        "kubectl",
        "--context",
        "kind-openlakeforge-local",
        "get",
        "pods",
        "-n",
        "lakehouse",
    ]


def test_get_with_name_and_output() -> None:
    kubectl, runner = _kubectl()

    kubectl.get("secret", name="polaris-credentials", namespace="lakehouse", output="json")

    assert runner.last_call.argv == [
        "kubectl",
        "get",
        "secret",
        "polaris-credentials",
        "-n",
        "lakehouse",
        "-o",
        "json",
    ]


def test_kubeconfig_is_passed_via_scoped_environment_not_argv() -> None:
    kubectl, runner = _kubectl()
    kubeconfig = Path("/repo/.tmp/kubeconfigs/local.yaml")

    kubectl.cluster_info(context="kind-openlakeforge-local", kubeconfig=kubeconfig)

    assert "--kubeconfig" not in runner.last_call.argv
    assert runner.last_call.kwargs["env"] == {"KUBECONFIG": str(kubeconfig)}


def test_delete_defaults_to_ignore_not_found() -> None:
    kubectl, runner = _kubectl()

    kubectl.delete("job", "polaris-bootstrap-abc123", namespace="lakehouse")

    assert runner.last_call.argv == [
        "kubectl",
        "delete",
        "job",
        "polaris-bootstrap-abc123",
        "-n",
        "lakehouse",
        "--ignore-not-found",
    ]


def test_delete_forwards_extra_args() -> None:
    kubectl, runner = _kubectl()

    kubectl.delete("job", "superset-init-db", namespace="lakehouse", extra_args=["--wait=true"])

    assert runner.last_call.argv == [
        "kubectl",
        "delete",
        "job",
        "superset-init-db",
        "-n",
        "lakehouse",
        "--ignore-not-found",
        "--wait=true",
    ]


def test_port_forward_argv_builds_exact_argv() -> None:
    kubectl, _runner = _kubectl()

    argv = kubectl.port_forward_argv(
        "svc/trino", ["8080:8080"], namespace="lakehouse", context="kind-openlakeforge-local"
    )

    assert argv == [
        "kubectl",
        "--context",
        "kind-openlakeforge-local",
        "port-forward",
        "svc/trino",
        "8080:8080",
        "--address",
        "127.0.0.1",
        "-n",
        "lakehouse",
    ]


def test_port_forward_argv_omits_context_when_absent() -> None:
    kubectl, _runner = _kubectl()

    argv = kubectl.port_forward_argv("pod/dagster-webserver-abc", ["3000:80"])

    assert "--context" not in argv


def test_config_get_contexts_parses_output_lines() -> None:
    kubectl, _runner = _kubectl(
        CommandResult(
            argv=(),
            returncode=0,
            stdout="kind-openlakeforge-local\neks-openlakeforge-poc\n",
            stderr="",
            duration_seconds=0.0,
        )
    )

    contexts = kubectl.config_get_contexts()

    assert contexts == ["kind-openlakeforge-local", "eks-openlakeforge-poc"]


def test_require_context_reachable_succeeds_when_present_and_reachable() -> None:
    kubectl, runner = _kubectl(
        CommandResult(argv=(), returncode=0, stdout="kind-openlakeforge-local\n", stderr="", duration_seconds=0.0)
    )

    kubectl.require_context_reachable("kind-openlakeforge-local")

    assert len(runner.calls) == 2  # config get-contexts, then cluster-info


def test_require_context_reachable_raises_when_context_absent() -> None:
    kubectl, _runner = _kubectl(
        CommandResult(argv=(), returncode=0, stdout="some-other-context\n", stderr="", duration_seconds=0.0)
    )

    with pytest.raises(KubeContextUnreachableError):
        kubectl.require_context_reachable("kind-openlakeforge-local")


def test_require_context_reachable_raises_when_cluster_info_fails() -> None:
    from _tooling_support import RecordedCall

    class FlakyRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if "cluster-info" in argv:
                raise CommandExecutionError(argv, 1, stderr="connection refused")
            return CommandResult(
                argv=tuple(argv), returncode=0, stdout="kind-openlakeforge-local\n", stderr="", duration_seconds=0.0
            )

    runner = FlakyRunner()
    resolver = PathExecutableResolver(overrides={"kubectl": Path("kubectl")})
    kubectl = Kubectl(runner, resolver)

    with pytest.raises(KubeContextUnreachableError):
        kubectl.require_context_reachable("kind-openlakeforge-local")
