"""Time-boxed, recorded access to a service while the issuer is down (#177).

Authority is the operator's kubeconfig, so nothing is created that outlives
the command: a port-forward (or an in-pod shell for the services that only
accept issuer login) ends with the process or at the TTL, and no Ingress or
perimeter setting is touched.

Each use leaves one ConfigMap in the shared namespace. It is written before
access opens and completed on exit, and it is never deleted, so the record
survives the session. A record with no `ended` is an unclean exit. Events
would also do, but Kubernetes expires them after an hour.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from olf.tooling.kubectl import Kubectl

LABEL = "openlakeforge.io/break-glass"
DEFAULT_TTL_MINUTES = 30


@dataclass(frozen=True)
class Target:
    resource: str
    port: int | None  # None: an in-pod shell instead of a port-forward
    shared: bool


# Dex has no admin account, so reaching it is for diagnosis. Superset and
# OpenMetadata accept only issuer login, so their administrative path is the
# CLI inside the pod. Dagster and Trino have no native authentication.
TARGETS: Mapping[str, Target] = {
    "dex": Target("svc/dex", 5556, shared=True),
    "trino": Target("svc/trino", 8080, shared=True),
    "dagster": Target("svc/dagster-dagster-webserver", 80, shared=False),
    "superset": Target("deploy/superset", None, shared=False),
    "openmetadata": Target("deploy/openmetadata", None, shared=True),
}


@dataclass(frozen=True)
class Cluster:
    kubectl: Kubectl
    context: str
    kubeconfig: Path | None
    shared_namespace: str
    namespace: str
    env: Mapping[str, str] | None = None

    @property
    def kw(self) -> dict[str, Any]:
        return {"context": self.context, "kubeconfig": self.kubeconfig, "env": self.env}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_session(
    cluster: Cluster,
    service: str,
    reason: str,
    ttl_minutes: int,
    *,
    local_port: int = 0,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    """Record the use, open the path until it exits or the TTL passes, then complete the record."""
    target = TARGETS[service]
    started = datetime.now(UTC)
    name = f"break-glass-{started:%Y%m%d%H%M%S}-{secrets.token_hex(2)}"
    namespace = cluster.shared_namespace if target.shared else cluster.namespace
    user = cluster.kubectl.config_user(**cluster.kw).stdout.strip() or "unknown"
    record = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": cluster.shared_namespace, "labels": {LABEL: "true"}},
        "data": {
            "service": service,
            "reason": reason,
            "user": user,
            "started": _now(),
            "ttl_minutes": str(ttl_minutes),
        },
    }
    cluster.kubectl.create_from_manifest(json.dumps(record), **cluster.kw)
    if target.port is None:
        argv = cluster.kubectl.exec_shell_argv(target.resource, namespace=namespace, context=cluster.context)
    else:
        ports = [f"{local_port or target.port}:{target.port}"]
        argv = cluster.kubectl.port_forward_argv(target.resource, ports, namespace=namespace, context=cluster.context)
    try:
        run(argv, timeout=ttl_minutes * 60, check=False, env=_env(cluster))
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        pass  # the TTL, or Ctrl-C, is the expected way out of a port-forward
    finally:
        seconds = int((datetime.now(UTC) - started).total_seconds())
        done = json.dumps({"data": {"ended": _now(), "duration_seconds": str(seconds)}})
        cluster.kubectl.patch("configmap", name, done, namespace=cluster.shared_namespace, **cluster.kw)
    return name


def _env(cluster: Cluster) -> dict[str, str]:
    import os

    kubeconfig = {"KUBECONFIG": str(cluster.kubeconfig)} if cluster.kubeconfig else {}
    return {**os.environ, **(cluster.env or {}), **kubeconfig}


def recent(kubectl: Kubectl, namespace: str, *, limit: int = 5, **kw: Any) -> list[dict[str, str]] | None:
    """The newest records, newest first; None when the namespace cannot be read."""
    result = kubectl.get("configmap", namespace=namespace, output="json", extra_args=["-l", LABEL], check=False, **kw)
    if not result.ok:
        return None
    try:
        items = [item.get("data") or {} for item in json.loads(result.stdout).get("items", [])]
    except ValueError:
        return None
    return sorted(items, key=lambda d: d.get("started", ""), reverse=True)[:limit]


def render(records: list[dict[str, str]] | None) -> str:
    if records is None:
        return "unavailable"
    if not records:
        return "none"
    return "\n".join(
        f"{r.get('started', '?')}  {r.get('user', '?')}  {r.get('service', '?')}  "
        + (f"{r['duration_seconds']}s" if "ended" in r else "no end recorded (unclean exit)")
        + f"  {r.get('reason', '')}"
        for r in records
    )
