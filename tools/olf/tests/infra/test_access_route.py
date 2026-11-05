"""The `Route` that `access.service_route` yields reaches a real server the way the contract says."""

from __future__ import annotations

import contextlib
import datetime
import ipaddress
import json
import os
import ssl
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from openlakeforge_services.base import JsonHttpClient, TransientServiceError
from openlakeforge_services.openmetadata import OpenMetadataClient
from openlakeforge_services.superset import SupersetClient

from olf.infra import access, k8s
from olf.infra.e2e import _dagster, _identity_github, _ingress, _perimeter, _perimeter_bearer
from olf.infra.e2e._shell import E2EError, user_trust_environment


@contextlib.contextmanager
def _server(label: str, hits: list[tuple[str, str]], *, tls: tuple[Path, Path] | None = None) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            hits.append((label, self.path))
            self._answer()

        do_POST = do_GET

        def _answer(self) -> None:
            body = json.dumps({"data": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    scheme = "http"
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"{scheme}://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxy_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    """An operator shell with an HTTP proxy configured, and the proxy it names."""
    hits: list[tuple[str, str]] = []
    with _server("proxy", hits) as proxy:
        for name in ("NO_PROXY", "no_proxy", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("http_proxy", proxy)
        yield hits


def _clients(route: access.Route) -> dict[str, Callable[[], Any]]:
    return {
        "OpenMetadataClient": lambda: route.client(OpenMetadataClient, token="t").request("GET", "/api/v1/ping"),
        "SupersetClient": lambda: route.client(SupersetClient).request("GET", "/api/v1/ping"),
        "JsonHttpClient": lambda: route.client(JsonHttpClient).send("GET", "/api/v1/ping"),
        "Dagster GraphQL": lambda: _dagster._graphql_over(route)("{ version }", None),
        "http_wait": lambda: k8s.http_wait(f"{route.url}/api/v1/ping", attempts=1, session=route.session()),
        "session": lambda: route.session().get(f"{route.url}/api/v1/ping", timeout=5),
    }


@pytest.mark.parametrize("family", list(_clients(access.Route("")).keys()))
def test_every_client_family_bypasses_the_proxy_the_route_exempts(
    family: str, proxy_in_the_environment: list[tuple[str, str]]
) -> None:
    hits = proxy_in_the_environment
    with _server("direct", hits) as url:
        exempt = access.Route(url, {"no_proxy": "127.0.0.1"})
        _clients(exempt)[family]()
        assert [label for label, _ in hits] == ["direct"]

        hits.clear()
        _clients(access.Route(url))[family]()
        assert [label for label, _ in hits] == ["proxy"]


def _self_signed(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "tls.crt", directory / "tls.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    return cert_path, key_path


@pytest.mark.parametrize("family", list(_clients(access.Route("")).keys()))
def test_every_client_family_trusts_the_ca_the_route_carries(
    family: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    cert, key = _self_signed(tmp_path)
    hits: list[tuple[str, str]] = []
    with _server("tls", hits, tls=(cert, key)) as url:
        _clients(access.Route(url, verify=str(cert)))[family]()
        assert [label for label, _ in hits] == ["tls"]

        hits.clear()
        untrusted = _clients(access.Route(url))[family]
        if family == "http_wait":
            assert untrusted() is False
        else:
            with pytest.raises((requests.exceptions.SSLError, TransientServiceError)):
                untrusted()
        assert hits == []


_CFG: Any = SimpleNamespace(platform_namespace="olf-system")


def _perimeter_helpers(route: access.Route) -> dict[str, Callable[[], Any]]:
    """The e2e helpers that used to rely on the ambient proxy exemption and CA, each aimed at `route.url`."""
    identity = {"issuer_url": route.url}
    return {
        "sign_in_with_stub": lambda: _identity_github.sign_in_with_stub(route, route.url, "u", "stub.invalid"),
        "check_unauthenticated_requests": lambda: _perimeter.check_unauthenticated_requests(
            route, identity, ["ref"], {"ref": route.url}, login=route.url
        ),
        "check_bearer_decisions": lambda: _perimeter_bearer.check_bearer_decisions(
            route, route.url, ["r"], lambda _role: True, lambda role: f"a.b.{role}", None
        ),
        "issue_id_token": lambda: _perimeter_bearer.issue_id_token(
            route, _CFG, identity, "u", "olf-cli", "http://127.0.0.1:1/callback"
        ),
    }


def _reaches(call: Callable[[], Any]) -> None:
    # The fixture server answers 200 to everything, so a helper that reached it ends in its own check failing.
    with contextlib.suppress(E2EError):
        call()


@pytest.mark.parametrize("helper", list(_perimeter_helpers(access.Route("")).keys()))
def test_every_perimeter_helper_trusts_the_ca_the_route_carries(
    helper: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    cert, key = _self_signed(tmp_path)
    hits: list[tuple[str, str]] = []
    with _server("tls", hits, tls=(cert, key)) as url:
        _reaches(_perimeter_helpers(access.Route(url, verify=str(cert)))[helper])
        assert hits and {label for label, _ in hits} == {"tls"}

        hits.clear()
        with pytest.raises(requests.exceptions.SSLError):
            _perimeter_helpers(access.Route(url))[helper]()
        assert hits == []


@pytest.mark.parametrize("helper", list(_perimeter_helpers(access.Route("")).keys()))
def test_every_perimeter_helper_bypasses_the_proxy_the_route_exempts(
    helper: str, proxy_in_the_environment: list[tuple[str, str]]
) -> None:
    hits = proxy_in_the_environment
    with _server("direct", hits) as url:
        _reaches(_perimeter_helpers(access.Route(url, {"no_proxy": "127.0.0.1"}))[helper])
        assert hits and {label for label, _ in hits} == {"direct"}

        hits.clear()
        _reaches(_perimeter_helpers(access.Route(url))[helper])
        assert hits and {label for label, _ in hits} == {"proxy"}


def test_the_served_certificate_is_read_with_the_ca_the_route_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    cert, key = _self_signed(tmp_path)
    with _server("tls", [], tls=(cert, key)) as url:
        port = int(url.rsplit(":", 1)[1])
        assert _ingress._served_serial("127.0.0.1", port, str(cert))
        with pytest.raises(ssl.SSLError):
            _ingress._served_serial("127.0.0.1", port, True)


def test_the_user_trust_environment_sets_what_a_user_would_set_for_its_block_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "internal.corp")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    route = access.Route("https://x.olf.localhost", {"no_proxy": "x.olf.localhost,internal.corp"}, "/ca.pem")

    with user_trust_environment(route):
        assert os.environ["REQUESTS_CA_BUNDLE"] == "/ca.pem"
        assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == "x.olf.localhost,internal.corp"

    assert os.environ["NO_PROXY"] == "internal.corp"
    assert "no_proxy" not in os.environ and "REQUESTS_CA_BUNDLE" not in os.environ


def test_the_user_trust_environment_is_restored_when_its_block_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/operator.pem")

    with pytest.raises(RuntimeError), user_trust_environment(access.Route("", {}, "/ca.pem")):
        raise RuntimeError("boom")

    assert os.environ["REQUESTS_CA_BUNDLE"] == "/operator.pem"


def test_a_route_with_nothing_to_trust_leaves_the_environment_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    before = dict(os.environ)

    with user_trust_environment(access.Route("http://127.0.0.1:1")):
        assert dict(os.environ) == before
