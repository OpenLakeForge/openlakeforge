"""Deploy gate: does this cluster's CNI enforce NetworkPolicy? (ADR 0017, slice 1a)

A deny-all namespace and an identical policy-free one each get a server and a
client. The policy-free pair is the positive control: without it a client that
cannot reach its server for any other reason would read as "blocked".
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import yaml

from olf import log
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.local.config import LocalDeploymentConfig

_DENY_NS = "olf-netpol-canary-deny"
_OPEN_NS = "olf-netpol-canary-open"
_PORT = 8080
# A dataplane programs a policy asynchronously, so a connection made right
# after the policy is created can still succeed on a cluster that enforces.
_ATTEMPTS = 6
_DELAY_SECONDS = 5.0


class PolicyNotEnforcedError(DeploymentPreconditionError):
    pass


def verdict(
    control: Callable[[], bool],
    denied: Callable[[], bool],
    *,
    attempts: int = _ATTEMPTS,
    delay: float = _DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Callables report whether a connection succeeded; raises unless control connects and denied does not."""
    if not control():
        raise PolicyNotEnforcedError(
            "NetworkPolicy canary is inconclusive: the client could not reach the server in the policy-free "
            "namespace, so a blocked connection in the deny-all namespace would prove nothing."
        )
    for attempt in range(attempts):
        if not denied():
            return
        if attempt < attempts - 1:
            sleep(delay)
    raise PolicyNotEnforcedError(
        "This cluster does not enforce NetworkPolicy: a client connected through a deny-all policy "
        f"({attempts} attempts). Default-deny policies would be inert (ADR 0017, Option B: install Calico)."
    )


def _manifest(namespace: str, image: str, *, deny_all: bool) -> str:
    def pod(name: str, command: list[str]) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"restartPolicy": "Never", "containers": [{"name": name, "image": image, "command": command}]},
        }

    docs: list[dict[str, Any]] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        pod("server", ["python3", "-m", "http.server", str(_PORT)]),
        pod("client", ["sleep", "3600"]),
    ]
    if deny_all:
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "deny-all", "namespace": namespace},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
            }
        )
    return yaml.safe_dump_all(docs)


def require_policy_enforcement(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    catalog = yaml.safe_load((config.context.paths.distribution_root / "release/component-catalog.yaml").read_text())
    image = catalog["components"]["images"]["k8s_bootstrap"]
    kubectl = tools.kubectl
    target = {"context": config.kube_context, "kubeconfig": config.paths.kubeconfig_path, "env": env}

    def probe(namespace: str) -> Callable[[], bool]:
        ip = kubectl.get("pod", name="server", namespace=namespace, output="jsonpath={.status.podIP}", **target)
        # The shell swallows curl's exit code so a blocked connection is not
        # confused with a failed exec, which must raise instead.
        script = f"curl -s -o /dev/null -m 3 http://{ip.stdout.strip()}:{_PORT}/ && echo connected || echo blocked"
        return lambda: "connected" in kubectl.exec("client", ["sh", "-c", script], namespace=namespace, **target).stdout

    log.step("Checking that the cluster enforces NetworkPolicy...")
    try:
        for namespace, deny_all in ((_OPEN_NS, False), (_DENY_NS, True)):
            kubectl.delete("namespace", namespace, **target)
            kubectl.create_from_manifest(_manifest(namespace, image, deny_all=deny_all), **target)
            for pod in ("server", "client"):
                kubectl.wait(f"pod/{pod}", for_condition="condition=Ready", namespace=namespace, timeout="180s", **target)
        verdict(probe(_OPEN_NS), probe(_DENY_NS))
    finally:
        for namespace in (_OPEN_NS, _DENY_NS):
            kubectl.delete("namespace", namespace, **target)
    log.step("NetworkPolicy is enforced.")
