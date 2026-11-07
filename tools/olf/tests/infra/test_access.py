from __future__ import annotations

import contextlib
import json
import os
import socket
from pathlib import Path

import pytest
import requests.utils

from olf.infra import access
from olf.infra.commands.access import trust_steps
from olf.infra.contracts import build_contract_env

REPO_ROOT = Path(__file__).resolve().parents[4]
ROUTES = {"tls": "local-ca", "routes": {"stage/dev/reporting": "https://superset-dev.olf.localhost"}}


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    @contextlib.contextmanager
    def _port_forward(service, remote_port, namespace, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(service)
        yield 18088

    monkeypatch.setattr(access.k8s, "port_forward", _port_forward)
    # The self-signed root's Secret exists only in the shared namespace, where
    # cert-manager keeps ClusterIssuer secrets, and tls.crt/tls.key are the keys
    # cert-manager guarantees. Any other namespace or key raises KeyError.
    secrets = {("local-ca", "olf-system"): {"tls\\.crt": "LOCAL-CA-PEM\n", "tls\\.key": "KEY"}}
    monkeypatch.setattr(access.k8s, "secret_value", lambda name, key, namespace, **_k: secrets[(name, namespace)][key])
    # service_route pins resolution process-wide; monkeypatch restores the original.
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv(access.PORT_FORWARD_ENV, raising=False)
    access._CA_BUNDLES.clear()
    return calls


def test_a_route_behind_the_perimeter_is_port_forwarded_unless_the_ingress_is_under_test(
    forwarded: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps({**ROUTES, "protected": ["stage/dev/reporting"]}))

    with _reach() as route:
        assert route.url == "http://127.0.0.1:18088"
    with _reach(through_ingress=True) as route:
        assert route.url == "https://superset-dev.olf.localhost"
    assert forwarded == ["superset"]


def test_contract_access_lists_the_protected_routes_only_when_the_perimeter_is_enabled() -> None:
    contracts = {
        "shared": {
            "access": {
                "tls": "local-ca",
                "routes": {"shared/portal": {"url": "https://olf.localhost", "enabled": True}},
                "perimeter": {"enabled": True, "protected_routes": ["shared/portal", "shared/gone"]},
            }
        }
    }

    assert access.contract_access(contracts)["protected"] == ["shared/portal"]
    contracts["shared"]["access"]["perimeter"]["enabled"] = False
    assert "protected" not in access.contract_access(contracts)


def _reach(ref: str = "stage/dev/reporting", **kwargs):  # noqa: ANN003, ANN202
    return access.service_route(
        ref,
        service="superset",
        remote_port=8088,
        namespace="olf-dev",
        log_path="/dev/null",
        shared_namespace="olf-system",
        **kwargs,
    )


def test_ingress_route_is_used_and_verified_against_the_local_ca(
    monkeypatch: pytest.MonkeyPatch, forwarded: list[str]
) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))

    with _reach() as route:
        assert isinstance(route.verify, str)
        bundle = Path(route.verify).read_text()
        loopback = socket.getaddrinfo("superset-dev.olf.localhost", 443, type=socket.SOCK_STREAM)

    assert route.url == "https://superset-dev.olf.localhost"
    assert forwarded == []
    assert bundle.endswith("LOCAL-CA-PEM\n") and "BEGIN CERTIFICATE" in bundle
    assert {info[4][0] for info in loopback} == {"127.0.0.1"}


def test_a_route_never_writes_the_process_environment(monkeypatch: pytest.MonkeyPatch, forwarded: list[str]) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))
    monkeypatch.setenv("NO_PROXY", "internal.corp")
    monkeypatch.delenv("no_proxy", raising=False)
    before = dict(os.environ)

    with _reach() as route:
        assert dict(os.environ) == before
        assert isinstance(route.verify, str) and route.proxies  # the route carries them instead
    assert dict(os.environ) == before
    assert "REQUESTS_CA_BUNDLE" not in os.environ and "no_proxy" not in os.environ


def test_port_forward_without_a_route_or_when_asked(monkeypatch: pytest.MonkeyPatch, forwarded: list[str]) -> None:
    monkeypatch.delenv(access.ACCESS_ENV, raising=False)
    with _reach() as route:
        assert route.url == "http://127.0.0.1:18088"

    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))
    monkeypatch.setenv(access.PORT_FORWARD_ENV, "1")
    with _reach() as route:
        assert route.url == "http://127.0.0.1:18088"
    assert forwarded == ["superset", "superset"]


def test_a_public_issuer_keeps_the_system_trust_store(monkeypatch: pytest.MonkeyPatch, forwarded: list[str]) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps({**ROUTES, "tls": "external"}))

    with _reach() as route:
        assert "REQUESTS_CA_BUNDLE" not in os.environ
    assert route.url == "https://superset-dev.olf.localhost"


def test_the_route_carries_what_the_local_ca_ingress_needs(
    monkeypatch: pytest.MonkeyPatch, forwarded: list[str], tmp_path: Path
) -> None:
    access_json = json.dumps({**ROUTES, "protected": ["stage/dev/other"]})
    environ = {access.ACCESS_ENV: access_json, "NO_PROXY": "internal.corp"}
    monkeypatch.delenv(access.ACCESS_ENV, raising=False)

    with _reach(environ=environ) as route:
        assert route.url == "https://superset-dev.olf.localhost"
        assert route.proxies == {"no_proxy": "superset-dev.olf.localhost,internal.corp"}
        assert isinstance(route.verify, str)
        assert Path(route.verify).read_text().endswith("LOCAL-CA-PEM\n")


