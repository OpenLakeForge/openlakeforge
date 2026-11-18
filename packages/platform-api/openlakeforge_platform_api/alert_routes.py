"""A stage's open alert incidents and their notification delivery (#1258), read from the evaluator's state.

The evaluator is the only writer of `alerts/` (ADR 0023, decision 3), and this route never writes: a state that does not
parse is answered `unavailable` and left for the evaluator, which keeps the corrupt copy, rather than through
`AlertStore.read_state`, which would write that copy itself and answer an empty state. An answer carries the fields of
the notification payload allowlist and delivery counts only: no receiver name, URL or header, and no link.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Request
from openlakeforge_core.alerts import AlertState, Incident, OutboxEntry
from openlakeforge_core.alerts_state import AlertStateError, notification_order, state_from_json

from openlakeforge_platform_api.alerting.store import STATE_OBJECT
from openlakeforge_platform_api.models import AlertIncident, AlertStateStatus, StageAlertIncidents

log = logging.getLogger(__name__)

# The evaluator's CronJob runs every 5 minutes (modules/observability/alerts). A state older than two runs means one
# was missed and the evaluator may have stopped, which must not read as "no incidents".
STALE_AFTER = timedelta(minutes=10)
# The read, connect and body included. The client's own timeouts end a stalled call; this ends the request.
READ_DEADLINE_SECONDS = 10.0
# The state holds open incidents and a bounded outbox; anything this large is not one the evaluator wrote.
MAX_STATE_BYTES = 16 * 1024 * 1024
_MISSING = frozenset({"NoSuchKey", "404"})
_READS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alert-state-read")

StateName = Literal["available", "unavailable", "not_provided"]


class StateStore(Protocol):
    """The one S3 call the route makes; boto3's S3 client satisfies it."""

    def get_object(self, *, Bucket: str, Key: str) -> Any: ...  # noqa: N803 - boto3's argument names


class _Unavailable(Exception):
    pass


class _Missing(Exception):
    pass


def _bounded[T](read: Callable[[], T]) -> T:
    expires = time.monotonic() + READ_DEADLINE_SECONDS

    def run() -> T:
        # A read queued behind stalled ones is dropped once its caller has stopped waiting.
        if time.monotonic() >= expires:
            raise _Unavailable("the read was dropped: its caller had stopped waiting")
        return read()

    answer = _READS.submit(run)
    try:
        return answer.result(timeout=READ_DEADLINE_SECONDS)
    except FutureTimeout as exc:
        answer.cancel()
        raise _Unavailable(f"the alert state was not read within {READ_DEADLINE_SECONDS:g} s") from exc


def _read(store: StateStore, base_uri: str) -> bytes:
    parts = urlsplit(base_uri)
    key = "/".join(part for part in (parts.path.strip("/"), *STATE_OBJECT) if part)
    try:
        body = store.get_object(Bucket=parts.netloc, Key=key)["Body"].read(MAX_STATE_BYTES + 1)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _MISSING:
            raise _Missing from exc
        log.warning("the alert state could not be read: %s", exc)
        raise _Unavailable("the alert state could not be read") from exc
    except BotoCoreError as exc:
        log.warning("the alert state could not be read: %s", exc)
        raise _Unavailable("the alert state could not be read") from exc
    if len(body) > MAX_STATE_BYTES:
        raise _Unavailable("the alert state is larger than any the evaluator writes")
    return bytes(body)


def _evaluated_at(body: bytes) -> datetime | None:
    """The evaluator's stamp, which sits beside the state model (AlertStore.write_state); None when absent or odd."""
    try:
        stamp = json.loads(body).get("evaluated_at")
        moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except (ValueError, AttributeError):
        return None
    if moment is None or moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def _latest_queued(state: AlertState) -> dict[str, OutboxEntry]:
    queued: dict[str, OutboxEntry] = {}
    for entry in sorted(state.outbox, key=lambda e: notification_order(e.notification.event)):
        queued[entry.notification.incident_id] = entry
    return queued


def _delivery(incident: Incident, queued: OutboxEntry | None) -> dict[str, Any]:
    settled = incident.delivery
    # An older notification still queued behind a newer one that already settled is not the latest: the settled is.
    if queued is not None and (
        settled is None or notification_order(queued.notification.event) >= notification_order(settled.event)
    ):
        return {
            "status": "pending",
            "event": queued.notification.event.value,
            "attempts": sum(queued.attempts.values()),
            "receivers": len(set(queued.attempts) | queued.delivered),
            "delivered": len(queued.delivered - queued.rejected),
        }
    if settled is None:
        return {"status": "unknown", "event": None, "attempts": None, "receivers": None, "delivered": None}
    return {
        "status": settled.outcome.value,
        "event": settled.event.value,
        "attempts": settled.attempts,
        "receivers": settled.receivers,
        "delivered": settled.delivered,
    }


def _incident(incident: Incident, queued: OutboxEntry | None) -> AlertIncident:
    # model_validate: the core enums' values are the model's literals, which pydantic checks and mypy cannot.
    return AlertIncident.model_validate(
        {
            "id": incident.id,
            "condition": incident.kind.value,
            "severity": incident.severity.value,
            "domain": incident.scope.domain,
            "product": incident.scope.product,
            "table": incident.scope.table,
            "component": incident.component,
            "opened_at": incident.opened_at,
            "last_seen_at": incident.last_seen_at,
            "summary": incident.reason,
            "run_ids": list(incident.run_ids),
            "delivery": _delivery(incident, queued),
        }
    )


def list_alert_incidents(request: Request, stage: str) -> StageAlertIncidents:
    """The stage's open incidents, each with its latest notification's delivery; never an empty list for none."""
    settings = request.app.state.settings
    store: StateStore | None = request.app.state.alert_state_store

    def answer(status: StateName, reason: str, **found: Any) -> StageAlertIncidents:
        state = AlertStateStatus(
            status=status,
            reason=reason,
            evaluated_at=found.get("evaluated_at"),
            stale=found.get("stale"),
        )
        return StageAlertIncidents(stage=stage, state=state, incidents=found.get("incidents"))

    if settings.alerts_state_base_uri is None or store is None:
        return answer("not_provided", "no alert evaluator, or no key to read its state, is configured")
    try:
        body = _bounded(lambda: _read(store, settings.alerts_state_base_uri))
    except _Missing:
        return answer("unavailable", "the alert evaluator has not written a state yet")
    except _Unavailable as exc:
        return answer("unavailable", str(exc))
    try:
        state = state_from_json(body.decode())
    except (AlertStateError, UnicodeDecodeError) as exc:
        log.warning("the alert state is unreadable: %s", exc)
        return answer("unavailable", "the alert state could not be parsed")
    evaluated_at = _evaluated_at(body)
    stale = evaluated_at is None or datetime.now(UTC) - evaluated_at > STALE_AFTER
    queued = _latest_queued(state)
    incidents = [_incident(i, queued.get(i.id)) for i in state.incidents if i.scope.stage == stage]
    if evaluated_at is None:
        reason = "the alert state does not say when it was evaluated"
    elif stale:
        reason = f"the alert evaluator has not run since {evaluated_at.isoformat()}"
    else:
        reason = "read from the alert evaluator's state"
    return answer("available", reason, evaluated_at=evaluated_at, stale=stale, incidents=incidents)
