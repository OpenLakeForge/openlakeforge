"""A stand-in for an alert webhook receiver, for the alert evaluator's e2e (#1173).

This file is also the program the e2e pod runs (`python3 -c`), so it imports nothing outside the standard library.

`POST /hook` records the request and answers the configured status (200 until `PUT /_status/<code>` says otherwise).
The e2e reads and clears what was recorded with `GET` and `DELETE /_records`, from inside the pod: the listener has no
authentication, and a NetworkPolicy lets only the evaluator reach it.
"""

from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HOOK = "/hook"
PORT = 8443
# The only headers kept: the webhook contract names these two (ADR 0023, decision 4), and a recorded Authorization
# header would be a place for a credential to leak into the e2e's output.
KEPT_HEADERS = ("content-type", "idempotency-key")


class Receiver:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.status = 200
        self.lock = threading.Lock()


def make_handler(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: object = None) -> None:
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode(errors="replace")
            if self.path != HOOK:
                return self._send(404)
            with receiver.lock:
                receiver.records.append(
                    {"headers": {name: self.headers.get(name, "") for name in KEPT_HEADERS}, "body": body}
                )
                status = receiver.status
            self._send(status)

        def do_GET(self) -> None:
            if self.path != "/_records":
                return self._send(404)
            with receiver.lock:
                self._send(200, receiver.records)

        def do_DELETE(self) -> None:
            if self.path != "/_records":
                return self._send(404)
            with receiver.lock:
                receiver.records.clear()
            self._send(204)

        def do_PUT(self) -> None:
            code = self.path.removeprefix("/_status/")
            if code == self.path or not code.isdigit() or not 200 <= int(code) <= 599:
                return self._send(400)
            with receiver.lock:
                receiver.status = int(code)
            self._send(204)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), make_handler(Receiver()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
