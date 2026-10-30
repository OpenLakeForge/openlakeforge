"""The signed-in instance and its on-disk session (ADR 0020)."""

from __future__ import annotations

import base64
import json
import os
import socket
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from olf.common.distribution import runtime_layout
from olf.common.profile import load_deployment_profile
from olf.common.toolchain.manager import default_home

CLI_CLIENT = "olf-cli"
# Dex serves the CLI's public client with PKCE, so no secret exists to protect.
SCOPES = "openid email profile groups offline_access"
# Refresh a little early so a token does not expire between the check and the request.
_LEEWAY_SECONDS = 30


class PlatformError(Exception):
    """A user-facing failure. Messages never carry a token."""


@dataclass(frozen=True)
class Instance:
    base_domain: str
    issuer: str
    client_id: str
    # The Dex adapter (`identity.issuer: github`), which serves people's data access (ADR 0019).
    dex: bool = False

    @property
    def anonymous(self) -> bool:
        return not self.issuer


@dataclass(frozen=True)
class Session:
    issuer: str
    id_token: str
    access_token: str
    refresh_token: str


def resolve_instance(profile_file: str) -> Instance:
    """The issuer and client id from the project's Deployment Profile; there is no discovery document."""
    try:
        profile = load_deployment_profile(profile_file, distribution_root=runtime_layout().distribution_root)
    except (OSError, ValueError) as exc:
        raise PlatformError(f"cannot read the Deployment Profile {profile_file}: {exc}") from exc
    access, identity = profile.access, profile.identity
    if access is None:
        raise PlatformError(
            "this deployment publishes no routes, so platform commands have nothing to reach; "
            "use the `olf infra` commands, which run with cluster access"
        )
    if identity is None:
        return Instance(access.base_domain, "", "")  # authentication is off: platform commands run anonymously
    # access.issuer 'none' turns off route TLS only; identity decides whether sign-in exists.
    scheme = "http" if access.issuer == "none" else "https"
    issuer = identity.issuer_url if identity.issuer == "external" else f"{scheme}://auth.{access.base_domain}"
    assert issuer is not None  # validated: issuer 'external' requires issuer_url
    client_id = (identity.client_ids or {}).get(CLI_CLIENT, CLI_CLIENT)
    return Instance(access.base_domain, issuer, client_id, dex=identity.issuer == "github")


def verify() -> bool | str:
    """TLS verification. `requests` ignores SSL_CERT_FILE, so honour it here for the local CA."""
    return os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True


def send(method: str, url: str, **kwargs: Any) -> requests.Response:
    """A request that reaches `*.localhost` issuers: RFC 6761 says loopback, but glibc and macOS often answer
    NXDOMAIN, and a proxy chosen from the hostname would carry the request away from loopback."""
    host = urlsplit(url).hostname or ""
    local = host.endswith(".localhost")
    resolver = socket.getaddrinfo

    def getaddrinfo(name: Any, *args: Any, **rest: Any) -> Any:
        return resolver("127.0.0.1" if local and name == host else name, *args, **rest)

    previous = os.environ.get("NO_PROXY")
    if local:
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]  # typeshed pins the stdlib signature
        os.environ["NO_PROXY"] = ",".join(filter(None, [host, previous]))
    try:
        return requests.request(method, url, verify=verify(), **kwargs)
    finally:
        socket.getaddrinfo = resolver
        if previous is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = previous


def discover(instance: Instance) -> Mapping[str, Any]:
    try:
        response = send("GET", f"{instance.issuer.rstrip('/')}/.well-known/openid-configuration", timeout=15)
        response.raise_for_status()
        document = response.json()
    except requests.exceptions.SSLError as exc:
        raise PlatformError(
            f"cannot verify the TLS certificate of {instance.issuer}. A local deployment uses its own CA: run "
            "`olf infra access trust`, then set REQUESTS_CA_BUNDLE to the certificate file it writes"
        ) from exc
    except (requests.RequestException, ValueError) as exc:
        raise PlatformError(f"cannot reach the issuer {instance.issuer}: {type(exc).__name__}") from exc
    if not isinstance(document, dict) or not {"authorization_endpoint", "token_endpoint"} <= document.keys():
        raise PlatformError(f"{instance.issuer} did not return an OpenID Connect discovery document")
    return document


def claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, for display and expiry only: the services validate the signature."""
    try:
        payload = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise PlatformError("the stored session is damaged; run `olf login`") from exc
    if not isinstance(decoded, dict):
        raise PlatformError("the stored session is damaged; run `olf login`")
    return decoded


def session_path(instance: Instance) -> Path:
    # ponytail: a 0600 file is the v0.6 ceiling; the OS keyring is the follow-up (#536).
    return default_home() / "sessions" / f"{instance.base_domain}.json"


def save(instance: Instance, session: Session) -> None:
    path = session_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)  # an older, wider file keeps its mode through O_CREAT
        json.dump(asdict(session), handle)


def _load(instance: Instance) -> Session:
    try:
        session = Session(**json.loads(session_path(instance).read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise PlatformError(f"not logged in to {instance.base_domain}; run `olf login`") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise PlatformError("the stored session is damaged; run `olf login`") from exc
    if session.issuer != instance.issuer:
        raise PlatformError(f"the stored session is for another issuer; run `olf login` for {instance.base_domain}")
    return session


def active_session(instance: Instance) -> Session:
    """The stored session, refreshed when its ID token has expired (Dex: 5 minutes)."""
    session = _load(instance)
    if claims(session.id_token).get("exp", 0) - _LEEWAY_SECONDS > time.time():
        return session
    expired = PlatformError(f"the session for {instance.base_domain} has expired; run `olf login`")
    if not session.refresh_token:
        raise expired
    try:
        response = send(
            "POST",
            str(discover(instance)["token_endpoint"]),
            data={
                "grant_type": "refresh_token",
                "refresh_token": session.refresh_token,
                "client_id": instance.client_id,
            },
            timeout=15,
        )
        tokens = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PlatformError(f"cannot refresh the session: {type(exc).__name__}") from exc
    if response.status_code != 200 or "id_token" not in tokens:
        raise expired
    refreshed = Session(
        instance.issuer,
        tokens["id_token"],
        tokens.get("access_token", ""),
        tokens.get("refresh_token", session.refresh_token),
    )
    save(instance, refreshed)
    return refreshed


def delete(instance: Instance) -> bool:
    try:
        session_path(instance).unlink()
    except FileNotFoundError:
        return False
    return True


def bearer_token(instance: Instance) -> str | None:
    """The token a platform command sends as `Authorization: Bearer`, or None on a deployment without sign-in.

    Never falls back to a service or bot credential."""
    return None if instance.anonymous else active_session(instance).id_token
