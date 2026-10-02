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
import socket
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime

from olf import log
from olf.tooling.resolver import build_resolver


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
    result = subprocess.run(
        command,
        capture_output=capture,
        text=True,
        check=False,
    )
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
) -> str:
    args = ["get", "secret", secret_name, "-n", namespace, "-o", f"jsonpath={{.data.{key}}}"]
    raw = _kubectl(
        args,
        capture=True,
        kube_context=kube_context,
    )
    return base64.b64decode(raw).decode("utf-8")


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


def _wait_for_port_forward(process: subprocess.Popen[bytes], port: int, log_path: str) -> None:
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
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_port_forward(process, port, log_path)
        yield port
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()


def discover_dagster_user_deployments(namespace: str) -> list[str]:
    """Dagster code-location deployments are chart-generated; discover them."""
    raw = _kubectl(
        [
            "get",
            "deployments",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=dagster-user-deployments,app.kubernetes.io/instance=dagster",
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        ],
        capture=True,
    )
    return raw.splitlines()


# --- Pure image-patch builders (unit tested) -------------------------------


def dagster_yaml_with_job_image(dagster_yaml: str, image: str) -> str:
    """Rewrite the run launcher job_image line in a dagster.yaml document."""
    lines = dagster_yaml.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("job_image:"):
            indent = line[: len(line) - len(stripped)]
            lines[index] = f'{indent}job_image: "{image}"'
            break
    else:
        raise KubectlError("dagster-instance ConfigMap does not contain run launcher job_image.")
    return "\n".join(lines) + ("\n" if dagster_yaml.endswith("\n") else "")


def deployment_container_patch(containers: list[dict], image: str, *, restarted_at: str | None = None) -> dict:
    """Build a strategic-merge patch that bumps regular container images.

    Keeps DAGSTER_CURRENT_IMAGE in sync where already set: Dagster's
    K8sRunLauncher derives run-pod images from the code location's
    DAGSTER_CURRENT_IMAGE, which overrides run_launcher.job_image. Bumping only
    the container image leaves run pods launching on the previous image.
    """
    if not containers:
        raise KubectlError("deployment has no regular containers to patch.")

    def one(container: dict) -> dict:
        entry = {"name": container["name"], "image": image}
        env = container.get("env") or []
        if any(var.get("name") == "DAGSTER_CURRENT_IMAGE" for var in env):
            entry["env"] = [{"name": "DAGSTER_CURRENT_IMAGE", "value": image}]
        return entry

    template: dict = {"spec": {"containers": [one(c) for c in containers]}}
    if restarted_at:
        template["metadata"] = {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}
    return {"spec": {"template": template}}


def cronjob_container_patch(containers: list[dict], image: str) -> dict:
    if not containers:
        raise KubectlError("cronjob has no regular containers to patch.")
    return {
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {"spec": {"containers": [{"name": c["name"], "image": image} for c in containers]}}
                }
            }
        }
    }


# --- Cluster-mutating image operations -------------------------------------


def _get_json(kind: str, name: str, namespace: str) -> dict:
    raw = _kubectl(["get", kind, name, "-n", namespace, "-o", "json"], capture=True)
    return json.loads(raw)


def patch_dagster_instance_configmap(image: str, namespace: str) -> None:
    configmap = "dagster-instance"
    if not resource_exists("configmap", configmap, namespace):
        return
    log.step(f"Updating Dagster run launcher image to {image}...")
    payload = _get_json("configmap", configmap, namespace)
    dagster_yaml = (payload.get("data") or {}).get("dagster.yaml")
    if dagster_yaml is None:
        return
    updated = dagster_yaml_with_job_image(dagster_yaml, image)
    patch = json.dumps({"data": {"dagster.yaml": updated}})
    _kubectl(["patch", "configmap", configmap, "-n", namespace, "--type", "merge", "-p", patch])


def patch_deployment_image_if_exists(
    deployment: str,
    image: str,
    namespace: str,
    *,
    restarted_at: str | None = None,
) -> bool:
    if not resource_exists("deployment", deployment, namespace):
        return False
    log.step(f"Updating {deployment} image to {image}...")
    payload = _get_json("deployment", deployment, namespace)
    containers = payload["spec"]["template"]["spec"].get("containers", [])
    patch = deployment_container_patch(containers, image, restarted_at=restarted_at)
    _kubectl(["patch", "deployment", deployment, "-n", namespace, "--type", "strategic", "-p", json.dumps(patch)])
    return True


def patch_cronjob_image_if_exists(cronjob: str, image: str, namespace: str) -> None:
    if not resource_exists("cronjob", cronjob, namespace):
        return
    log.step(f"Updating {cronjob} image to {image}...")
    payload = _get_json("cronjob", cronjob, namespace)
    containers = payload["spec"]["jobTemplate"]["spec"]["template"]["spec"].get("containers", [])
    patch = cronjob_container_patch(containers, image)
    _kubectl(["patch", "cronjob", cronjob, "-n", namespace, "--type", "strategic", "-p", json.dumps(patch)])


_DAGSTER_CORE_DEPLOYMENTS = (
    "dagster-dagster-webserver",
    "dagster-dagster-daemon",
    "dagster-webserver",
    "dagster-daemon",
)


def _wait_for_rollout_with_diagnostics(deployment: str, namespace: str, timeout: str) -> None:
    try:
        _kubectl(
            [
                "rollout",
                "status",
                f"deployment/{deployment}",
                "-n",
                namespace,
                f"--timeout={timeout}",
            ]
        )
    except KubectlError:
        context = os.environ.get("KUBE_CONTEXT", "<current-context>")
        log.error(f"Dagster rollout failed for {deployment} in context {context}; showing pods and recent events.")
        _kubectl(["get", "pods", "-n", namespace, "-o", "wide"], check=False)
        _kubectl(
            ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"],
            check=False,
        )
        raise


def set_project_code_image(image: str, namespace: str, *, rollout_timeout: str = "600s") -> None:
    """Point Dagster at an image and perform exactly one rollout per deployment."""
    patch_dagster_instance_configmap(image, namespace)
    deployments = [*_DAGSTER_CORE_DEPLOYMENTS, *discover_dagster_user_deployments(namespace)]
    restarted_at = datetime.now(UTC).isoformat()
    updated = [
        deployment
        for deployment in deployments
        if patch_deployment_image_if_exists(
            deployment,
            image,
            namespace,
            restarted_at=restarted_at,
        )
    ]
    patch_cronjob_image_if_exists("openlakeforge-k8s-log-archive", image, namespace)
    for deployment in updated:
        log.step(f"Waiting for {deployment} rollout in {os.environ.get('KUBE_CONTEXT', 'current context')}...")
        _wait_for_rollout_with_diagnostics(deployment, namespace, rollout_timeout)


def wait_for_rollout(kind_name: str, namespace: str, timeout: str = "300s") -> None:
    _kubectl(["rollout", "status", kind_name, "-n", namespace, f"--timeout={timeout}"])


def http_wait(url: str, *, attempts: int = 60, delay: float = 2.0) -> bool:
    """Poll an HTTP endpoint until it answers 2xx/3xx/4xx (i.e. reachable)."""
    import urllib.error
    import urllib.request

    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310 - localhost port-forward
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(delay)
    return False
