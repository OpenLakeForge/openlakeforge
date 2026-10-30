"""Bearer-token validation against the issuer's published keys (ADR 0021, decision 3)."""

from __future__ import annotations

import json
import ssl
import threading
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from openlakeforge_platform_api.settings import Settings

# Asymmetric algorithms only. Accepting HS* would let anyone holding the public
# key mint a token, and `none` would accept an unsigned one.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


class InvalidToken(Exception):
    """The token is not one this API accepts: answered with 401."""


class IssuerUnavailable(Exception):
    """The issuer's discovery document or keys could not be read: answered with 503, never as an accepted token."""


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str | None
    email: str | None
    name: str | None
    audience: str


def canonical_role(claim: Any, role_mapping: Mapping[str, tuple[str, ...]], precedence: tuple[str, ...]) -> str | None:
    """The highest canonical role whose mapped claim values the token carries, or None.

    ADR 0014: several roles are a union, and grants are monotonic, so the union
    is the highest held role. A claim may be one string or a list of them.
    """
    values = {claim} if isinstance(claim, str) else {value for value in claim or () if isinstance(value, str)}
    for role in precedence:
        if values & set(role_mapping.get(role, ())):
            return role
    return None


class TokenValidator:
    """Checks signature, issuer, expiry and audience, then derives the canonical role.

    The issuer's keys are found through its discovery document on first use, so
    the API starts while the issuer is still coming up. Thread-safe: FastAPI
    calls it from its worker threads.
    """

    def __init__(self, settings: Settings, *, timeout: float = 10.0, jwks_lifespan: float = 300.0) -> None:
        self._settings = settings
        self._timeout = timeout
        self._jwks_lifespan = jwks_lifespan
        self._ssl_context = ssl.create_default_context()
        # Added to the system roots, not instead of them: a local CA signs
        # Dex's route, a public CA an external issuer's.
        if settings.ca_file:
            self._ssl_context.load_verify_locations(cafile=settings.ca_file)
        self._lock = threading.Lock()
        self._jwks: jwt.PyJWKClient | None = None

    def _discover(self) -> jwt.PyJWKClient:
        with self._lock:
            if self._jwks is not None:
                return self._jwks
            url = f"{self._settings.issuer_url.rstrip('/')}/.well-known/openid-configuration"
            try:
                with urllib.request.urlopen(url, timeout=self._timeout, context=self._ssl_context) as response:
                    document = json.load(response)
            except (OSError, ValueError) as exc:
                raise IssuerUnavailable(f"cannot read {url}: {exc}") from exc
            # OIDC Discovery 1.0 section 4.3: the document must name the
            # issuer it was fetched for, or its keys are not that issuer's.
            if document.get("issuer") != self._settings.issuer_url or not document.get("jwks_uri"):
                raise IssuerUnavailable(f"{url} does not describe issuer {self._settings.issuer_url}")
            # Only the key set is cached, and it expires: PyJWT's per-key cache
            # (cache_keys) never does, so a key the issuer withdrew after a
            # compromise would keep validating tokens until a restart.
            self._jwks = jwt.PyJWKClient(
                document["jwks_uri"],
                lifespan=self._jwks_lifespan,
                timeout=self._timeout,
                ssl_context=self._ssl_context,
            )
            return self._jwks

    def validate(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken("malformed token") from exc
        if header.get("alg") not in ALGORITHMS:
            raise InvalidToken(f"algorithm {header.get('alg')!r} is not accepted")
        try:
            key = self._discover().get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError as exc:
            raise IssuerUnavailable(str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise InvalidToken("no issuer key matches the token") from exc
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                issuer=self._settings.issuer_url,
                audience=list(self._settings.audiences),
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        audiences = [claims["aud"]] if isinstance(claims["aud"], str) else list(claims["aud"])
        return Principal(
            subject=str(claims["sub"]),
            role=canonical_role(
                claims.get(self._settings.role_claim), self._settings.role_mapping, self._settings.precedence
            ),
            email=claims.get("email"),
            name=claims.get("preferred_username") or claims.get("name"),
            audience=next(audience for audience in audiences if audience in self._settings.audiences),
        )
