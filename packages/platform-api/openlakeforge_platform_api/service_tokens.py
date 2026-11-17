"""A service principal's token: a pod's projected ServiceAccount token, checked with a TokenReview.

ADR 0021, decision 3. The API accepts no other service credential. The Kubernetes API server decides whether the token
is genuine, unexpired and issued for `service_audience`; this module decides whether the ServiceAccount it names is
one the settings admit. Every failure to decide is a refusal: an unreachable API server never admits a caller.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import jwt
import requests

from openlakeforge_platform_api.settings import Settings
from openlakeforge_platform_api.tokens import InvalidToken, Principal

_SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_REVIEW_URL = "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
REVIEW_TIMEOUT = 5.0

# Asks the API server about `token` for `audience`; returns the review's `status`. Raises ReviewUnavailable.
TokenReview = Callable[[str, str], Mapping[str, Any]]


class ReviewUnavailable(Exception):
    """The API server could not answer: the caller is refused with 503, never accepted."""


def in_cluster_review(token: str, audience: str) -> Mapping[str, Any]:
    """POST a TokenReview as the API's own ServiceAccount (the one rule that allows it is `tokenreviews` `create`)."""
    try:
        # Read per call: the kubelet rotates the projected token.
        own = (_SERVICE_ACCOUNT / "token").read_text().strip()
        response = requests.post(
            _REVIEW_URL,
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": token, "audiences": [audience]},
            },
            headers={"Authorization": f"Bearer {own}", "Accept": "application/json"},
            verify=str(_SERVICE_ACCOUNT / "ca.crt"),
            timeout=REVIEW_TIMEOUT,
        )
        if not response.ok:
            raise ReviewUnavailable(f"the Kubernetes API answered HTTP {response.status_code} to a TokenReview")
        status = response.json().get("status")
    except (OSError, ValueError, requests.RequestException) as exc:
        raise ReviewUnavailable(f"the Kubernetes API could not review the token: {type(exc).__name__}") from exc
    if not isinstance(status, dict):
        raise ReviewUnavailable("the TokenReview carried no status")
    return status


class ServiceTokenValidator:
    def __init__(self, settings: Settings, review: TokenReview = in_cluster_review) -> None:
        self._audience = settings.service_audience
        self._callers = {
            f"system:serviceaccount:{caller.namespace}:{caller.service_account}": name
            for name, caller in settings.service_callers.items()
        }
        self._issuer = settings.issuer_url
        self._review = review

    @property
    def enabled(self) -> bool:
        return bool(self._callers)

    def is_service_token(self, token: str) -> bool:
        """Whether the token is not an issuer token, by its unverified `iss`. Only routes it; neither path trusts it.

        A token that is not a JWT, or names the issuer, is the issuer's path: it fails there. So the issuer's tokens
        are never sent to the API server, and a service token never depends on the issuer being up.
        """
        if not self.enabled:
            return False
        try:
            issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError:
            return False
        return issuer != self._issuer

    def validate(self, token: str) -> Principal:
        status = self._review(token, self._audience)
        if status.get("authenticated") is not True:
            raise InvalidToken("the token was not authenticated by the Kubernetes API")
        audiences = status.get("audiences")
        if not isinstance(audiences, list) or self._audience not in audiences:
            raise InvalidToken("the token was not issued for this API")
        user = status.get("user")
        username = user.get("username") if isinstance(user, dict) else None
        service = self._callers.get(username) if isinstance(username, str) else None
        if service is None:
            raise InvalidToken("the ServiceAccount is not a service caller of this API")
        return Principal(
            subject=str(username), role=None, email=None, name=service, audience=self._audience, service=service
        )
