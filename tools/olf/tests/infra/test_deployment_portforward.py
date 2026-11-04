from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import ProcessRunner
from olf.common.tooling.resolver import PathExecutableResolver
from olf.infra.deployment.portforward import ForwardSpec, ForwardTarget, PortForwardSupervisor


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


    with pytest.raises(RuntimeError):
        supervisor.run(spec, wait=_boom)

    assert all(p.terminated for p in popens)


def test_run_default_wait_raises_when_a_later_target_dies(tmp_path: Path) -> None:

    from olf.common.deployment.errors import DeploymentPreconditionError

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


def test_start_passes_env_overrides_through_for_the_runner_to_layer(tmp_path: Path) -> None:
    """`ProcessRunner.popen` layers the overrides over the ambient environment
    (covered in test_process.py), so the supervisor hands them over unmerged.
    """
    popens: list[_FakePopen] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        popen = _FakePopen(argv, **kwargs)
        popen.env = kwargs.get("env")
        popens.append(popen)
        return popen

    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local", popen=fake_popen)
    spec = _spec()

    supervisor.start(spec.targets[0], spec, env={"KUBECONFIG": "/repo/.tmp/kubeconfigs/aws.yaml"})

    assert popens[0].env == {"KUBECONFIG": "/repo/.tmp/kubeconfigs/aws.yaml"}


def test_default_popen_is_the_process_runner(tmp_path: Path) -> None:
    supervisor = PortForwardSupervisor(_kubectl(), log_prefix=tmp_path / "openlakeforge-local")

    assert supervisor._popen.__func__ is ProcessRunner.popen  # type: ignore[attr-defined]  # bound method of a runner


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
