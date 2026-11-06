import base64
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from olf.common.deployment.errors import ToolchainError
from olf.infra import k8s


def test_secret_value_uses_explicit_kube_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []
    encoded = base64.b64encode(b"secret").decode("ascii")
    monkeypatch.setattr(
        k8s,
        "_kubectl",
        lambda args, **kwargs: calls.append((args, kwargs)) or encoded,
    )

    value = k8s.secret_value(
        "seaweedfs-s3-creds",
        "AWS_ACCESS_KEY_ID",
        "lakehouse",
        kube_context="kind-openlakeforge-local",
    )

    assert value == "secret"
    assert calls == [
        (
            [
                "get",
                "secret",
                "seaweedfs-s3-creds",
                "-n",
                "lakehouse",
                "-o",
                "jsonpath={.data.AWS_ACCESS_KEY_ID}",
            ],
            {"capture": True, "kube_context": "kind-openlakeforge-local", "environ": None},
        )
    ]


def test_kubectl_command_uses_environment_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBE_CONTEXT", "aks-openlakeforge-poc")

    assert k8s.kubectl_command(["get", "pods"]) == [
        "kubectl",
        "--context",
        "aks-openlakeforge-poc",
        "get",
        "pods",
    ]


def test_kubectl_command_requires_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUBE_CONTEXT", raising=False)

    with pytest.raises(k8s.KubectlError, match="KUBE_CONTEXT is required"):
        k8s.kubectl_command(["get", "pods"])


def test_port_forward_uses_explicit_kube_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = Mock()
    process.poll.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(k8s.ProcessRunner, "popen", popen)
    monkeypatch.setattr(k8s, "_wait_for_port_forward", Mock())
    # Executable resolution (#127) is covered by test_toolchain_resolver.py;
    # here only the argv shape matters.
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda: "kubectl")

    with k8s.port_forward(
        "superset",
        8088,
        "lakehouse",
        local_port=18088,
        log_path=str(tmp_path / "port-forward.log"),
        kube_context="kind-openlakeforge-local",
    ) as local_port:
        assert local_port == 18088

    assert popen.call_args.args[0] == [
        "kubectl",
        "--context",
        "kind-openlakeforge-local",
        "port-forward",
        "svc/superset",
        "18088:8088",
        "-n",
        "lakehouse",
    ]
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10)


def test_wait_for_port_forward_retries_until_the_listener_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = Mock()
    process.poll.return_value = None
    attempts = iter((OSError("not ready"), MagicMock()))

    def create_connection(*_args: object, **_kwargs: object) -> MagicMock:
        result = next(attempts)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(k8s.socket, "create_connection", create_connection)
    monkeypatch.setattr(k8s.time, "sleep", lambda _delay: None)

    k8s._wait_for_port_forward(process, 18088, str(tmp_path / "port-forward.log"))


def test_wait_for_port_forward_reports_an_early_process_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_path = tmp_path / "port-forward.log"
    log_path.write_text("error: services \"missing\" not found\n")
    process = Mock()
    process.poll.return_value = 1

    with pytest.raises(k8s.KubectlError, match="services .*missing.*not found"):
        k8s._wait_for_port_forward(process, 18088, str(log_path))


def test_wait_for_port_forward_times_out_and_reports_the_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_path = tmp_path / "port-forward.log"
    log_path.write_text("still starting\n")
    process = Mock()
    process.poll.return_value = None
    clock = iter((0.0, 0.0, k8s.PORT_FORWARD_READY_TIMEOUT_SECONDS + 0.1))

    monkeypatch.setattr(k8s.socket, "create_connection", Mock(side_effect=OSError("not ready")))
    monkeypatch.setattr(k8s.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(k8s.time, "sleep", lambda _delay: None)

    with pytest.raises(k8s.KubectlError, match=r"(?s)timed out.*still starting"):
        k8s._wait_for_port_forward(process, 18088, str(log_path))


def test_port_forward_cleans_up_after_readiness_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = Mock()
    process.poll.return_value = 0
    monkeypatch.setattr(k8s.ProcessRunner, "popen", Mock(return_value=process))
    monkeypatch.setattr(k8s, "_wait_for_port_forward", Mock(side_effect=k8s.KubectlError("not ready")))
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda: "kubectl")

    with pytest.raises(k8s.KubectlError, match="not ready"):
        with k8s.port_forward(
            "polaris",
            8181,
            "lakehouse",
            log_path=str(tmp_path / "port-forward.log"),
            kube_context="kind-openlakeforge-local",
        ):
            pass

    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10)


