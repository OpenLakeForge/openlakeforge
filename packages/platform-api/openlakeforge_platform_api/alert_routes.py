"""A stage's open alert incidents and their notification delivery (#1258), read from the evaluator's state.

The evaluator is the only writer of `alerts/` (ADR 0023, decision 3), and this route never writes: a state that does not
parse is answered `unavailable` and left for the evaluator, which keeps the corrupt copy, rather than through
`AlertStore.read_state`, which would write that copy itself and answer an empty state. An answer carries the fields of
the notification payload allowlist and delivery counts only: no receiver name, URL or header. Of the payload's links
it carries the runbook (#1344), built from this API's own `runbook_base_url` as the evaluator builds it from the same
URL, and not the run link, which names the stage's Dagster.

The one other read is the stage's Dagster job list, for each incident's domain and product (#1272): an incident is
scoped by job name, which the state keeps and its fingerprint includes, so the mapping is looked up when answering and
never written into the state.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Request
from openlakeforge_core.alerts import AlertState, Incident, OutboxEntry, runbook_link
from openlakeforge_core.alerts_state import AlertStateError, notification_order, state_from_json
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient

from openlakeforge_platform_api.alerting.store import STATE_OBJECT
from openlakeforge_platform_api.models import AlertIncident, AlertStateStatus, StageAlertIncidents
from openlakeforge_platform_api.pipeline_health import JobScope, job_scope

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
# The optional Dagster job read has its own workers: a stalled Dagster keeps a call running past the deadline (its
# client's own timeout is longer), and it must never hold the workers the alert state is read with.
_JOB_READS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alert-job-read")

StateName = Literal["available", "unavailable", "not_provided"]


class StateStore(Protocol):
    """The one S3 call the route makes; boto3's S3 client satisfies it."""

    def get_object(self, *, Bucket: str, Key: str) -> Any: ...  # noqa: N803 - boto3's argument names


class _Unavailable(Exception):
    pass


class _Missing(Exception):
    pass


def _bounded[T](read: Callable[[], T], expires: float, pool: ThreadPoolExecutor = _READS) -> T:
    """`read`'s answer before `expires`, a monotonic time shared by the whole request so that its reads together keep
    to READ_DEADLINE_SECONDS."""

    def run() -> T:
        # A read queued behind stalled ones is dropped once its caller has stopped waiting.
        if time.monotonic() >= expires:
            raise _Unavailable("the read was dropped: its caller had stopped waiting")
        return read()

    answer = pool.submit(run)
    try:
        return answer.result(timeout=max(expires - time.monotonic(), 0))
    except FutureTimeout as exc:
        answer.cancel()
        raise _Unavailable(f"the read did not finish within the request's {READ_DEADLINE_SECONDS:g} s") from exc


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


def _job_scopes(request: Request, stage: str, expires: float) -> dict[str, JobScope]:
    """Each listed job's domain and product; none when the stage has no Dagster or it cannot be read in time, and the
    incidents are then answered with a null domain and product rather than not at all."""
    url = request.app.state.settings.dagster_graphql_urls.get(stage)
    if not url:
        return {}
    try:
        jobs = _bounded(lambda: DagsterClient(url).list_jobs(), expires, _JOB_READS)
    except (_Unavailable, ServiceClientError) as exc:
        log.warning("the jobs of stage %s could not be read for incident scopes: %s", stage, exc)
        return {}
    return {job.name: job_scope(job.tags) for job in jobs}


def _incident(
    incident: Incident, queued: OutboxEntry | None, scopes: Mapping[str, JobScope], runbook_base_url: str | None
) -> AlertIncident:
    # The core scope's `product` is the job name (pipeline_health._evaluate).
    job = incident.scope.product
    scope = scopes.get(job, JobScope()) if job else JobScope()
    # model_validate: the core enums' values are the model's literals, which pydantic checks and mypy cannot.
    return AlertIncident.model_validate(
        {
            "id": incident.id,
            "condition": incident.kind.value,
            "severity": incident.severity.value,
            "job": job,
            "domain": scope.domain,
            "product": scope.product,
            "table": incident.scope.table,
            "component": incident.component,
            "opened_at": incident.opened_at,
            "last_seen_at": incident.last_seen_at,
            "summary": incident.reason,
            "run_ids": list(incident.run_ids),
            "delivery": _delivery(incident, queued),
            "runbook": runbook_link(runbook_base_url, incident.kind) if runbook_base_url else None,
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
    # One deadline for the request: the optional job lookup gets only what the state read left.
    expires = time.monotonic() + READ_DEADLINE_SECONDS
    try:
        body = _bounded(lambda: _read(store, settings.alerts_state_base_uri), expires)
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
    found = [i for i in state.incidents if i.scope.stage == stage]
    scopes = _job_scopes(request, stage, expires) if any(i.scope.product for i in found) else {}
    incidents = [_incident(i, queued.get(i.id), scopes, settings.runbook_base_url) for i in found]
    if evaluated_at is None:
        reason = "the alert state does not say when it was evaluated"
    elif stale:
        reason = f"the alert evaluator has not run since {evaluated_at.isoformat()}"
    else:
        reason = "read from the alert evaluator's state"
    return answer("available", reason, evaluated_at=evaluated_at, stale=stale, incidents=incidents)
