"""Kubernetes helpers that shell out to kubectl.

Shell scripts stay the orchestrators for terraform/kubectl/helm/docker, but the
image-bookkeeping and port-forward logic used to be copy-pasted (as Python
heredocs) across the Azure and AWS artifact deploy scripts. This module is the
single implementation. The JSON-shaping helpers are kept pure so they can be
unit tested without a cluster.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import socket
import tempfile
import time
from collections.abc import Iterator

from olf.common.deployment.errors import CommandExecutionError
from olf.common.tooling.process import STDOUT, Popen, ProcessRunner, TimeoutExpired
from olf.common.tooling.resolver import build_resolver
from olf.common.tooling.toolkit import Toolkit

_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


class KubectlError(RuntimeError):
    pass


PORT_FORWARD_READY_TIMEOUT_SECONDS = 30.0
PORT_FORWARD_READY_POLL_SECONDS = 0.1


def kubectl_command(args: list[str], *, kube_context: str | None = None) -> list[str]:
    """Build a kubectl command pinned to the declared deployment context."""
    context = kube_context or os.environ.get("KUBE_CONTEXT")
    if not context:
        raise KubectlError("KUBE_CONTEXT is required for Kubernetes operations.")
    command = ["kubectl", "--context", context]
    command.extend(args)
    return command


def _kubectl_executable() -> str:
    """The resolved (possibly managed, #127) kubectl executable path.

    A separate function so tests can substitute a literal `"kubectl"`
    without exercising real toolchain resolution.
    """
    return str(build_resolver().resolve("kubectl"))


def _resolved_kubectl_argv(args: list[str], *, kube_context: str | None = None) -> list[str]:
    """`kubectl_command`'s argv with `kubectl` substituted for its resolved
    executable path, used at every actual subprocess execution site.
    `kubectl_command` itself stays a pure argv builder so callers/tests that
    only need the command shape never pay for resolution.
    """
    command = kubectl_command(args, kube_context=kube_context)
    command[0] = _kubectl_executable()
    return command


def _kubectl(
    args: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    kube_context: str | None = None,
) -> str:
    command = _resolved_kubectl_argv(args, kube_context=kube_context)
    # Uncaptured output still reaches the terminal: ProcessRunner tees it when streaming.
    result = ProcessRunner().run(command, check=False, stream_output=not capture)
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise KubectlError(f"{' '.join(command)} failed: {detail}")
    return result.stdout if capture else ""


def resource_exists(kind: str, name: str, namespace: str) -> bool:
    return bool(
        _kubectl(
            ["get", kind, name, "-n", namespace],
            capture=True,
            check=False,
        )
    )


def secret_value(
    secret_name: str,
    key: str,
    namespace: str,
    *,
    kube_context: str | None = None,
    missing_ok: bool = False,
) -> str:
    args = ["get", "secret", secret_name, "-n", namespace, "-o", f"jsonpath={{.data.{key}}}"]
    if missing_ok:
        args.append("--ignore-not-found")
    raw = _kubectl(
        args,
        capture=True,
        kube_context=kube_context,
    )
    return base64.b64decode(raw).decode("utf-8")


def configmap_data(name: str, namespace: str) -> dict[str, str] | None:
    """The ConfigMap's data, or None when it does not exist."""
    raw = _kubectl(["get", "configmap", name, "-n", namespace, "-o", "json", "--ignore-not-found"], capture=True)
    return (json.loads(raw).get("data") or {}) if raw else None


def exec_succeeds(deployment: str, namespace: str, argv: list[str]) -> bool:
    """Whether `argv` exits 0 inside the deployment's pod."""
    command = _resolved_kubectl_argv(["exec", f"deploy/{deployment}", "-n", namespace, "--", *argv])
    return ProcessRunner().run(command, check=False).ok


def restart_deployment(deployment: str, namespace: str) -> None:
    _kubectl(["rollout", "restart", f"deployment/{deployment}", "-n", namespace])
    wait_for_rollout(f"deployment/{deployment}", namespace)


def _free_local_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_forward_failure(log_path: str, detail: str) -> KubectlError:
    """Return a bounded, actionable error for a port-forward that did not start."""
    try:
        log_tail = open(log_path, encoding="utf-8").read()[-4000:].strip()
    except OSError:
        log_tail = ""
    if log_tail:
        detail = f"{detail}\nkubectl port-forward log:\n{log_tail}"
    return KubectlError(detail)


def _wait_for_port_forward(process: Popen[bytes], port: int, log_path: str) -> None:
    """Wait until kubectl has bound the local forwarding socket."""
    deadline = time.monotonic() + PORT_FORWARD_READY_TIMEOUT_SECONDS
    while True:
        if process.poll() is not None:
            raise _port_forward_failure(log_path, f"kubectl port-forward exited before localhost:{port} became ready.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=PORT_FORWARD_READY_POLL_SECONDS):
                return
        except OSError as err:
            if time.monotonic() >= deadline:
                raise _port_forward_failure(
                    log_path,
                    f"timed out waiting for kubectl port-forward to bind localhost:{port}.",
                ) from err
            time.sleep(PORT_FORWARD_READY_POLL_SECONDS)


@contextlib.contextmanager
def port_forward(
    service: str,
    remote_port: int,
    namespace: str,
    *,
    local_port: int | None = None,
    log_path: str = "/tmp/openlakeforge-port-forward.log",
    kube_context: str | None = None,
) -> Iterator[int]:
    """Run `kubectl port-forward` for the block, yielding the local port.

    The yielded localhost port is accepting connections. Callers still own the
    service-specific health probe (Polaris OAuth, OpenMetadata JWKS, S3 bucket).
    """
    port = local_port or _free_local_port()
    command = _resolved_kubectl_argv(
        [
            "port-forward",
            f"svc/{service}",
            f"{port}:{remote_port}",
            "-n",
            namespace,
        ],
        kube_context=kube_context,
    )
    with open(log_path, "w", encoding="utf-8") as log_file:
        process = ProcessRunner().popen(command, stdout=log_file, stderr=STDOUT)
    try:
        _wait_for_port_forward(process, port, log_path)
        yield port
    finally:
        process.terminate()
        with contextlib.suppress(TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()


def wait_for_rollout(kind_name: str, namespace: str, timeout: str = "300s") -> None:
    _kubectl(["rollout", "status", kind_name, "-n", namespace, f"--timeout={timeout}"])


def http_wait(url: str, *, attempts: int = 60, delay: float = 2.0) -> bool:
    """Poll an HTTP endpoint until it answers at all (any status means reachable).

    Through `requests`, so an ingress URL is verified against the bundle
    `olf.infra.access.service_url` selects.
    """
    import requests

    for _ in range(attempts):
        try:
            requests.get(url, timeout=2)
            return True
        except requests.RequestException:
            time.sleep(delay)
    return False



def apply_manifest(manifest: dict) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
        json.dump(manifest, handle)
        handle.flush()
        _kubectl(["apply", "-f", handle.name])


def write_secret(name: str, namespace: str, data: dict[str, str], *, overwrite: bool = True) -> None:
    """Store `data` as an Opaque Secret; with `overwrite=False`, fail if it already exists."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "stringData": data,
    }
    context = os.environ.get("KUBE_CONTEXT")
    if not context:
        raise KubectlError("KUBE_CONTEXT is required for Kubernetes operations.")
    kubectl = Toolkit.default().kubectl
    write = kubectl.apply_from_manifest if overwrite else kubectl.create_from_manifest
    try:
        write(json.dumps(manifest), context=context)
    except CommandExecutionError as exc:
        raise KubectlError(str(exc)) from exc


def run_job(job: dict, *, timeout_seconds: int, poll_seconds: float = 5.0) -> None:
    """Replace the named Job (its spec is immutable) and wait for Complete or Failed.

    `kubectl wait` takes one condition, so a failed Job would block until the
    deadline; the status is polled instead. The log tail is masked of JWTs.
    """
    name, namespace = job["metadata"]["name"], job["metadata"]["namespace"]
    _kubectl(["delete", "job", name, "-n", namespace, "--ignore-not-found"])
    with tempfile.NamedTemporaryFile("w", suffix=".json") as manifest:
        json.dump(job, manifest)
        manifest.flush()
        _kubectl(["create", "-f", manifest.name])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        conditions = _kubectl(
            ["get", f"job/{name}", "-n", namespace, "-o", "jsonpath={.status.conditions[*].type}"], capture=True
        ).split()
        if "Complete" in conditions:
            return
        if "Failed" in conditions:
            tail = _kubectl(["logs", f"job/{name}", "-n", namespace, "--tail=40"], capture=True, check=False)
            masked = _JWT.sub("***", tail)
            raise KubectlError(f"Job {name} failed:\n{masked}")
        time.sleep(poll_seconds)
    raise KubectlError(f"Job {name} did not finish within {timeout_seconds}s")
