from __future__ import annotations

import contextlib
import json
import os
import socket
from pathlib import Path

import pytest
import requests.utils

from olf import access
from olf.commands.access import trust_steps
from olf.contracts import build_contract_env

REPO_ROOT = Path(__file__).resolve().parents[3]
ROUTES = {"issuer": "local-ca", "routes": {"stage/dev/reporting": "https://superset.dev.olf.localhost"}}


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
    # service_url pins resolution process-wide; monkeypatch restores the original.
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv(access.PORT_FORWARD_ENV, raising=False)
    access._ca_bundle.cache_clear()
    return calls


def _reach(ref: str = "stage/dev/reporting"):  # noqa: ANN202
    return access.service_url(
        ref,
        service="superset",
        remote_port=8088,
        namespace="olf-dev",
        log_path="/dev/null",
        shared_namespace="olf-system",
    )


def test_ingress_route_is_used_and_verified_against_the_local_ca(
    monkeypatch: pytest.MonkeyPatch, forwarded: list[str]
) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))

    with _reach() as url:
        bundle = Path(os.environ["REQUESTS_CA_BUNDLE"]).read_text()
        loopback = socket.getaddrinfo("superset.dev.olf.localhost", 443, type=socket.SOCK_STREAM)

    assert url == "https://superset.dev.olf.localhost"
    assert forwarded == []
    assert bundle.endswith("LOCAL-CA-PEM\n") and "BEGIN CERTIFICATE" in bundle
    assert {info[4][0] for info in loopback} == {"127.0.0.1"}
    assert "REQUESTS_CA_BUNDLE" not in os.environ


def test_port_forward_without_a_route_or_when_asked(monkeypatch: pytest.MonkeyPatch, forwarded: list[str]) -> None:
    monkeypatch.delenv(access.ACCESS_ENV, raising=False)
    with _reach() as url:
        assert url == "http://127.0.0.1:18088"

    monkeypatch.setenv(access.ACCESS_ENV, json.dumps(ROUTES))
    monkeypatch.setenv(access.PORT_FORWARD_ENV, "1")
    with _reach() as url:
        assert url == "http://127.0.0.1:18088"
    assert forwarded == ["superset", "superset"]


def test_a_public_issuer_keeps_the_system_trust_store(monkeypatch: pytest.MonkeyPatch, forwarded: list[str]) -> None:
    monkeypatch.setenv(access.ACCESS_ENV, json.dumps({**ROUTES, "issuer": "letsencrypt"}))

    with _reach() as url:
        assert "REQUESTS_CA_BUNDLE" not in os.environ
    assert url == "https://superset.dev.olf.localhost"


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

    with _reach() as url:
        bypassed = requests.utils.should_bypass_proxies(url, no_proxy=None)

    assert bypassed
    assert os.environ["NO_PROXY"] == "internal.corp"
    assert "no_proxy" not in os.environ
