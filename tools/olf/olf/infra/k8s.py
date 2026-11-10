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
from collections.abc import Callable, Collection, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olf.common.deployment.errors import CommandExecutionError
from olf.common.tooling.process import STDOUT, Popen, ProcessRunner, TimeoutExpired, unsets_of
from olf.common.tooling.resolver import build_resolver
from olf.common.tooling.toolkit import Toolkit

if TYPE_CHECKING:
    from pathlib import Path

    import requests

_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


class KubectlError(RuntimeError):
    pass


REPLICAS_ANNOTATION = "openlakeforge.io/restore-replicas"
PORT_FORWARD_READY_TIMEOUT_SECONDS = 30.0
PORT_FORWARD_READY_POLL_SECONDS = 0.1


def kubectl_command(
    args: list[str], *, kube_context: str | None = None, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Build a kubectl command pinned to the declared deployment context.

    `environ` (default `os.environ`) supplies `KUBE_CONTEXT` when no context is given.
    """
    context = kube_context or (os.environ if environ is None else environ).get("KUBE_CONTEXT")
    if not context:
        raise KubectlError("KUBE_CONTEXT is required for Kubernetes operations.")
    command = ["kubectl", "--context", context]
    command.extend(args)
    return command


def _kubectl_executable(environ: Mapping[str, str] | None = None) -> str:
    """The resolved (possibly managed, #127) kubectl executable path.

    A separate function so tests can substitute a literal `"kubectl"`
    without exercising real toolchain resolution.
    """
    return str(build_resolver(environ=environ).resolve("kubectl"))


def _resolved_kubectl_argv(
    args: list[str], *, kube_context: str | None = None, environ: Mapping[str, str] | None = None
) -> list[str]:
    """`kubectl_command`'s argv with `kubectl` substituted for its resolved
    executable path, used at every actual subprocess execution site.
    `kubectl_command` itself stays a pure argv builder so callers/tests that
    only need the command shape never pay for resolution.
    """
    command = kubectl_command(args, kube_context=kube_context, environ=environ)
    # Called bare when no environ is given: tests substitute a zero-argument stand-in.
    command[0] = _kubectl_executable() if environ is None else _kubectl_executable(environ)
    return command


def _kubectl(
    args: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    kube_context: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    command = _resolved_kubectl_argv(args, kube_context=kube_context, environ=environ)
    # Uncaptured output still reaches the terminal: ProcessRunner tees it when streaming.
    result = ProcessRunner().run(command, check=False, stream_output=not capture, env=environ, unset=unsets_of(environ))
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise KubectlError(f"{' '.join(command)} failed: {detail}")
    return result.stdout if capture else ""


def resource_exists(kind: str, name: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> bool:
    return bool(
        _kubectl(
            ["get", kind, name, "-n", namespace],
            capture=True,
            check=False,
            environ=environ,
        )
    )


def secret_value(
    secret_name: str,
    key: str,
    namespace: str,
    *,
    kube_context: str | None = None,
    missing_ok: bool = False,
    environ: Mapping[str, str] | None = None,
) -> str:
    args = ["get", "secret", secret_name, "-n", namespace, "-o", f"jsonpath={{.data.{key}}}"]
    if missing_ok:
        args.append("--ignore-not-found")
    raw = _kubectl(
        args,
        capture=True,
        kube_context=kube_context,
        environ=environ,
    )
    return base64.b64decode(raw).decode("utf-8")


def secret_data(name: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The Secret's base64-encoded data, or None when it does not exist. Captured, so never printed."""
    raw = _kubectl(
        ["get", "secret", name, "-n", namespace, "-o", "json", "--ignore-not-found"], capture=True, environ=environ
    )
    return (json.loads(raw).get("data") or {}) if raw else None


def secret_with_type(
    name: str, namespace: str, *, environ: Mapping[str, str] | None = None
) -> tuple[str, dict[str, str]] | None:
    """The Secret's type and base64-encoded data, or None when it does not exist. Captured, so never printed."""
    raw = _kubectl(
        ["get", "secret", name, "-n", namespace, "-o", "json", "--ignore-not-found"], capture=True, environ=environ
    )
    if not raw:
        return None
    secret = json.loads(raw)
    return secret.get("type", "Opaque"), secret.get("data") or {}


def delete_secret(name: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    _kubectl(["delete", "secret", name, "-n", namespace, "--ignore-not-found"], capture=True, environ=environ)


def configmap_data(name: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The ConfigMap's data, or None when it does not exist."""
    raw = _kubectl(
        ["get", "configmap", name, "-n", namespace, "-o", "json", "--ignore-not-found"], capture=True, environ=environ
    )
    return (json.loads(raw).get("data") or {}) if raw else None


def delete_configmap(name: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    _kubectl(["delete", "configmap", name, "-n", namespace, "--ignore-not-found"], capture=True, environ=environ)


def exec_succeeds(
    deployment: str, namespace: str, argv: list[str], *, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether `argv` exits 0 inside the deployment's pod."""
    command = _resolved_kubectl_argv(["exec", f"deploy/{deployment}", "-n", namespace, "--", *argv], environ=environ)
    return ProcessRunner().run(command, check=False, env=environ, unset=unsets_of(environ)).ok


def exec_to_file(
    pod: str,
    namespace: str,
    argv: list[str],
    destination: Path,
    *,
    container: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run `argv` in the pod with its stdout streamed to `destination`, so a dump never sits in memory."""
    target = ["-c", container] if container else []
    command = _resolved_kubectl_argv(["exec", pod, "-n", namespace, *target, "--", *argv], environ=environ)
    with destination.open("wb") as out, tempfile.TemporaryFile() as err:
        process = ProcessRunner().popen(command, stdout=out, stderr=err, env=environ, unset=unsets_of(environ))
        if process.wait() != 0:
            err.seek(0)
            detail = err.read()[-2000:].decode(errors="replace").strip()
            raise KubectlError(f"kubectl exec {pod} -n {namespace} -- {argv[0]} failed: {detail}")


def deployment_replicas(deployment: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> int:
    raw = _kubectl(
        ["get", f"deployment/{deployment}", "-n", namespace, "-o", "jsonpath={.spec.replicas}"],
        capture=True,
        environ=environ,
    )
    return int(raw or 0)


def scale_deployment(
    deployment: str,
    namespace: str,
    replicas: int,
    *,
    remember: int | None = None,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = 120.0,
    poll_seconds: float = 2.0,
) -> None:
    """Scale the Deployment, then wait: for its pods to be gone at zero, else for the rollout.

    `rollout status` returns while a terminating pod is still running, so zero is waited for on the pods.
    With `remember`, a caller that wants to rerun after a failure keeps the count on the Deployment while it is
    at 0 (see `deployment_replica_counts`); a scale-up with `remember=0` removes it.
    """
    labels = _kubectl(
        ["get", f"deployment/{deployment}", "-n", namespace, "-o", "jsonpath={.spec.selector.matchLabels}"],
        capture=True,
        environ=environ,
    )
    selector = ",".join(f"{key}={value}" for key, value in json.loads(labels).items())
    if remember:  # only a scale-down remembers a count
        annotation = f"{REPLICAS_ANNOTATION}={remember}"
        _kubectl(["annotate", f"deployment/{deployment}", "-n", namespace, annotation, "--overwrite"], environ=environ)
    _kubectl(["scale", f"deployment/{deployment}", f"--replicas={replicas}", "-n", namespace], environ=environ)
    if replicas:
        wait_for_rollout(f"deployment/{deployment}", namespace, environ=environ)
        if remember is not None:
            forget = f"{REPLICAS_ANNOTATION}-"
            _kubectl(["annotate", f"deployment/{deployment}", "-n", namespace, forget], environ=environ)
        return
    deadline = time.monotonic() + timeout_seconds
    # A finished pod never goes away and can carry the Deployment's labels: platform-api's superset-users Job pod
    # does, which made a restore wait out its timeout for a Deployment that had already stopped. A terminating
    # pod is still Running, so it is still waited for.
    running = "status.phase!=Succeeded,status.phase!=Failed"
    while _kubectl(
        ["get", "pods", "-l", selector, "--field-selector", running, "-n", namespace, "-o", "name"],
        capture=True,
        environ=environ,
    ).strip():
        if time.monotonic() >= deadline:
            raise KubectlError(
                f"the pods of deployment/{deployment} in {namespace} did not stop within {timeout_seconds}s"
            )
        time.sleep(poll_seconds)


def restart_deployment(deployment: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    _kubectl(["rollout", "restart", f"deployment/{deployment}", "-n", namespace], environ=environ)
    wait_for_rollout(f"deployment/{deployment}", namespace, environ=environ)


def delete_jobs(selector: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Delete the Jobs the label selector matches (their pods go with them); none is not an error."""
    _kubectl(["delete", "job", "-l", selector, "-n", namespace, "--ignore-not-found"], capture=True, environ=environ)


def delete_cronjobs(selector: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Delete the CronJobs the label selector matches (the Jobs they started go with them); none is not an error."""
    _kubectl(
        ["delete", "cronjob", "-l", selector, "-n", namespace, "--ignore-not-found"], capture=True, environ=environ
    )


def certificates_issued_by(
    cluster_issuer: str, *, environ: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """Every cert-manager Certificate whose issuerRef is the ClusterIssuer, as (namespace, name, secret name)."""
    raw = _kubectl(["get", "certificates", "-A", "-o", "json"], capture=True, environ=environ)
    return [
        (item["metadata"]["namespace"], item["metadata"]["name"], item["spec"]["secretName"])
        for item in json.loads(raw or "{}").get("items", [])
        if item["spec"].get("issuerRef", {}).get("name") == cluster_issuer
        and item["spec"]["issuerRef"].get("kind") == "ClusterIssuer"
    ]


def wait_for_certificate(
    name: str,
    namespace: str,
    secret_name: str,
    *,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = 300.0,
    poll_seconds: float = 2.0,
) -> None:
    """Wait until cert-manager has written the Certificate's Secret, then until the Certificate is Ready.

    The Secret comes first: a Certificate whose Secret was deleted keeps reporting Ready until cert-manager notices.
    """
    deadline = time.monotonic() + timeout_seconds
    while secret_data(secret_name, namespace, environ=environ) is None:
        if time.monotonic() >= deadline:
            raise KubectlError(
                f"cert-manager did not re-issue Secret {namespace}/{secret_name} within {timeout_seconds}s"
            )
        time.sleep(poll_seconds)
    _kubectl(
        ["wait", "--for=condition=Ready", f"certificate/{name}", "-n", namespace, f"--timeout={int(timeout_seconds)}s"],
        environ=environ,
    )


def workloads_reading(
    namespaces: Collection[str],
    *,
    configmaps: Collection[str],
    secrets: Collection[tuple[str, str]],
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """The running Deployments and StatefulSets that mount one of the ConfigMaps (by name) or Secrets (by namespace
    and name), as (kind, name, namespace). They read what they mount at start, so a change needs a restart.

    ponytail: volumes only; a Secret read through `env` or `envFrom` is not found. Every consumer of the CA mounts it.
    """
    found = []
    for namespace in dict.fromkeys(namespaces):
        raw = _kubectl(
            ["get", "deployments,statefulsets", "-n", namespace, "-o", "json"], capture=True, environ=environ
        )
        for item in json.loads(raw or "{}").get("items", []):
            if not item["spec"].get("replicas", 1):
                continue
            volumes = item["spec"]["template"]["spec"].get("volumes") or []
            sources = [
                source for volume in volumes for source in [volume, *(volume.get("projected") or {}).get("sources", [])]
            ]
            if any(
                (s.get("configMap") or {}).get("name") in configmaps
                or (namespace, (s.get("secret") or {}).get("secretName") or (s.get("secret") or {}).get("name"))
                in secrets
                for s in sources
            ):
                found.append((item["kind"], item["metadata"]["name"], namespace))
    return found


def restart_workload(kind: str, name: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Roll the Deployment or StatefulSet and wait for the rollout."""
    _kubectl(["rollout", "restart", f"{kind.lower()}/{name}", "-n", namespace], environ=environ)
    wait_for_rollout(f"{kind.lower()}/{name}", namespace, environ=environ)


def exec_in_pod(
    pod: str,
    namespace: str,
    argv: list[str],
    *,
    container: str,
    stdin_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run `argv` in the pod's container, with the file as stdin; stderr is the error when it exits non-zero."""
    command = _resolved_kubectl_argv(
        ["exec", "-i", pod, "-n", namespace, "-c", container, "--", *argv], environ=environ
    )
    result = ProcessRunner().run(command, check=False, env=environ, unset=unsets_of(environ), stdin_path=stdin_path)
    if result.returncode != 0:
        raise KubectlError(f"{argv[0]} in {namespace}/{pod} failed: {(result.stderr or '').strip()[-2000:]}")


def deployment_replica_counts(namespace: str, *, environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """Each Deployment's replica count. One that `scale_deployment` left at 0 reports the count it remembered."""
    raw = _kubectl(["get", "deployment", "-n", namespace, "-o", "json"], capture=True, environ=environ)
    counts = {}
    for item in json.loads(raw or "{}").get("items", []):
        replicas = item["spec"].get("replicas", 1)
        remembered = (item["metadata"].get("annotations") or {}).get(REPLICAS_ANNOTATION)
        counts[item["metadata"]["name"]] = int(remembered) if replicas == 0 and remembered else replicas
    return counts


CRONJOB_SUSPENDED_ANNOTATION = "openlakeforge.io/suspended-before-restore"


def _cronjobs(namespace: str, environ: Mapping[str, str] | None) -> list[dict[str, Any]]:
    raw = _kubectl(["get", "cronjob", "-n", namespace, "-o", "json"], capture=True, environ=environ)
    return json.loads(raw or "{}").get("items", [])


def _patch_cronjob(name: str, namespace: str, patch: dict[str, Any], environ: Mapping[str, str] | None) -> None:
    _kubectl(["patch", "cronjob", name, "-n", namespace, "--type=merge", "-p", json.dumps(patch)], environ=environ)


def suspend_cronjobs(
    namespace: str,
    *,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = 120.0,
    poll_seconds: float = 2.0,
) -> None:
    """Suspend every CronJob, then wait until none has a Job running.

    Each CronJob keeps whether it was suspended before in an annotation that `resume_cronjobs` reads, and a CronJob
    that already has it keeps the first answer, so a rerun after a failure still restores the original state.
    """
    for item in _cronjobs(namespace, environ):
        patch: dict[str, Any] = {"spec": {"suspend": True}}
        if CRONJOB_SUSPENDED_ANNOTATION not in (item["metadata"].get("annotations") or {}):
            was = str(bool(item["spec"].get("suspend"))).lower()
            patch["metadata"] = {"annotations": {CRONJOB_SUSPENDED_ANNOTATION: was}}
        _patch_cronjob(item["metadata"]["name"], namespace, patch, environ)
    deadline = time.monotonic() + timeout_seconds
    while busy := [
        item["metadata"]["name"] for item in _cronjobs(namespace, environ) if item.get("status", {}).get("active")
    ]:
        if time.monotonic() >= deadline:
            raise KubectlError(f"the CronJobs {busy} in {namespace} still have Jobs running after {timeout_seconds}s")
        time.sleep(poll_seconds)


def resume_cronjobs(namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Give each CronJob `suspend_cronjobs` suspended the suspension it had before."""
    for item in _cronjobs(namespace, environ):
        was = (item["metadata"].get("annotations") or {}).get(CRONJOB_SUSPENDED_ANNOTATION)
        if was is not None:
            patch = {
                "spec": {"suspend": was == "true"},
                "metadata": {"annotations": {CRONJOB_SUSPENDED_ANNOTATION: None}},
            }
            _patch_cronjob(item["metadata"]["name"], namespace, patch, environ)


def annotate_pod_templates(
    selector: str, namespace: str, key: str, value: str, *, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Set a pod-template annotation on every Deployment the selector matches, returning those it changed.

    A changed pod template rolls the Deployment out; one whose annotation already holds `value` is left alone.
    """
    raw = _kubectl(["get", "deployment", "-l", selector, "-n", namespace, "-o", "json"], capture=True, environ=environ)
    changed = []
    for item in json.loads(raw or "{}").get("items", []):
        annotations = item["spec"]["template"]["metadata"].get("annotations") or {}
        if annotations.get(key) != value:
            changed.append(f"deployment/{item['metadata']['name']}")
    patch = json.dumps({"spec": {"template": {"metadata": {"annotations": {key: value}}}}})
    for deployment in changed:
        _kubectl(["patch", deployment, "-n", namespace, "--type=merge", "-p", patch], environ=environ)
    return changed


def wait_for_deployments(selector: str, namespace: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Wait for the rollout of every Deployment the label selector matches."""
    _kubectl(["rollout", "status", "deployment", "-l", selector, "-n", namespace, "--timeout=300s"], environ=environ)


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


# A re-established forward that dies again each time is a cluster problem, not a blip.
PORT_FORWARD_MAX_REVIVALS = 3


class PortForwardHandle:
    """A running `kubectl port-forward` that can be re-established when it dies.

    `kubectl port-forward` exits, or stops accepting, when its stream to the pod
    breaks (an idle timeout, a kubelet/apiserver hiccup) while the pod stays
    Ready, so a long e2e step that reuses one forward fails with "connection
    refused" against a healthy service. `revive` replaces the dead process on
    the same local port, at most `PORT_FORWARD_MAX_REVIVALS` times.
    """

    def __init__(self, launch: Callable[[], Popen[bytes]], port: int, log_path: str, *, label: str) -> None:
        self.port = port
        self._launch = launch
        self._log_path = log_path
        self._label = label
        self._revivals = 0
        self._process = launch()

    def alive(self) -> bool:
        if self._process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=PORT_FORWARD_READY_POLL_SECONDS):
                return True
        except OSError:
            return False

    def wait_ready(self) -> None:
        _wait_for_port_forward(self._process, self.port, self._log_path)

    def revive(self) -> None:
        """Re-establish the forward if it is dead; a no-op while it still accepts connections."""
        if self.alive():
            return
        if self._revivals >= PORT_FORWARD_MAX_REVIVALS:
            raise _port_forward_failure(
                self._log_path,
                f"port-forward to {self._label} on localhost:{self.port} died again after "
                f"{PORT_FORWARD_MAX_REVIVALS} re-establishments.",
            )
        self._revivals += 1
        self._stop()
        self._process = self._launch()
        self.wait_ready()

    def close(self) -> None:
        self._stop()

    def _stop(self) -> None:
        self._process.terminate()
        with contextlib.suppress(TimeoutExpired):
            self._process.wait(timeout=10)
        if self._process.poll() is None:
            self._process.kill()


@contextlib.contextmanager
def supervised_port_forward(
    service: str,
    remote_port: int,
    namespace: str,
    *,
    local_port: int | None = None,
    log_path: str = "/tmp/openlakeforge-port-forward.log",
    kube_context: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Iterator[PortForwardHandle]:
    """Run `kubectl port-forward` for the block; the handle re-establishes it if it dies."""
    port = local_port or _free_local_port()
    command = _resolved_kubectl_argv(
        ["port-forward", f"svc/{service}", f"{port}:{remote_port}", "-n", namespace],
        kube_context=kube_context,
        environ=environ,
    )

    launched = False

    def launch() -> Popen[bytes]:
        # Relaunches append: the dead forward's last lines are the diagnostic a give-up error needs to show.
        nonlocal launched
        with open(log_path, "a" if launched else "w", encoding="utf-8") as log_file:
            if launched:
                log_file.write("--- port-forward relaunched ---\n")
            launched = True
            return ProcessRunner().popen(command, stdout=log_file, stderr=STDOUT, env=environ, unset=unsets_of(environ))

    handle = PortForwardHandle(launch, port, log_path, label=f"{namespace}/{service}")
    try:
        handle.wait_ready()
        yield handle
    finally:
        handle.close()


@contextlib.contextmanager
def port_forward(
    service: str,
    remote_port: int,
    namespace: str,
    *,
    local_port: int | None = None,
    log_path: str = "/tmp/openlakeforge-port-forward.log",
    kube_context: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Iterator[int]:
    """Run `kubectl port-forward` for the block, yielding the local port.

    The yielded localhost port is accepting connections. Callers still own the
    service-specific health probe (Polaris OAuth, OpenMetadata JWKS, S3 bucket).
    """
    with supervised_port_forward(
        service,
        remote_port,
        namespace,
        local_port=local_port,
        log_path=log_path,
        kube_context=kube_context,
        environ=environ,
    ) as handle:
        yield handle.port


def wait_for_rollout(
    kind_name: str, namespace: str, timeout: str = "300s", *, environ: Mapping[str, str] | None = None
) -> None:
    _kubectl(["rollout", "status", kind_name, "-n", namespace, f"--timeout={timeout}"], environ=environ)


def http_wait(url: str, *, attempts: int = 60, delay: float = 2.0, session: requests.Session | None = None) -> bool:
    """Poll an HTTP endpoint until it answers at all (any status means reachable).

    Through `requests`, so an ingress URL is verified against the bundle the
    `session` of its `olf.infra.access.Route` selects.
    """
    import requests

    client = session or requests
    for _ in range(attempts):
        try:
            client.get(url, timeout=2)
            return True
        except requests.RequestException:
            time.sleep(delay)
    return False


def apply_manifest(manifest: dict, *, environ: Mapping[str, str] | None = None) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
        json.dump(manifest, handle)
        handle.flush()
        _kubectl(["apply", "-f", handle.name], environ=environ)


def write_secret(
    name: str,
    namespace: str,
    data: dict[str, str],
    *,
    overwrite: bool = True,
    secret_type: str = "Opaque",
    environ: Mapping[str, str] | None = None,
) -> None:
    """Store `data` as a Secret (Opaque unless `secret_type` says); with `overwrite=False`, fail if it exists."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": secret_type,
        "stringData": data,
    }
    context = (os.environ if environ is None else environ).get("KUBE_CONTEXT")
    if not context:
        raise KubectlError("KUBE_CONTEXT is required for Kubernetes operations.")
    # Called bare when no environ is given: tests substitute a zero-argument stand-in.
    kubectl = (Toolkit.default() if environ is None else Toolkit.default(environ=environ)).kubectl
    write = kubectl.apply_from_manifest if overwrite else kubectl.create_from_manifest
    try:
        write(json.dumps(manifest), context=context, env=environ)
    except CommandExecutionError as exc:
        raise KubectlError(str(exc)) from exc


def run_job(
    job: dict, *, timeout_seconds: int, poll_seconds: float = 5.0, environ: Mapping[str, str] | None = None
) -> None:
    """Replace the named Job (its spec is immutable) and wait for Complete or Failed.

    `kubectl wait` takes one condition, so a failed Job would block until the
    deadline; the status is polled instead. The log tail is masked of JWTs.
    """
    name, namespace = job["metadata"]["name"], job["metadata"]["namespace"]
    _kubectl(["delete", "job", name, "-n", namespace, "--ignore-not-found"], environ=environ)
    with tempfile.NamedTemporaryFile("w", suffix=".json") as manifest:
        json.dump(job, manifest)
        manifest.flush()
        _kubectl(["create", "-f", manifest.name], environ=environ)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        conditions = _kubectl(
            ["get", f"job/{name}", "-n", namespace, "-o", "jsonpath={.status.conditions[*].type}"],
            capture=True,
            environ=environ,
        ).split()
        if "Complete" in conditions:
            return
        if "Failed" in conditions:
            tail = _kubectl(
                ["logs", f"job/{name}", "-n", namespace, "--tail=40"], capture=True, check=False, environ=environ
            )
            masked = _JWT.sub("***", tail)
            raise KubectlError(f"Job {name} failed:\n{masked}")
        time.sleep(poll_seconds)
    raise KubectlError(f"Job {name} did not finish within {timeout_seconds}s")
