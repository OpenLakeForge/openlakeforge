from __future__ import annotations

import datetime
import http.server
import io
import ipaddress
import logging
import ssl
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from openlakeforge_services import notify
from openlakeforge_services.notify import DeliveryResult, WebhookNotifier, quiet_transport_logs

SECRET_PATH = "/hooks/T0SECRET/B0TOKEN"
SECRET_QUERY = "key=s3cr3t-query"

Script = list[tuple[int, dict[str, str]]]


class _Receiver:
    """Answers each POST with the next scripted (status, headers) and records what it was sent."""

    def __init__(self, script: Script, context: ssl.SSLContext) -> None:
        self.script = list(script)
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append((self.path, dict(self.headers), self.rfile.read(length)))
                status, headers = outer.script.pop(0) if outer.script else (200, {})
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"BODY-X")

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server.server_port}{SECRET_PATH}?{SECRET_QUERY}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("tls")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_file, key_file


@pytest.fixture
def serve(cert: tuple[Path, Path]) -> Iterator[Callable[[Script], _Receiver]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*cert)
    started: list[_Receiver] = []

    def make(script: Script) -> _Receiver:
        receiver = _Receiver(script, context)
        started.append(receiver)
        return receiver

    yield make
    for receiver in started:
        receiver.server.shutdown()
        receiver.server.server_close()


def _notifier(url: str, cert: tuple[Path, Path], sleeps: list[float]) -> WebhookNotifier:
    return WebhookNotifier("ops", url, ca_file=str(cert[0]), sleep=sleeps.append, jitter=lambda ceiling: ceiling)


def test_retries_then_delivers(serve: Callable[[Script], _Receiver], cert: tuple[Path, Path]) -> None:
    receiver = serve([(503, {}), (429, {"Retry-After": "2"}), (200, {})])
    sleeps: list[float] = []
    result = _notifier(receiver.url, cert, sleeps).send({"a": 1}, delivery_id="d-1")
    assert result == DeliveryResult("delivered", 200, 3)
    assert len(receiver.requests) == 3
    assert len(sleeps) == 2 and sleeps[1] >= 2.0
    path, headers, body = receiver.requests[0]
    assert path == f"{SECRET_PATH}?{SECRET_QUERY}"
    assert headers["Idempotency-Key"] == "d-1" and headers["X-OpenLakeForge-Delivery"] == "d-1"
    assert headers["Content-Type"] == "application/json"
    assert body == b'{"a": 1}'


def test_attempts_and_total_wait_are_bounded(serve: Callable[[Script], _Receiver], cert: tuple[Path, Path]) -> None:
    receiver = serve([(500, {"Retry-After": "3600"})] * 10)
    sleeps: list[float] = []
    result = _notifier(receiver.url, cert, sleeps).send({}, delivery_id="d")
    assert result == DeliveryResult("retryable", 500, notify.MAX_ATTEMPTS)
    assert len(receiver.requests) == notify.MAX_ATTEMPTS
    assert len(sleeps) == notify.MAX_ATTEMPTS - 1
    assert max(sleeps) <= notify.RETRY_AFTER_MAX_SECONDS
    request_budget = notify.MAX_ATTEMPTS * (notify.CONNECT_TIMEOUT_SECONDS + notify.READ_TIMEOUT_SECONDS)
    assert sum(sleeps) + request_budget <= notify.MAX_TOTAL_SECONDS


def test_client_error_is_rejected_without_retry(serve: Callable[[Script], _Receiver], cert: tuple[Path, Path]) -> None:
    receiver = serve([(400, {})])
    sleeps: list[float] = []
    result = _notifier(receiver.url, cert, sleeps).send({}, delivery_id="d")
    assert result == DeliveryResult("rejected", 400, 1)
    assert len(receiver.requests) == 1 and sleeps == []


def test_redirect_is_not_followed(serve: Callable[[Script], _Receiver], cert: tuple[Path, Path]) -> None:
    target = serve([])
    receiver = serve([(302, {"Location": target.url})])
    result = _notifier(receiver.url, cert, []).send({}, delivery_id="d")
    assert result == DeliveryResult("rejected", 302, 1)
    assert target.requests == []


def test_connection_failure_is_retryable(cert: tuple[Path, Path]) -> None:
    sleeps: list[float] = []
    result = _notifier(f"https://127.0.0.1:1{SECRET_PATH}", cert, sleeps).send({}, delivery_id="d")
    assert result == DeliveryResult("retryable", None, notify.MAX_ATTEMPTS)
    assert len(sleeps) == notify.MAX_ATTEMPTS - 1


@pytest.mark.parametrize("url", ["http://example.com/hook", "ftp://example.com/hook", "example.com/hook"])
def test_non_https_is_refused_without_naming_the_url(url: str) -> None:
    with pytest.raises(ValueError) as caught:
        WebhookNotifier("ops", url)
    assert url not in str(caught.value)


def test_url_never_reaches_logs_exceptions_or_repr(
    serve: Callable[[Script], _Receiver], cert: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    receiver = serve([(500, {}), (500, {}), (500, {}), (400, {})])
    caplog.set_level(logging.DEBUG)
    quiet_transport_logs()
    notifier = _notifier(receiver.url, cert, [])
    notifier.send({}, delivery_id="d")
    notifier.send({}, delivery_id="d")
    unreachable = _notifier(f"https://127.0.0.1:1{SECRET_PATH}?{SECRET_QUERY}", cert, [])
    unreachable.send({}, delivery_id="d")
    with pytest.raises(TypeError) as caught:
        notifier.send({"bad": object()}, delivery_id="d")

    text = "\n".join([caplog.text, repr(notifier), repr(unreachable), str(caught.value)])
    for leaked in (SECRET_PATH, "T0SECRET", "s3cr3t-query", "BODY-X", receiver.url):
        assert leaked not in text
    assert "ops" in caplog.text


def test_importing_the_notifier_leaves_other_clients_logging_alone() -> None:
    probe = (
        "import logging, openlakeforge_services.notify as n;"
        "pool = logging.getLogger('urllib3.connectionpool');"
        "assert pool.filters == [], 'import changed urllib3 logging';"
        "n.quiet_transport_logs(); n.quiet_transport_logs();"
        "assert len(pool.filters) == 1"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def _headers(**values: str) -> requests.Response:
    response = requests.Response()
    response.headers.update(values)
    return response


def test_retry_after_accepts_seconds_and_http_dates() -> None:
    now = datetime.datetime(2026, 11, 16, 12, 0, 0, tzinfo=datetime.UTC)
    clock = lambda: now  # noqa: E731
    assert notify._retry_after(_headers(**{"Retry-After": "3"}), clock) == 3.0
    assert notify._retry_after(_headers(**{"Retry-After": "Mon, 16 Nov 2026 12:00:05 GMT"}), clock) == 5.0
    assert notify._retry_after(_headers(**{"Retry-After": "Mon, 16 Nov 2026 11:00:00 GMT"}), clock) == 0.0
    assert notify._retry_after(_headers(**{"Retry-After": "Mon, 16 Nov 2026 13:00:00 GMT"}), clock) == (
        notify.RETRY_AFTER_MAX_SECONDS
    )
    assert notify._retry_after(_headers(**{"Retry-After": "soon"}), clock) == 0.0
    assert notify._retry_after(_headers(), clock) == 0.0


def test_the_response_body_is_never_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> requests.Response:
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(b"never read")
        return response

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert WebhookNotifier("ops", "https://receiver.invalid/hook").send({}, delivery_id="d").status == "delivered"
    assert calls[0]["stream"] is True
