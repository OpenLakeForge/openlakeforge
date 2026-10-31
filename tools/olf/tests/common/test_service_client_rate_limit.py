"""The shared service client waits out a 429 instead of failing the operation it is part of."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from openlakeforge_services import base
from openlakeforge_services.base import JsonHttpClient, ServiceClientError


class Limited(ThreadingHTTPServer):
    """Answers 429 to the first `refusals` requests, with `retry_after` when set, then 200."""

    def __init__(self) -> None:
        self.refusals = 0
        self.retry_after: str | None = None
        self.requests: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                server.requests.append(self.command)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if server.refusals:
                    server.refusals -= 1
                    self.send_response(429)
                    if server.retry_after is not None:
                        self.send_header("Retry-After", server.retry_after)
                    payload = b'{"message": "429 Too Many Requests: 50 per 1 second"}'
                else:
                    self.send_response(200)
                    payload = json.dumps({"ok": True}).encode()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer  # noqa: N815 - BaseHTTPRequestHandler API

            def log_message(self, *args: object) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)


@pytest.fixture
def limited(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Limited, list[float]]]:
    waits: list[float] = []
    monkeypatch.setattr(base.time, "sleep", waits.append)
    server = Limited()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, waits
    server.shutdown()


def _client(server: Limited) -> JsonHttpClient:
    return JsonHttpClient(f"http://127.0.0.1:{server.server_port}")


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_rate_limited_request_is_sent_again_after_a_wait(limited: Any, method: str) -> None:
    server, waits = limited
    server.refusals = 2

    assert _client(server).request(method, "/api/v1/dataset/6", json_body={} if method == "POST" else None) == {
        "ok": True
    }
    assert server.requests == [method] * 3 and waits == [1.0, 1.0]


@pytest.mark.parametrize(("header", "wait"), [("2", 2.0), ("60", base.RATE_LIMIT_MAX_WAIT_SECONDS), ("soon", 1.0)])
def test_the_wait_is_the_services_retry_after_capped(limited: Any, header: str, wait: float) -> None:
    server, waits = limited
    server.refusals, server.retry_after = 1, header

    _client(server).request("GET", "/x")

    assert waits == [wait]


def test_a_service_that_keeps_refusing_fails_with_its_429(limited: Any) -> None:
    server, waits = limited
    server.refusals = base.RATE_LIMIT_RETRIES + 1

    with pytest.raises(ServiceClientError, match="HTTP 429") as raised:
        _client(server).request("GET", "/x")

    assert raised.value.status == 429 and len(server.requests) == base.RATE_LIMIT_RETRIES + 1
    assert len(waits) == base.RATE_LIMIT_RETRIES
