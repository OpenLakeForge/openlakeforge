"""The Prometheus HTTP API client against a loopback server that answers as Prometheus 3 does."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from openlakeforge_services.base import ServiceClientError, TransientServiceError
from openlakeforge_services.prometheus import PrometheusClient, PrometheusError, PrometheusSeries


class FakePrometheus(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.status = 200
        self.body: Any = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        self.requests: list[tuple[str, dict[str, list[str]]]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's handler name
                parts = urlsplit(self.path)
                server.requests.append((parts.path, parse_qs(parts.query)))
                payload = server.body if isinstance(server.body, bytes) else json.dumps(server.body).encode()
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@pytest.fixture
def prometheus() -> Iterator[FakePrometheus]:
    server = FakePrometheus()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_a_range_query_sends_the_window_and_parses_each_line(prometheus: FakePrometheus) -> None:
    prometheus.body = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"stage": "dev", "component": "trino"}, "values": [[1700000060, "0.25"], [1700000120, "1"]]}
            ],
        },
    }

    lines = PrometheusClient(prometheus.url).query_range(
        'olf:cpu_usage_cores{stage="dev"}', start=1700000060, end=1700000120, step=60
    )

    assert lines == [
        PrometheusSeries({"stage": "dev", "component": "trino"}, [(1700000060.0, 0.25), (1700000120.0, 1.0)])
    ]
    ((path, params),) = prometheus.requests
    assert path == "/api/v1/query_range"
    assert params == {
        "query": ['olf:cpu_usage_cores{stage="dev"}'],
        "start": ["1700000060"],
        "end": ["1700000120"],
        "step": ["60"],
    }


def test_an_instant_query_gives_one_sample_per_series(prometheus: FakePrometheus) -> None:
    prometheus.body = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {"job": "trino"}, "value": [1700000000, "0"]}]},
    }

    assert PrometheusClient(prometheus.url).query("up", time=1700000000) == [
        PrometheusSeries({"job": "trino"}, [(1700000000.0, 0.0)])
    ]
    assert prometheus.requests[0][0] == "/api/v1/query"


@pytest.mark.parametrize("raw", ["NaN", "+Inf", "-Inf"])
def test_a_value_json_cannot_carry_is_none_never_zero(prometheus: FakePrometheus, raw: str) -> None:
    prometheus.body = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{"metric": {}, "values": [[1700000000, raw]]}]},
    }

    (line,) = PrometheusClient(prometheus.url).query_range("x", start=1700000000, end=1700000000, step=60)

    assert line.samples == [(1700000000.0, None)]


def test_a_refused_query_raises_with_prometheus_reason(prometheus: FakePrometheus) -> None:
    prometheus.status = 400
    prometheus.body = {"status": "error", "errorType": "bad_data", "error": "parse error"}

    with pytest.raises(ServiceClientError, match="parse error") as raised:
        PrometheusClient(prometheus.url).query("(", time=1700000000)

    assert not isinstance(raised.value, TransientServiceError)


def test_a_failing_prometheus_is_transient(prometheus: FakePrometheus) -> None:
    prometheus.status = 503
    prometheus.body = b"Service Unavailable"

    with pytest.raises(TransientServiceError):
        PrometheusClient(prometheus.url).query("up", time=1700000000)


def test_an_unreachable_prometheus_is_transient() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with pytest.raises(TransientServiceError):
        PrometheusClient(f"http://127.0.0.1:{port}", timeout=1).query("up", time=1700000000)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<html>a proxy page</html>", "answered None"),
        ({"status": "success", "data": {"resultType": "vector", "result": []}}, "expected a matrix result"),
        ({"status": "success", "data": {"resultType": "matrix", "result": [{"values": []}]}}, "malformed"),
        (
            {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": [[1]]}]}},
            "malformed",
        ),
    ],
)
def test_an_answer_that_is_not_a_range_result_is_an_error_not_an_empty_chart(
    prometheus: FakePrometheus, body: Any, message: str
) -> None:
    prometheus.body = body

    with pytest.raises(PrometheusError, match=message):
        PrometheusClient(prometheus.url).query_range("x", start=1700000000, end=1700000060, step=60)
