"""How olf's own clients reach a routed service (ADR 0013, #267).

Every client goes through `service_url`: the contract's ingress route when
there is one, a `kubectl port-forward` when there is not (AWS and Azure
contracts carry no routes) or when `OLF_PORT_FORWARD` asks for it.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import requests.utils

from olf import config, k8s

ACCESS_ENV = "OPENLAKEFORGE_ACCESS_JSON"
PORT_FORWARD_ENV = "OLF_PORT_FORWARD"
LOCAL_CA_ISSUER = "local-ca"
# The cert-manager Certificate the local-ca ClusterIssuer signs from writes
# its root here, in the shared namespace (modules/access/cert-manager).
LOCAL_CA_SECRET = "local-ca"


def contract_access(contracts: Mapping[str, Any] | None, *, stage: str | None = None) -> dict[str, Any]:
    """The issuer and enabled routes of a raw provider contract, or {} without an ingress.

    With `stage`, another stage's routes are dropped: a stage's environment
    never names another stage's services.
    """
    access = ((contracts or {}).get("shared") or {}).get("access") or {}
    if "routes" not in access:
        return {}
    routes = {
        ref: route["url"]
        for ref, route in access["routes"].items()
        if route.get("enabled") and (stage is None or not ref.startswith("stage/") or ref.startswith(f"stage/{stage}/"))
    }
    return {"issuer": access["issuer"], "routes": routes}


def _resolve_localhost(host: Any) -> Any:
    # RFC 6761: *.localhost is loopback. Browsers honour that; glibc and macOS
    # often answer NXDOMAIN, and Traefik's host ports bind 127.0.0.1 only.
    if isinstance(host, str) and host.lower().rstrip(".").endswith(".localhost"):
        return "127.0.0.1"
    return host


_system_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    return _system_getaddrinfo(_resolve_localhost(host), *args, **kwargs)


@cache
def _ca_bundle(kube_context: str | None) -> str:
    """The public CA bundle plus the cluster's local CA, so one bundle verifies both."""
    pem = k8s.secret_value(LOCAL_CA_SECRET, "ca\\.crt", config.shared_namespace(), kube_context=kube_context)
    bundle = Path(tempfile.mkdtemp(prefix="olf-ca-")) / "ca-bundle.pem"
    bundle.write_text(Path(requests.utils.DEFAULT_CA_BUNDLE_PATH).read_text() + "\n" + pem)
    return str(bundle)


@contextlib.contextmanager
def service_url(
    ref: str,
    *,
    service: str,
    remote_port: int,
    namespace: str,
    log_path: str,
    local_port: int | None = None,
    kube_context: str | None = None,
) -> Iterator[str]:
    """Yield the base URL of the service bound at contract `ref` for the block.

    Over the ingress, `*.localhost` resolves to loopback and `requests`
    verifies against the local CA for the block (REQUESTS_CA_BUNDLE, which a
    bare `requests.post` honours as well as a session) - no /etc/hosts edit,
    no verify=False.
    """
    access = json.loads(os.environ.get(ACCESS_ENV) or "{}")
    url = None if config.truthy(os.environ.get(PORT_FORWARD_ENV, "")) else access.get("routes", {}).get(ref)
    if url is None:
        with k8s.port_forward(
            service, remote_port, namespace, local_port=local_port, log_path=log_path, kube_context=kube_context
        ) as port:
            yield f"http://127.0.0.1:{port}"
        return
    socket.getaddrinfo = _pinned_getaddrinfo
    if access.get("issuer") != LOCAL_CA_ISSUER:
        yield url
        return
    previous = os.environ.get("REQUESTS_CA_BUNDLE")
    os.environ["REQUESTS_CA_BUNDLE"] = _ca_bundle(kube_context)
    try:
        yield url
    finally:
        if previous is None:
            os.environ.pop("REQUESTS_CA_BUNDLE", None)
        else:
            os.environ["REQUESTS_CA_BUNDLE"] = previous
