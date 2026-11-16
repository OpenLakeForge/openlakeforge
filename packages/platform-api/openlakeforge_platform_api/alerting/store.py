"""Alert objects in the ops bucket: the state document, history records and a kept copy of a corrupt state.

Layout under the base prefix (ADR 0023, decision 3): `state/v1/state.json`, `state/v1/corrupt/<time>.json` and
`history/v1/date=<day>/<name>.json`. Nothing here takes a webhook URL, so none can reach an object.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from openlakeforge_core.alerts import AlertState
from openlakeforge_core.alerts_state import AlertStateError, state_from_json, state_to_json
from openlakeforge_services.logs import ObjectStore

log = logging.getLogger(__name__)

_MISSING = frozenset({"NoSuchKey", "404"})


class AlertObjectStore(ObjectStore, Protocol):
    """The reads of the log archive plus the one write this evaluator adds; boto3's S3 client satisfies it."""

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> Any: ...  # noqa: N803


class StoreError(Exception):
    """The bucket cannot be read or written. The evaluator delivers nothing and exits non-zero."""


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


class AlertStore:
    def __init__(self, client: AlertObjectStore, base_uri: str, *, prefix: str | None = None) -> None:
        parts = urlsplit(base_uri)
        if parts.scheme != "s3" or not parts.netloc:
            raise ValueError(f"expected an s3:// state base URI, got {base_uri!r}")
        self._client = client
        self._bucket = parts.netloc
        # `prefix` replaces the base's, so the e2e keeps its objects out of the real `alerts/`.
        self._prefix = (parts.path if prefix is None else prefix).strip("/")

    def _key(self, *parts: str) -> str:
        return "/".join(part for part in (self._prefix, *parts) if part)

    def read_state(self, now: datetime) -> AlertState:
        """The stored state; an empty one when it is missing, or when it is corrupt after keeping a copy of it."""
        key = self._key("state", "v1", "state.json")
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _MISSING:
                return AlertState()
            raise StoreError(f"alert state could not be read: {error}") from error
        except BotoCoreError as error:
            raise StoreError(f"alert state could not be read: {error}") from error
        try:
            return state_from_json(body.decode())
        except (AlertStateError, UnicodeDecodeError) as error:
            copy = self._key("state", "v1", "corrupt", f"{now:%Y%m%dT%H%M%SZ}.json")
            log.warning("alert state is unreadable (%s); keeping a copy at %s and starting empty", error, copy)
            self._put(copy, body)
            return AlertState()

    def write_state(self, state: AlertState) -> None:
        self._put(self._key("state", "v1", "state.json"), state_to_json(state).encode())

    def write_history(self, at: datetime, name: str, record: dict[str, Any]) -> None:
        key = self._key("history", "v1", f"date={at:%Y-%m-%d}", f"{safe_name(name)}.json")
        self._put(key, json.dumps(record, sort_keys=True, separators=(",", ":")).encode())

    def _put(self, key: str, body: bytes) -> None:
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/json")
        except (ClientError, BotoCoreError) as error:
            raise StoreError(f"{key} could not be written: {error}") from error
