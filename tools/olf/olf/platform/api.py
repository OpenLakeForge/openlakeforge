"""The platform API as the platform commands' one backend (ADR 0021): an authenticated GET, its refusals explained."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

from olf.platform import session as sessions


def base_url(instance: sessions.Instance) -> str:
    """The API answers under /api on the portal's host, the deployment's base domain, with the issuer's scheme."""
    scheme = urlsplit(instance.issuer).scheme or "https"
    return f"{scheme}://{instance.base_domain}/api"


def _detail(response: requests.Response) -> str:
    """The API's own explanation, which names the role a refusal needs; empty when the answer is not the API's."""
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else ""


def get(instance: sessions.Instance, path: str, params: Mapping[str, Any] | None = None) -> Any:
    url = f"{base_url(instance)}{path}"
    headers = {"Accept": "application/json"}
    token = sessions.bearer_token(instance)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = sessions.send("GET", url, headers=headers, params=dict(params or {}), timeout=60)
    except requests.exceptions.SSLError as exc:
        raise sessions.PlatformError(
            f"cannot verify the TLS certificate of {instance.base_domain}. A local deployment uses its own CA: run "
            "`olf infra access trust`, then set REQUESTS_CA_BUNDLE to the certificate file it writes"
        ) from exc
    except requests.RequestException as exc:
        raise sessions.PlatformError(f"cannot reach the platform API at {url}: {type(exc).__name__}") from exc
    detail = _detail(response)
    if response.status_code == 401:
        raise sessions.PlatformError(f"the platform API refused your session ({detail or 'HTTP 401'}); run `olf login`")
    if response.status_code == 403:
        # A refusal that is not the API's JSON came from the perimeter in front of it, most often for a person whose
        # teams map to no role at all; its other middlewares are not ruled out, so the message says "most often".
        raise sessions.PlatformError(
            f"the platform API refused this: {detail}"
            if detail
            else "the perimeter refused the request (HTTP 403); most often your GitHub teams map to no OpenLakeForge "
            "role, which a platform-admin assigns"
        )
    if response.status_code == 404 and not detail:
        raise sessions.PlatformError(
            f"{instance.base_domain} serves no platform API at {url}; it is deployed with sign-in "
            "(spec.identity) and the platform API image"
        )
    if response.status_code != 200:
        raise sessions.PlatformError(f"the platform API answered HTTP {response.status_code}: {detail or 'no detail'}")
    try:
        return response.json()
    except ValueError as exc:
        raise sessions.PlatformError(f"the platform API answered {url} without JSON") from exc
