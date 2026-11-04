import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

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
            {"capture": True, "kube_context": "kind-openlakeforge-local"},
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
    monkeypatch.setattr(k8s.subprocess, "Popen", popen)
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
    monkeypatch.setattr(k8s.subprocess, "Popen", Mock(return_value=process))
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
