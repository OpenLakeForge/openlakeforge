"""Deploy gate: does this cluster's CNI enforce NetworkPolicy? (ADR 0017, slice 1a)

One server and one client. The pair must connect with no policy, which is the
positive control: a pair that cannot connect for another reason would read as
"blocked". Then an ingress-only and an egress-only deny are each applied to the
same pair and must each block, because a dataplane can enforce one direction
and not the other. Last, an ingress deny must block, adding a rule allowing
the client must connect, and removing that rule must block again: a dataplane
can drop everything a policy selects and still fail to admit what it allows (#622).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import yaml

from olf.common import log
from olf.common.deployment.errors import DeploymentPreconditionError
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.local import prefetch

_NAMESPACE = "olf-netpol-canary"
_PORT = 8080
# A dataplane programs a policy asynchronously, so a connection made right
# after the policy is created can still succeed on a cluster that enforces.
_ATTEMPTS = 6
_DELAY_SECONDS = 5.0


# kindnet is the only CNI the local provider is built and tested with (ADR 0017); Calico was never built.
_UNSUPPORTED_HOST = (
    "The local provider supports only kind's default CNI (kindnet), on a host that enforces NetworkPolicy with it. "
    "A cluster whose CNI was replaced, or a host where kindnet does not enforce, is not supported (ADR 0017)."
)

Step = tuple[Callable[[], object], Callable[[], object]]


class PolicyNotEnforcedError(DeploymentPreconditionError):
    pass


def verdict(
    connects: Callable[[], bool],
    denies: Sequence[tuple[str, Callable[[], object], Callable[[], object]]],
    allow: tuple[Step, Step] | None = None,
    *,
    attempts: int = _ATTEMPTS,
    delay: float = _DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """`denies` holds (direction, apply, remove). Each must turn a connecting pair into a blocked one.

    `allow` is ((apply deny, remove deny), (apply allow, remove allow)): the deny alone must block, adding the
    allow must connect, and removing the allow must block again.
    """

    def eventually(expected: bool) -> bool:
        # Removing a policy is as asynchronous as applying one.
        for attempt in range(attempts):
            if connects() is expected:
                return True
            if attempt < attempts - 1:
                sleep(delay)
        return False

    for direction, apply, remove in denies:
        if not eventually(True):
            raise PolicyNotEnforcedError(
                f"NetworkPolicy canary is inconclusive: the pair could not connect before the {direction} "
                "policy, so a blocked connection afterwards would prove nothing."
            )
        apply()
        try:
            if not eventually(False):
                raise PolicyNotEnforcedError(
                    f"This cluster does not enforce {direction} NetworkPolicy: the client still connected "
                    f"through a deny policy ({attempts} attempts). Default-deny policies would be inert. "
                    + _UNSUPPORTED_HOST
                )
        finally:
            remove()

    if allow is None:
        return
    (apply_deny, remove_deny), (apply_allow, remove_allow) = allow
    if not eventually(True):
        raise PolicyNotEnforcedError(
            "NetworkPolicy canary is inconclusive: the pair could not connect before the allow policy."
        )
    apply_deny()
    try:
        # The deny must be seen to block first: a probe that ran before either policy was programmed would
        # connect and pass the allow without proving anything.
        if not eventually(False):
            raise PolicyNotEnforcedError(
                f"This cluster does not enforce ingress NetworkPolicy: the client still connected through a deny "
                f"policy ({attempts} attempts). Default-deny policies would be inert. " + _UNSUPPORTED_HOST
            )
        apply_allow()
        try:
            if not eventually(True):
                raise PolicyNotEnforcedError(
                    "This cluster blocks traffic a NetworkPolicy allows: the client could not connect through an "
                    f"ingress deny plus a rule allowing it ({attempts} attempts). Services behind the platform's "
                    "policies would be unreachable. " + _UNSUPPORTED_HOST
                )
        finally:
            remove_allow()
        if not eventually(False):
            raise PolicyNotEnforcedError(
                "This cluster kept admitting traffic after its NetworkPolicy allow rule was removed "
                f"({attempts} attempts), so the allow rule never took effect on its own. " + _UNSUPPORTED_HOST
            )
    finally:
        remove_deny()


def _deny(direction: str) -> str:
    pod = "server" if direction == "ingress" else "client"
    return yaml.safe_dump(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": f"deny-{direction}", "namespace": _NAMESPACE},
            "spec": {"podSelector": {"matchLabels": {"app": pod}}, "policyTypes": [direction.capitalize()]},
        }
    )


def _allow_client() -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-client", "namespace": _NAMESPACE},
            "spec": {
                "podSelector": {"matchLabels": {"app": "server"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "client"}}}]}],
            },
        }
    )


def _pods(image: str) -> str:
    def pod(name: str, command: list[str]) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": _NAMESPACE, "labels": {"app": name}},
            "spec": {"restartPolicy": "Never", "containers": [{"name": name, "image": image, "command": command}]},
        }

    return yaml.safe_dump_all(
        [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": _NAMESPACE}},
            pod("server", ["python3", "-m", "http.server", str(_PORT)]),
            pod("client", ["sleep", "3600"]),
        ]
    )


def require_policy_enforcement(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    catalog = yaml.safe_load((config.context.paths.distribution_root / "release/component-catalog.yaml").read_text())
    image = catalog["components"]["images"]["k8s_bootstrap"]
    kubectl = tools.kubectl
    target = {"context": config.kube_context, "kubeconfig": config.paths.kubeconfig_path, "env": env}

    log.step("Checking that the cluster enforces NetworkPolicy...")
    kubectl.delete("namespace", _NAMESPACE, **target)
    # The gate runs before the prefetch phase, so without this the kubelet
    # pulls the image itself and the readiness wait below times out on a slow network.
    prefetch.load_image_into_nodes(config, tools, image, env=env)
    try:
        kubectl.create_from_manifest(_pods(image), **target)
        for pod in ("server", "client"):
            kubectl.wait(f"pod/{pod}", for_condition="condition=Ready", namespace=_NAMESPACE, timeout="180s", **target)
        ip = kubectl.get("pod", name="server", namespace=_NAMESPACE, output="jsonpath={.status.podIP}", **target)
        # The shell swallows curl's exit code so a blocked connection is not
        # confused with a failed exec, which must raise instead.
        script = f"curl -s -o /dev/null -m 3 http://{ip.stdout.strip()}:{_PORT}/ && echo connected || echo blocked"

        def connects() -> bool:
            return "connected" in kubectl.exec("client", ["sh", "-c", script], namespace=_NAMESPACE, **target).stdout

        def deny(direction: str) -> tuple[str, Callable[[], object], Callable[[], object]]:
            return (
                direction,
                lambda: kubectl.create_from_manifest(_deny(direction), **target),
                lambda: kubectl.delete("networkpolicy", f"deny-{direction}", namespace=_NAMESPACE, **target),
            )

        _, apply_deny, remove_deny = deny("ingress")
        allow = (
            lambda: kubectl.create_from_manifest(_allow_client(), **target),
            lambda: kubectl.delete("networkpolicy", "allow-client", namespace=_NAMESPACE, **target),
        )
        verdict(connects, [deny("ingress"), deny("egress")], ((apply_deny, remove_deny), allow))
    finally:
        kubectl.delete("namespace", _NAMESPACE, **target)
    log.step("NetworkPolicy is enforced.")