def _job_kubectl(status: str, log: str = ""):
    def fake(args, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        return {"get": status, "logs": log}.get(args[0], "")

    return fake


_JOB = {"metadata": {"name": "ingest", "namespace": "olf-system"}}


def test_run_job_returns_on_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "_kubectl", _job_kubectl("Complete"))
    k8s.run_job(_JOB, timeout_seconds=60)


def test_run_job_raises_promptly_on_failed_with_the_token_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "_kubectl", _job_kubectl("Failed", "boom jwt=eyJhbGci.eyJzdWIi.c2ln-_x end"))
    monkeypatch.setattr(k8s.time, "sleep", lambda _s: pytest.fail("kept polling a failed Job"))
    with pytest.raises(k8s.KubectlError) as raised:
        k8s.run_job(_JOB, timeout_seconds=60)
    assert "boom jwt=*** end" in str(raised.value)
    assert "eyJ" not in str(raised.value)


@pytest.mark.parametrize("overwrite", [False, True])
def test_write_secret_sends_credentials_on_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overwrite: bool
) -> None:
    from types import SimpleNamespace

    from olf.common.tooling.kubectl import Kubectl
    from olf.common.tooling.process import CommandResult

    runner = Mock()
    runner.run.return_value = CommandResult((), 0, "", "", 0.0)
    resolver = Mock()
    resolver.resolve.return_value = tmp_path / "kubectl"
    toolkit = SimpleNamespace(kubectl=Kubectl(runner, resolver))
    monkeypatch.setattr(k8s.Toolkit, "default", lambda: toolkit)
    monkeypatch.setenv("KUBE_CONTEXT", "kind-test")
    monkeypatch.setattr(k8s.tempfile, "NamedTemporaryFile", Mock(side_effect=AssertionError("secret written to disk")))

    k8s.write_secret("bootstrap", "olf-system", {"password": "private-password"}, overwrite=overwrite)

    argv = runner.run.call_args.args[0]
    assert "private-password" not in str(argv)
    assert argv[-2:] == ["-f", "-"]
    assert "--context" in argv and "kind-test" in argv
    assert ("apply" if overwrite else "create") in argv
    manifest = json.loads(runner.run.call_args.kwargs["input_text"])
    assert manifest["metadata"] == {"name": "bootstrap", "namespace": "olf-system"}
    assert manifest["stringData"] == {"password": "private-password"}
    if overwrite:
        assert "--server-side" in argv


