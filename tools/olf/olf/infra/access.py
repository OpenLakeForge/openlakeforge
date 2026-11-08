"""How olf's own clients reach a routed service (ADR 0013, #267).

Every client goes through `service_route`: the contract's ingress route when
there is one, a `kubectl port-forward` when there is not (AWS and Azure
contracts carry no routes) or when `OLF_PORT_FORWARD` asks for it.

A route behind the perimeter (ADR 0014) answers a request with no session by
redirecting to login, so automation never takes it: it port-forwards to the
internal Service, which only an operator with `pods/portforward` can reach.
Only a caller that tests the ingress itself passes `through_ingress=True`.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import requests.utils
from openlakeforge_services.base import JsonHttpClient

from olf.common import config
from olf.common.edge import pinned_address
from olf.infra import k8s

ACCESS_ENV = "OPENLAKEFORGE_ACCESS_JSON"
PORT_FORWARD_ENV = "OLF_PORT_FORWARD"
LOCAL_CA_ISSUER = "local-ca"
# The self-signed root Certificate writes this Secret in the shared namespace
# and the local-ca ClusterIssuer signs from it (modules/access/cert-manager).
# Its `tls.crt` is the root itself; `ca.crt` is the issuer's chain, which
# cert-manager does not guarantee for every issuer type.
LOCAL_CA_SECRET = "local-ca"
LOCAL_CA_KEY = "tls\\.crt"


def contract_access(contracts: Mapping[str, Any] | None, *, stage: str | None = None) -> dict[str, Any]:
    """The TLS mode and enabled routes of a raw provider contract, or {} without an ingress.

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
    perimeter = access.get("perimeter") or {}
    protected = sorted(set(perimeter.get("protected_routes", [])) & set(routes)) if perimeter.get("enabled") else []
    return {
        "tls": access.get("tls", access.get("issuer")),
        "routes": routes,
        **({"protected": protected} if protected else {}),
    }


@dataclass(frozen=True)
class Route:
    """How a client reaches a service: its base URL, and the proxy exemption and CA that URL needs."""

    url: str
    # requests reads `no_proxy` from the call, not from `Session.proxies`, so `session()` injects it per request.
    proxies: Mapping[str, str] = field(default_factory=dict)
    verify: bool | str = True

    def session(self) -> requests.Session:
        return _RouteSession(self)

    def client[C: JsonHttpClient](self, cls: type[C], **kwargs: Any) -> C:
        return cls(self.url, session=self.session(), **kwargs)


class _RouteSession(requests.Session):
    def __init__(self, route: Route) -> None:
        super().__init__()
        self._route = route

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:  # type: ignore[override]  # narrows requests' keyword list to what olf sends
        kwargs["proxies"] = {**self._route.proxies, **(kwargs.get("proxies") or {})}
        kwargs.setdefault("verify", self._route.verify)
        return super().request(method, url, **kwargs)


def _resolve_localhost(host: Any) -> Any:
    # RFC 6761: *.localhost is loopback. Browsers honour that; glibc and macOS
    # often answer NXDOMAIN, and Traefik's host ports bind 127.0.0.1 only.
    if isinstance(host, str):
        name = host.lower().rstrip(".")
        if name.endswith(".localhost"):
            return "127.0.0.1"
        pinned = pinned_address(name)
        if pinned:
            return pinned
    return host


_system_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    return _system_getaddrinfo(_resolve_localhost(host), *args, **kwargs)


# Keyed by the cluster the lookup reaches - namespace, context and kubeconfig - so
# routes resolved for different deployments never share a trust bundle.
_CA_BUNDLES: dict[tuple[str, str | None, str | None], str] = {}


def _ca_bundle(shared_namespace: str, kube_context: str | None, environ: Mapping[str, str] | None = None) -> str:
    """The public CA bundle plus the cluster's local CA, so one bundle verifies both."""
    env = os.environ if environ is None else environ
    key = (shared_namespace, kube_context or env.get("KUBE_CONTEXT"), env.get("KUBECONFIG"))
    if key not in _CA_BUNDLES:
        pem = k8s.secret_value(
            LOCAL_CA_SECRET, LOCAL_CA_KEY, shared_namespace, kube_context=kube_context, environ=environ
        )
        bundle = Path(tempfile.mkdtemp(prefix="olf-ca-")) / "ca-bundle.pem"
        bundle_contents = Path(requests.utils.DEFAULT_CA_BUNDLE_PATH).read_text() + "\n" + pem
        fd = os.open(bundle, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as bundle_file:
            bundle_file.write(bundle_contents)
        _CA_BUNDLES[key] = str(bundle)
    return _CA_BUNDLES[key]


@contextlib.contextmanager
def service_route(
    ref: str,
    *,
    service: str,
    remote_port: int,
    namespace: str,
    log_path: str,
    shared_namespace: str,
    local_port: int | None = None,
    kube_context: str | None = None,
    through_ingress: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Iterator[Route]:
    """Yield the `Route` to the service bound at contract `ref` for the block.

    Over the ingress, `*.localhost` resolves to loopback and the route's
    `verify` is the local CA - no /etc/hosts edit, no verify=False. Clients
    take the proxy exemption and CA from the route (`Route.session()` or
    `Route.client`); the process environment is never written. The CA is read
    from `shared_namespace`, where cert-manager keeps the ClusterIssuer's
    Secret; callers pass it explicitly because `config.shared_namespace()`
    falls back to the stage namespace.
    """
    env = os.environ if environ is None else environ
    access = json.loads(env.get(ACCESS_ENV) or "{}")
    bypass = config.truthy(env.get(PORT_FORWARD_ENV, "")) or (
        ref in access.get("protected", []) and not through_ingress
    )
    url = None if bypass else access.get("routes", {}).get(ref)
    if url is None:
        with k8s.port_forward(
            service,
            remote_port,
            namespace,
            local_port=local_port,
            log_path=log_path,
            kube_context=kube_context,
            environ=environ,
        ) as port:
            yield Route(f"http://127.0.0.1:{port}")
        return
    # A deliberate ambient pin, process-wide and never restored; outside the scope of #559.
    socket.getaddrinfo = _pinned_getaddrinfo
    # requests picks a proxy from the hostname before resolving it, so an
    # HTTPS_PROXY would carry the request away from the pinned loopback.
    host = urlsplit(url).hostname or ""
    # Both spellings: requests honours `no_proxy` and `NO_PROXY`, and an operator may set either.
    no_proxy = ",".join(filter(None, [host, env.get("no_proxy"), env.get("NO_PROXY")]))
    verify: bool | str = True
    # The e2e edge's certificate is signed by the same local CA; a real edge's is publicly trusted.
    if access.get("tls") == LOCAL_CA_ISSUER or (access.get("tls") == "external" and pinned_address(host, env)):
        verify = _ca_bundle(shared_namespace, kube_context, environ)
    yield Route(url, {"no_proxy": no_proxy}, verify)
