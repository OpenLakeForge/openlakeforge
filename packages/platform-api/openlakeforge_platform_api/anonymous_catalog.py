"""The Polaris token the anonymous principal's queries carry (ADR 0021, decision 8).

With authentication off there is no person's token, and the stage catalogs are keyless (ADR 0019, section 9), so
the API mints one for the Polaris principal `platform-api-anonymous`. Its credentials are files in a Secret volume
that olf's Polaris bootstrap fills after the pod starts, so they are read when a token is needed, never at start.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import requests
from fastapi import HTTPException

DIRECTORY = Path("/anonymous-catalog")
CLIENT_ID, CLIENT_SECRET = "POLARIS_ANONYMOUS_CLIENT_ID", "POLARIS_ANONYMOUS_CLIENT_SECRET"
# Renewed this long before Polaris's expiry, so a query never starts with a token that dies mid-request.
MARGIN_SECONDS = 60


class AnonymousCatalogToken:
    def __init__(
        self,
        token_url: str,
        *,
        directory: Path = DIRECTORY,
        post: Callable[..., requests.Response] = requests.post,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url, self._directory, self._post, self._clock = token_url, directory, post, clock
        self._lock = threading.Lock()
        self._token, self._expires = "", 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires:
                return self._token
            try:
                client_id = (self._directory / CLIENT_ID).read_text().strip()
                secret = (self._directory / CLIENT_SECRET).read_text().strip()
            except OSError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="the anonymous catalog credential is not issued yet; rerun `olf infra deploy`",
                ) from exc
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
                "scope": "PRINCIPAL_ROLE:ALL",
            }
            try:
                response = self._post(self._url, data=data, timeout=15)
                response.raise_for_status()
                body = response.json()
                token = str(body["access_token"])
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                raise HTTPException(status_code=503, detail="the catalog did not issue the anonymous token") from exc
            self._token = token
            self._expires = self._clock() + max(int(body.get("expires_in", 3600)) - MARGIN_SECONDS, 0)
            return token
