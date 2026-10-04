from __future__ import annotations

import contextlib
import json
import os
import socket
from pathlib import Path

import pytest

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
    monkeypatch.setattr(access.k8s, "secret_value", lambda *_a, **_k: "LOCAL-CA-PEM\n")
    # service_url pins resolution process-wide; monkeypatch restores the original.
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv(access.PORT_FORWARD_ENV, raising=False)
    access._ca_bundle.cache_clear()
    return calls


def _reach(ref: str = "stage/dev/reporting"):  # noqa: ANN202
    return access.service_url(ref, service="superset", remote_port=8088, namespace="olf-dev", log_path="/dev/null")


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
