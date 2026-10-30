"""A stand-in for GitHub Enterprise Server, for Dex's `github` connector (#384).

Only the endpoints Dex v2.46.0 calls with `hostName` set are served. This file
is also the program the e2e pod runs (`python3 -c`), so it imports nothing
outside the standard library.

There is no sign-in page: the e2e, playing the browser, answers the authorize
redirect itself with `code=<login>`, and the code is the identity. A user is a
login mapped to `<organization>:<team-slug>` groups, read from STUB_USERS.
"""

from __future__ import annotations

import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

API = "/api/v3"


def make_handler(users: dict[str, list[str]]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: object = None) -> None:
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _login(self) -> str | None:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").removeprefix("tok-")
            return token if token in users else None

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/login/oauth/access_token":
                return self._send(404)
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            code = form.get("code", [""])[0]
            # Dex sends the OAuth App credentials in the form or as Basic auth; either must be present.
            has_credentials = self.headers.get("Authorization", "").startswith("Basic ") or (
                form.get("client_id", [""])[0] and form.get("client_secret", [""])[0]
            )
            if not has_credentials:
                return self._send(401, {"error": "incorrect_client_credentials"})
            if code not in users:
                return self._send(400, {"error": "bad_verification_code"})
            self._send(200, {"access_token": f"tok-{code}", "token_type": "bearer"})

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            login = self._login()
            if login is None:
                return self._send(401, {"message": "Bad credentials"})
            if path == f"{API}/user":
                user = {"login": login, "id": sorted(users).index(login) + 1, "name": login}
                return self._send(200, user | {"email": f"{login}@example.invalid"})
            if path == f"{API}/user/teams":
                teams = [g.split(":", 1) for g in users[login]]
                return self._send(200, [{"name": s, "slug": s, "organization": {"login": o}} for o, s in teams])
            if path.startswith(f"{API}/orgs/") and path.endswith(f"/members/{login}"):
                return self._send(204)
            self._send(404)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8443), make_handler(json.loads(os.environ["STUB_USERS"])))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
