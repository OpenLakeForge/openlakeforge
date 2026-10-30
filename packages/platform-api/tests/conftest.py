"""A fake issuer on loopback that publishes discovery and keys, and a token minter signed by it."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings

KID = "issuer-key"
AUDIENCE = "olf-cli"
PRECEDENCE = ("platform-admin", "data-engineer", "analyst", "viewer")
# The shape the Dex adapter renders for GitHub teams (ADR 0014): <org>:<slug>.
ROLE_MAPPING = {role: (f"acme:{role}s",) for role in PRECEDENCE}

Mint = Callable[..., str]

# Generated once: a 2048-bit key per test made the suite take half a minute.
ISSUER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeIssuer:
    def __init__(self) -> None:
        self.key = ISSUER_KEY
        self.requests = 0
        self.down = False
        # The issuer the discovery document claims; None claims the real URL.
        self.advertised: str | None = None
        # False withdraws the signing key from the published set, as a rotation after a compromise would.
        self.publishes_key = True
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dex"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's handler name
                issuer.requests += 1
                if issuer.down:
                    self.send_error(503)
                    return
                if self.path == "/dex/.well-known/openid-configuration":
                    body: dict[str, Any] = {"issuer": issuer.advertised or issuer.url, "jwks_uri": f"{issuer.url}/keys"}
                elif self.path == "/dex/keys":
                    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(issuer.key.public_key()))
                    keys = [{**public, "kid": KID, "use": "sig", "alg": "RS256"}] if issuer.publishes_key else []
                    body = {"keys": keys}
                else:
                    self.send_error(404)
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        return Handler

    def mint(self, *, key: Any = None, kid: str = KID, algorithm: str = "RS256", **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.url,
            "aud": AUDIENCE,
            "sub": "user-1",
            "iat": now,
            "exp": now + 300,
            "email": "ada@example.com",
            "preferred_username": "ada",
            "groups": ["acme:viewers"],
        }
        claims.update(overrides)
        claims = {name: value for name, value in claims.items() if value is not None}
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers={"kid": kid})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def issuer() -> Iterator[FakeIssuer]:
    fake = FakeIssuer()
    yield fake
    fake.close()


@pytest.fixture
def settings(issuer: FakeIssuer) -> Settings:
    return Settings.from_mapping(
        {
            "issuer_url": issuer.url,
            "audiences": [AUDIENCE],
            "role_claim": "groups",
            "role_mapping": {role: list(values) for role, values in ROLE_MAPPING.items()},
            "precedence": list(PRECEDENCE),
            "stages": ["dev", "prod"],
        }
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def mint(issuer: FakeIssuer) -> Mint:
    return issuer.mint


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
