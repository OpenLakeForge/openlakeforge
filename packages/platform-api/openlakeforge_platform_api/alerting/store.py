"""Alert objects in the ops bucket: the state document, history records and a kept copy of a corrupt state.

Layout under the base prefix (ADR 0023, decision 3): `state/v1/state.json`, `state/v1/corrupt/<time>.json`,
`state/v1/pending-history.json` and `history/v1/date=<day>/<name>.json`. Nothing here takes a webhook URL, so
none can reach an object.
"""

from __future__ import annotations

import hashlib
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
# The state document, under the base prefix. The platform API reads it too, never through this class: read_state writes.
STATE_OBJECT = ("state", "v1", "state.json")


class AlertObjectStore(ObjectStore, Protocol):
    """The reads of the log archive plus the one write this evaluator adds; boto3's S3 client satisfies it."""

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> Any: ...  # noqa: N803


# (UTC day of the event, history object name, record): replayable without the evaluation that produced it.
PendingRecord = tuple[str, str, dict[str, Any]]


def state_digest(state: AlertState) -> str:
    return hashlib.sha256(state_to_json(state).encode()).hexdigest()


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
        key = self._key(*STATE_OBJECT)
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

    def read_object(self, key: str) -> bytes | None:
        """An object of the state bucket by its full key, or None when it does not exist."""
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise StoreError(f"{key} could not be read: {error}") from error
        except BotoCoreError as error:
            raise StoreError(f"{key} could not be read: {error}") from error

    def read_policy_cache(self, stage: str) -> bytes | None:
        return self.read_object(self._key("state", "v1", "policy", f"{safe_name(stage)}.json"))

    def write_policy_cache(self, stage: str, body: bytes) -> None:
        self._put(self._key("state", "v1", "policy", f"{safe_name(stage)}.json"), body)

    def write_state(self, state: AlertState, *, evaluated_at: datetime) -> None:
        """The state, stamped with `evaluated_at` so a quiet but working evaluator is told from a stopped one.

        The stamp sits beside the model's document (its reader ignores unknown keys), so it changes neither the
        state nor its digest.
        """
        document = json.loads(state_to_json(state))
        document["evaluated_at"] = evaluated_at.isoformat()
        body = json.dumps(document, sort_keys=True, separators=(",", ":"))
        self._put(self._key(*STATE_OBJECT), body.encode())

    def write_pending_history(self, records: list[PendingRecord], state: AlertState | None) -> None:
        """The transition records of a state about to be written, tagged with that state's digest; [] clears them."""
        body = {"state": state_digest(state) if state is not None else None, "records": records}
        self._put(self._key("state", "v1", "pending-history.json"), json.dumps(body, sort_keys=True).encode())

    def read_pending_history(self, stored: AlertState) -> list[PendingRecord]:
        """Records to replay: only those written for the state that is actually stored, else none.

        Records tagged with another state belong to a state write that failed: replaying them would record
        transitions that never happened (their incident ids were minted at a time that never committed).
        """
        key = self._key("state", "v1", "pending-history.json")
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _MISSING:
                return []
            raise StoreError(f"pending alert history could not be read: {error}") from error
        except BotoCoreError as error:
            raise StoreError(f"pending alert history could not be read: {error}") from error
        try:
            pending = json.loads(body)
            records = pending["records"]
            if pending["state"] != state_digest(stored) or not isinstance(records, list):
                return []
            return [(str(at), str(name), dict(record)) for at, name, record in records]
        except (ValueError, KeyError, TypeError) as error:
            log.warning("pending alert history is unreadable (%s); dropping it", error)
            return []

    def write_history_at(self, day: str, name: str, record: dict[str, Any]) -> None:
        key = self._key("history", "v1", f"date={day}", f"{safe_name(name)}.json")
        self._put(key, json.dumps(record, sort_keys=True, separators=(",", ":")).encode())

    def write_history(self, at: datetime, name: str, record: dict[str, Any]) -> None:
        key = self._key("history", "v1", f"date={at:%Y-%m-%d}", f"{safe_name(name)}.json")
        self._put(key, json.dumps(record, sort_keys=True, separators=(",", ":")).encode())

    def _put(self, key: str, body: bytes) -> None:
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/json")
        except (ClientError, BotoCoreError) as error:
            raise StoreError(f"{key} could not be written: {error}") from error
