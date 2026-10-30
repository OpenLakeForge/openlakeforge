from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from olf.deployment.portforward import ForwardSpec, ForwardTarget, PortForwardSupervisor
from olf.tooling.kubectl import Kubectl
from olf.tooling.resolver import PathExecutableResolver


class _FakePopen:
    def __init__(self, argv, **kwargs) -> None:  # noqa: ANN001, ARG002
        self.argv = argv
        self.terminated = False
        self.killed = False
        self.waited = False
        self._returncode: int | None = None

    def poll(self):
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):  # noqa: ANN001
        self.waited = True
        return self._returncode


def _kubectl() -> Kubectl:
    from _tooling_support import RecordingRunner

    return Kubectl(RecordingRunner(), PathExecutableResolver(overrides={"kubectl": Path("kubectl")}))


def _spec() -> ForwardSpec:
    return ForwardSpec(
        targets=(
            ForwardTarget("trino", "svc/trino", 8080, 8080),
            ForwardTarget("polaris", "svc/polaris", 8181, 8181),
        ),
        namespace="lakehouse",
        context="kind-openlakeforge-local",
        kubeconfig=Path("/repo/.tmp/kubeconfigs/local.yaml"),
    )


def test_start_builds_argv_via_kubectl_adapter_and_logs_per_target(tmp_path: Path) -> None:
    popens: list[_FakePopen] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        popen = _FakePopen(argv, **kwargs)
        popens.append(popen)
        return popen

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()

    supervisor.start(spec.targets[0], spec)

    assert popens[0].argv == [
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
    assert (tmp_path / "openlakeforge-local-trino-port-forward.log").exists()


def test_run_starts_every_target_and_stops_all_on_normal_exit(tmp_path: Path) -> None:
    popens: list[_FakePopen] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        popen = _FakePopen(argv, **kwargs)
        popens.append(popen)
        return popen

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()

    supervisor.run(spec, wait=lambda: None)

    assert len(popens) == 2
    assert all(p.terminated for p in popens)


def test_run_stops_all_children_when_the_wait_callable_raises(tmp_path: Path) -> None:
    popens: list[_FakePopen] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        popen = _FakePopen(argv, **kwargs)
        popens.append(popen)
        return popen

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()

    def _boom() -> None:
        raise RuntimeError("boom")

    import pytest

    with pytest.raises(RuntimeError):
        supervisor.run(spec, wait=_boom)

    assert all(p.terminated for p in popens)


def test_run_default_wait_raises_when_a_later_target_dies(tmp_path: Path) -> None:
    import pytest

    from olf.deployment.errors import DeploymentPreconditionError

    class _DiesAfterOnePoll(_FakePopen):
        """Simulates a healthy start followed by an early, unattended exit."""

        def __init__(self, argv, **kwargs) -> None:  # noqa: ANN001
            super().__init__(argv, **kwargs)
            self._polls = 0

        def poll(self):
            self._polls += 1
            if self._polls >= 2:
                self._returncode = 1
            return self._returncode

    popens: list[_FakePopen] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        # Only the second target ("polaris" - port 8181) is flaky.
        cls = _DiesAfterOnePoll if "8181:8181" in argv else _FakePopen
        popen = cls(argv, **kwargs)
        popens.append(popen)
        return popen

    sleeps: list[float] = []
    supervisor = PortForwardSupervisor(
        _kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen, sleep=sleeps.append
    )
    spec = _spec()

    with pytest.raises(DeploymentPreconditionError, match="polaris"):
        supervisor.run(spec)

    # The still-healthy target was cleaned up too, not just the dead one.
    assert popens[0].terminated
    assert popens[1]._returncode == 1
    assert sleeps  # the default wait actually polled at least once


def test_stop_all_is_idempotent(tmp_path: Path) -> None:
    def fake_popen(argv, **kwargs):  # noqa: ANN001
        return _FakePopen(argv, **kwargs)

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()
    supervisor.start(spec.targets[0], spec)

    supervisor.stop_all()
    supervisor.stop_all()  # must not raise on an already-stopped supervisor


def test_stop_all_kills_processes_that_do_not_terminate_in_time(tmp_path: Path) -> None:
    class _StubbornPopen(_FakePopen):
        def wait(self, timeout=None):  # noqa: ANN001
            raise subprocess.TimeoutExpired(cmd="kubectl", timeout=timeout or 0)

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        return _StubbornPopen(argv, **kwargs)

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()
    process = supervisor.start(spec.targets[0], spec)

    supervisor.stop_all()

    assert process.killed


def test_start_layers_env_overrides_over_the_ambient_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `env=dict(overrides)` would replace the whole child environment,
    dropping `PATH`/`HOME` and any cloud credential-plugin config (e.g.
    `AWS_PROFILE`, Azure's config dir) that EKS/AKS exec-based kubeconfigs
    need in order to authenticate - see the CloudProvider.forward() caller.
    """
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    popens: list[_FakePopen] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        popen = _FakePopen(argv, **kwargs)
        popen.env = kwargs.get("env")
        popens.append(popen)
        return popen

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()

    supervisor.start(spec.targets[0], spec, env={"KUBECONFIG": "/repo/.tmp/kubeconfigs/aws.yaml"})

    child_env = popens[0].env
    assert child_env["AWS_PROFILE"] == "ambient-profile"
    assert child_env["PATH"] == os.environ["PATH"]
    assert child_env["KUBECONFIG"] == "/repo/.tmp/kubeconfigs/aws.yaml"


def test_start_passes_none_env_when_no_overrides_given(tmp_path: Path) -> None:
    popens: list[_FakePopen] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        popen = _FakePopen(argv, **kwargs)
        popen.env = kwargs.get("env")
        popens.append(popen)
        return popen

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()

    supervisor.start(spec.targets[0], spec)

    assert popens[0].env is None