@pytest.fixture
def fake_kubectl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A kubectl stand-in: echoes its argv on stdout, `err` on stderr, exits with $FAKE_KUBECTL_RC."""
    script = tmp_path / "kubectl"
    script.write_text('#!/bin/sh\necho "args: $*"\necho err >&2\nexit "${FAKE_KUBECTL_RC:-0}"\n')
    script.chmod(0o755)
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda: str(script))
    return script


def test_kubectl_captures_output_and_raises_with_stderr_on_failure(
    monkeypatch: pytest.MonkeyPatch, fake_kubectl: Path
) -> None:
    assert k8s._kubectl(["get", "pods"], capture=True, kube_context="ctx") == "args: --context ctx get pods\n"

    monkeypatch.setenv("FAKE_KUBECTL_RC", "1")
    with pytest.raises(k8s.KubectlError, match=r"--context ctx get pods failed: err$"):
        k8s._kubectl(["get", "pods"], capture=True, kube_context="ctx")


def test_kubectl_uncaptured_output_reaches_the_terminal(
    capfd: pytest.CaptureFixture[str], fake_kubectl: Path
) -> None:
    assert k8s._kubectl(["apply", "-f", "x"], kube_context="ctx") == ""

    out, err = capfd.readouterr()
    assert "args: --context ctx apply -f x" in out
    assert "err" in err


@pytest.mark.parametrize("returncode", [0, 1])
def test_exec_succeeds_reflects_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, fake_kubectl: Path, returncode: int
) -> None:
    monkeypatch.setenv("KUBE_CONTEXT", "ctx")
    monkeypatch.setenv("FAKE_KUBECTL_RC", str(returncode))

    assert k8s.exec_succeeds("dep", "ns", ["true"]) is (returncode == 0)


@pytest.fixture
def env_probe_kubectl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """A `kubectl` on PATH that reports the env it was launched with; the process env carries none of it.

    Returns the mapping a caller passes as `environ=`.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "kubectl"
    script.write_text(
        '#!/bin/sh\necho "args: $* kubeconfig=$KUBECONFIG mode=$OLF_TOOLCHAIN_MODE"\n'
        '[ "$KUBECONFIG" = "/explicit/kubeconfig" ]\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    for name in ("KUBE_CONTEXT", "KUBECONFIG", "OLF_TOOLCHAIN_MODE"):
        monkeypatch.delenv(name, raising=False)
    # Toolchain mode comes from the mapping alone: `host` resolves kubectl from PATH.
    return {"KUBE_CONTEXT": "explicit-ctx", "KUBECONFIG": "/explicit/kubeconfig", "OLF_TOOLCHAIN_MODE": "host"}


def test_kubectl_child_sees_the_explicit_environ(env_probe_kubectl: dict[str, str]) -> None:
    out = k8s._kubectl(["get", "pods"], capture=True, environ=env_probe_kubectl)

    assert out == "args: --context explicit-ctx get pods kubeconfig=/explicit/kubeconfig mode=host\n"


def test_exec_succeeds_and_resource_exists_use_the_explicit_environ(env_probe_kubectl: dict[str, str]) -> None:
    assert k8s.exec_succeeds("dep", "ns", ["true"], environ=env_probe_kubectl)
    assert k8s.resource_exists("pod", "p", "ns", environ=env_probe_kubectl)
    with pytest.raises(k8s.KubectlError, match="KUBE_CONTEXT is required"):
        k8s.exec_succeeds("dep", "ns", ["true"])


def test_port_forward_child_sees_the_explicit_environ(env_probe_kubectl: dict[str, str], tmp_path: Path) -> None:
    log = tmp_path / "pf.log"
    # The probe exits at once, so the failure carries the child's log: the env it saw.
    with pytest.raises(k8s.KubectlError, match="kubeconfig=/explicit/kubeconfig mode=host"):
        with k8s.port_forward("svc", 80, "ns", log_path=str(log), environ=env_probe_kubectl):
            pass


def test_kubectl_executable_honours_the_toolchain_mode_of_the_given_mapping(
    env_probe_kubectl: dict[str, str],
) -> None:
    assert Path(k8s._kubectl_executable(env_probe_kubectl)).name == "kubectl"

    with pytest.raises(ToolchainError, match="unknown value 'bogus'"):
        k8s._kubectl_executable({"OLF_TOOLCHAIN_MODE": "bogus"})


@pytest.mark.parametrize("overwrite", [True, False])
def test_write_secret_child_sees_the_explicit_environ(env_probe_kubectl: dict[str, str], overwrite: bool) -> None:
    # The probe exits non-zero unless the kubectl child received the mapping's KUBECONFIG.
    k8s.write_secret("s", "ns", {"k": "v"}, overwrite=overwrite, environ=env_probe_kubectl)


def test_kubectl_does_not_get_back_a_variable_the_contract_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from olf.infra.deployment.contract_env import ContractEnvironment

    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    # Exits non-zero when the contract-unset variable reaches the child.
    kubectl.write_text('#!/bin/sh\n[ -z "${OLF_OPERATOR_ONLY+x}" ]\n')
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OLF_OPERATOR_ONLY", "from-the-shell")
    environ = {"KUBE_CONTEXT": "ctx", "OLF_TOOLCHAIN_MODE": "host"}
    contract = ContractEnvironment(
        environ=environ,
        namespace="ns",
        kube_context="ctx",
        kubeconfig_path=tmp_path / "kubeconfig",
        stage=None,
        unsets=("OLF_OPERATOR_ONLY",),
    )

    assert k8s.exec_succeeds("dep", "ns", ["true"], environ=contract)
    assert not k8s.exec_succeeds("dep", "ns", ["true"], environ=environ)


def test_exec_to_file_streams_stdout_to_the_file_and_reports_stderr_on_failure(
    monkeypatch: pytest.MonkeyPatch, fake_kubectl: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUBE_CONTEXT", "ctx")
    destination = tmp_path / "out.dump"

    k8s.exec_to_file("postgresql-0", "olf-system", ["pg_dump", "db"], destination, container="postgresql")

    assert (
        destination.read_text() == "args: --context ctx exec postgresql-0 -n olf-system -c postgresql -- pg_dump db\n"
    )

    monkeypatch.setenv("FAKE_KUBECTL_RC", "1")
    with pytest.raises(k8s.KubectlError, match=r"pg_dump failed: err$"):
        k8s.exec_to_file("postgresql-0", "olf-system", ["pg_dump", "db"], destination)


def test_a_deployment_scaled_to_zero_reports_the_replicas_it_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        {"metadata": {"name": "up"}, "spec": {"replicas": 2}},
        {"metadata": {"name": "down", "annotations": {k8s.REPLICAS_ANNOTATION: "3"}}, "spec": {"replicas": 0}},
        {"metadata": {"name": "never"}, "spec": {"replicas": 0}},
    ]
    monkeypatch.setattr(k8s, "_kubectl", lambda *_a, **_k: json.dumps({"items": items}))

    assert k8s.deployment_replica_counts("olf-dev") == {"up": 2, "down": 3, "never": 0}


class _FakeDeployment:
    """One Deployment as the API server holds it: replicas, annotations and the pods actually running.

    A scale changes only the desired count. Surplus pods terminate one per `get pods` poll, unless `stuck`;
    `rollout status` returns once the running pods match the desired count.
    """

    def __init__(self, name: str, replicas: int, *, stuck: bool = False) -> None:
        self.name, self.replicas, self.pods, self.stuck = name, replicas, replicas, stuck
        self.annotations: dict[str, str] = {}

    def kubectl(self, args: list[str], **_: object) -> str:
        if args[0] == "annotate":
            change = args[4]
            if change.endswith("-"):
                self.annotations.pop(change[:-1], None)
            else:
                key, value = change.split("=", 1)
                self.annotations[key] = value
        elif args[0] == "scale":
            self.replicas = int(args[2].removeprefix("--replicas="))
        elif args[:2] == ["rollout", "status"]:
            self.pods = self.replicas
        elif args[:2] == ["get", "pods"]:
            running = "".join(f"pod/{self.name}-{i}\n" for i in range(self.pods))
            if self.pods > self.replicas and not self.stuck:
                self.pods -= 1
            return running
        elif "jsonpath={.spec.selector.matchLabels}" in args:
            return json.dumps({"app": self.name})
        elif args[:2] == ["get", "deployment"]:
            item = {
                "metadata": {"name": self.name, "annotations": self.annotations},
                "spec": {"replicas": self.replicas},
            }
            return json.dumps({"items": [item]})
        return ""


def test_scale_to_zero_returns_only_once_the_pods_are_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _FakeDeployment("dagster-daemon", 2)
    monkeypatch.setattr(k8s, "_kubectl", daemon.kubectl)

    k8s.scale_deployment("dagster-daemon", "olf-dev", 0, poll_seconds=0)

    assert (daemon.replicas, daemon.pods) == (0, 0)


def test_scale_to_zero_fails_when_the_pods_never_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _FakeDeployment("dagster-daemon", 1, stuck=True)
    monkeypatch.setattr(k8s, "_kubectl", daemon.kubectl)

    with pytest.raises(k8s.KubectlError, match="did not stop"):
        k8s.scale_deployment("dagster-daemon", "olf-dev", 0, timeout_seconds=0, poll_seconds=0)


def test_scale_up_returns_once_the_rollout_has_the_pods_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stuck pods would make a wait-for-pods-to-go loop time out at once; a scale-up must wait on the rollout.
    daemon = _FakeDeployment("dagster-daemon", 0, stuck=True)
    monkeypatch.setattr(k8s, "_kubectl", daemon.kubectl)

    k8s.scale_deployment("dagster-daemon", "olf-dev", 1, timeout_seconds=0, poll_seconds=0)

    assert (daemon.replicas, daemon.pods) == (1, 1)


def test_a_deployment_scaled_to_zero_with_remember_still_reports_its_count(monkeypatch: pytest.MonkeyPatch) -> None:
    superset = _FakeDeployment("superset", 2)
    monkeypatch.setattr(k8s, "_kubectl", superset.kubectl)

    k8s.scale_deployment("superset", "olf-dev", 0, remember=2, poll_seconds=0)

    # A rerun after an interruption here reads the count back from the Deployment, not from the caller.
    assert superset.replicas == 0
    assert k8s.deployment_replica_counts("olf-dev") == {"superset": 2}


def test_scaling_back_up_with_remember_zero_drops_the_remembered_count(monkeypatch: pytest.MonkeyPatch) -> None:
    superset = _FakeDeployment("superset", 2)
    monkeypatch.setattr(k8s, "_kubectl", superset.kubectl)
    k8s.scale_deployment("superset", "olf-dev", 0, remember=2, poll_seconds=0)

    k8s.scale_deployment("superset", "olf-dev", 2, remember=0)

    assert superset.replicas == 2
    assert k8s.REPLICAS_ANNOTATION not in superset.annotations
    # Scaled to 0 later by someone else, it no longer reports the count this scale-down remembered.
    superset.replicas = 0
    assert k8s.deployment_replica_counts("olf-dev") == {"superset": 0}


def test_scaling_without_remember_leaves_no_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _FakeDeployment("dagster-daemon", 1)
    monkeypatch.setattr(k8s, "_kubectl", daemon.kubectl)

    k8s.scale_deployment("dagster-daemon", "olf-dev", 0, poll_seconds=0)
    k8s.scale_deployment("dagster-daemon", "olf-dev", 1)

    assert daemon.annotations == {}
