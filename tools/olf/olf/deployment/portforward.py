"""Provider-neutral multi-target `kubectl port-forward` supervision.

Port of the Makefile's inline `local-forward` target: backgrounds one
`kubectl port-forward` child per service, and guarantees every child is
cleaned up on exit, exception, or `SIGINT`/`SIGTERM` - the Python equivalent
of the shell target's `trap ... INT TERM EXIT; wait`.

`Kubectl.port_forward` is a blocking, single-call adapter method (used for
one-shot waits elsewhere); it is the wrong shape for N concurrent,
long-lived, backgrounded forwards, so this module builds argv through
`Kubectl.port_forward_argv` and owns the `subprocess.Popen` lifecycle itself.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from olf.deployment.errors import DeploymentPreconditionError
from olf.tooling.kubectl import Kubectl

_TERMINATE_TIMEOUT_SECONDS = 10.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class ForwardTarget:
    label: str
    resource: str
    local_port: int
    remote_port: int
    namespace: str = ""
    """The namespace this one service lives in, when it is not the spec's.

    Shared platform services and stage-scoped services occupy different
    namespaces on a stage-aware cluster (ADR 0011), so one `ForwardSpec` can
    no longer carry a single namespace for every target."""

    @property
    def ports(self) -> str:
        return f"{self.local_port}:{self.remote_port}"


@dataclass(frozen=True)
class ForwardSpec:
    targets: tuple[ForwardTarget, ...]
    namespace: str
    context: str
    kubeconfig: Path
    banner: tuple[str, ...] = ()


class PortForwardSupervisor:
    def __init__(
        self,
        kubectl: Kubectl,
        *,
        log_prefix: Path,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._kubectl = kubectl
        self._log_prefix = log_prefix
        self._popen = popen
        self._sleep = sleep
        self._poll_interval_seconds = poll_interval_seconds
        self._processes: list[subprocess.Popen] = []
        self._log_files: list = []
        self._targets: list[ForwardTarget] = []
        self._log_paths: list[Path] = []

    def start(
        self, target: ForwardTarget, spec: ForwardSpec, *, env: Mapping[str, str] | None = None
    ) -> subprocess.Popen:
        """Launch one forwarding child.

        `env` is layered over the ambient process environment (matching
        `ProcessRunner.run`'s `{**os.environ, **command.env}` merge), not
        substituted for it - a bare `subprocess.Popen(..., env=dict(env))`
        would replace the whole environment, dropping `PATH`/`HOME` and any
        cloud credential-plugin config (`AWS_PROFILE`, Azure's config dir)
        that EKS/AKS exec-based kubeconfigs need to authenticate.
        """
        argv = self._kubectl_argv(target, spec)
        log_path = Path(f"{self._log_prefix}-{target.label}-port-forward.log")
        log_file = open(log_path, "w")  # noqa: SIM115 - lifetime tied to the child process
        popen_env = {**os.environ, **env} if env else None
        process = self._popen(argv, stdout=log_file, stderr=subprocess.STDOUT, env=popen_env)
        self._processes.append(process)
        self._log_files.append(log_file)
        self._targets.append(target)
        self._log_paths.append(log_path)
        return process

    def _wait_for_any_failure(self) -> None:
        """Block until a signal arrives, or raise as soon as any child exits.

        A healthy `kubectl port-forward` never exits on its own, so any exit
        - regardless of code - means that target stopped forwarding; block
        indefinitely on the first (and normally only) `wait()` target is not
        enough to catch a later target dying (e.g. a port already in use).
        """
        if not self._processes:
            return
        while True:
            for target, process, log_path in zip(self._targets, self._processes, self._log_paths, strict=True):
                code = process.poll()
                if code is not None:
                    raise DeploymentPreconditionError(
                        f"port-forward '{target.label}' ({target.resource}) exited unexpectedly "
                        f"(exit code {code}); see {log_path}"
                    )
            self._sleep(self._poll_interval_seconds)

    def _kubectl_argv(self, target: ForwardTarget, spec: ForwardSpec) -> list[str]:
        argv = self._kubectl.port_forward_argv(
            target.resource, [target.ports], namespace=target.namespace or spec.namespace, context=spec.context
        )
        return argv

    def stop_all(self) -> None:
        for process in self._processes:
            if process.poll() is not None:
                continue
            process.terminate()
        for process in self._processes:
            try:
                process.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
        for log_file in self._log_files:
            try:
                log_file.close()
            except OSError:
                pass
        self._processes = []
        self._log_files = []
        self._targets = []
        self._log_paths = []

    def run(
        self,
        spec: ForwardSpec,
        *,
        env: Mapping[str, str] | None = None,
        wait: Callable[[], None] | None = None,
    ) -> None:
        installed = [(sig, signal.getsignal(sig)) for sig in (signal.SIGINT, signal.SIGTERM)]

        def _handle_signal(signum, frame):  # noqa: ANN001, ARG001
            self.stop_all()
            raise SystemExit(0)

        try:
            for sig, _previous in installed:
                signal.signal(sig, _handle_signal)

            for target in spec.targets:
                self.start(target, spec, env=env)

            if wait is not None:
                wait()
            else:
                self._wait_for_any_failure()
        finally:
            self.stop_all()
            for sig, previous in installed:
                signal.signal(sig, previous)