def test_the_route_of_a_public_issuer_verifies_with_the_system_trust_store(forwarded: list[str]) -> None:
    environ = {access.ACCESS_ENV: json.dumps({**ROUTES, "tls": "external"})}

    with _reach(environ=environ) as route:
        assert (route.url, route.verify) == ("https://superset-dev.olf.localhost", True)
        assert route.proxies == {"no_proxy": "superset-dev.olf.localhost"}


def test_the_route_of_a_port_forward_is_the_loopback_without_exemptions(forwarded: list[str]) -> None:
    protected = {access.ACCESS_ENV: json.dumps({**ROUTES, "protected": ["stage/dev/reporting"]})}
    forced = {access.ACCESS_ENV: json.dumps(ROUTES), access.PORT_FORWARD_ENV: "1"}

    for environ in ({}, protected, forced):
        with _reach(environ=environ) as route:
            assert route == access.Route("http://127.0.0.1:18088")
    assert forwarded == ["superset"] * 3


def test_the_environ_argument_replaces_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, forwarded: list[str]
) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))

    with _reach(environ={}) as route:
        assert route.url == "http://127.0.0.1:18088"


def test_contract_without_an_ingress_clears_a_stale_access_env() -> None:
    _, unsets = build_contract_env({access.ACCESS_ENV: "{}"}, None, repo_root=REPO_ROOT)

    assert access.ACCESS_ENV in unsets
    assert access.contract_access({"shared": {"access": {"implementation": "access.kubectl_port_forward"}}}) == {}


def test_trust_steps_name_the_store_the_browser_reads() -> None:
    cert = Path("/work/openlakeforge-local-ca.crt")

    assert "/Library/Keychains/System.keychain" in trust_steps(cert, platform="darwin", wsl=False)
    assert "Cert:\\CurrentUser\\Root" in trust_steps(cert, platform="linux", wsl=True)
    linux = trust_steps(cert, platform="linux", wsl=False)
    assert "update-ca-certificates" in linux and "certutil" in linux


def test_trust_steps_quote_the_certificate_path() -> None:
    cert = Path("/home/a user/openlakeforge-local-ca.crt")

    for platform, wsl in (("darwin", False), ("linux", False), ("linux", True)):
        steps = trust_steps(cert, platform=platform, wsl=wsl)
        assert "/home/a user/" not in steps.replace("'/home/a user/openlakeforge-local-ca.crt'", "")
    assert "$(wslpath -w '/home/a user/openlakeforge-local-ca.crt')" in trust_steps(cert, platform="linux", wsl=True)
    assert "-CertStoreLocation 'Cert:\\CurrentUser\\Root'" in trust_steps(cert, platform="linux", wsl=True)


def test_a_configured_proxy_is_bypassed_for_the_pinned_route(
    monkeypatch: pytest.MonkeyPatch, forwarded: list[str]
) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "internal.corp")
    monkeypatch.delenv("no_proxy", raising=False)

    with _reach() as route:
        bypassed = requests.utils.should_bypass_proxies(route.url, no_proxy=route.proxies["no_proxy"])

    assert bypassed
    assert os.environ["NO_PROXY"] == "internal.corp"
    assert "no_proxy" not in os.environ


@pytest.mark.parametrize("name", ["no_proxy", "NO_PROXY"])
def test_the_route_keeps_the_operators_proxy_exemptions_in_either_case(
    monkeypatch: pytest.MonkeyPatch, forwarded: list[str], name: str
) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv(name, ".localhost")

    with _reach() as route:
        exemption = route.proxies["no_proxy"]

    # Another loopback host the same session reaches (the portal) must still bypass the proxy.
    assert requests.utils.should_bypass_proxies("https://olf.localhost/", no_proxy=exemption)
    assert requests.utils.should_bypass_proxies(route.url, no_proxy=exemption)


def test_the_local_ca_is_read_from_the_cluster_the_supplied_environ_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import base64

    bindir = tmp_path / "bin"
    bindir.mkdir()
    pem = base64.b64encode(b"CONTRACT-CA-PEM\n").decode()
    # Answers only for the supplied kubeconfig and context: the ambient cluster has no CA to give.
    (bindir / "kubectl").write_text(
        f'#!/bin/sh\n[ "$KUBECONFIG" = "{tmp_path}/kc" ] && [ "$2" = "contract-ctx" ] || exit 1\necho {pem}\n'
    )
    (bindir / "kubectl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    for name in ("KUBE_CONTEXT", "KUBECONFIG", "REQUESTS_CA_BUNDLE", access.ACCESS_ENV, access.PORT_FORWARD_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    environ = {
        access.ACCESS_ENV: json.dumps(ROUTES),
        "KUBE_CONTEXT": "contract-ctx",
        "KUBECONFIG": str(tmp_path / "kc"),
        "OLF_TOOLCHAIN_MODE": "host",
    }

    with _reach(environ=environ) as route:
        assert isinstance(route.verify, str)
        assert Path(route.verify).read_text().endswith("CONTRACT-CA-PEM\n")
