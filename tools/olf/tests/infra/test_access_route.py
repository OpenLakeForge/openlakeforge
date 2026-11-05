"""The `Route` that `access.service_route` yields reaches a real server the way the contract says."""

from __future__ import annotations

import contextlib
import datetime
import ipaddress
import json
import ssl
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
