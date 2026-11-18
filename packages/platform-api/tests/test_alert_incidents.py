"""The alert incidents route (#1258) against an in-memory ops bucket holding states the evaluator's own code wrote."""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

import pytest
from _alerting_fakes import FakeBucket
from conftest import Mint
from fastapi.testclient import TestClient
from openlakeforge_core.alerts import (
    AlertEvent,
    AlertKind,
    AlertSeverity,
    AlertState,
    Incident,
    Notification,
    OutboxEntry,
)
from openlakeforge_core.alerts_state import expire_outbox, record_attempt, record_delivery
from openlakeforge_core.health import Scope
from test_reads import _as
from test_reads import _settings as _api_settings

from openlakeforge_platform_api import alert_routes
from openlakeforge_platform_api.alerting.store import AlertStore
from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.settings import Settings, SettingsError

BASE = "s3://ops/alerts/"
STATE_KEY = "alerts/state/v1/state.json"
INCIDENTS = "/api/v1/stages/dev/alerts/incidents"
RECEIVER_URL = "https://hooks.example.test/T000/secret-token"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _incident(
    number: int = 1,
    *,
    stage: str = "dev",
    kind: AlertKind = AlertKind.RUN_FAILED,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    component: str | None = None,
) -> Incident:
    opened = _now() - timedelta(hours=1)
    scope = (
        Scope(stage=stage)
        if kind in (AlertKind.SERVICE_DEGRADED, AlertKind.HEALTH_UNREADABLE)
        else Scope(stage=stage, domain="sales", product="orders", table="silver.orders")
    )
    return Incident(
        id=f"fp{number}-{int(opened.timestamp())}-1",
        fingerprint=f"fp{number}",
        kind=kind,
        severity=severity,
        notified_severity=severity,
        scope=scope,
        opened_at=opened,
        last_seen_at=_now(),
        reason="latest run failed",
        component=component,
        run_ids=("run-1", "run-2"),
    )


def _queued(incident: Incident, event: AlertEvent = AlertEvent.OPENED) -> OutboxEntry:
    notification = Notification(
        id=f"{incident.id}-{event.value}",
        event=event,
        incident_id=incident.id,
        severity=incident.severity,
        kind=incident.kind,
        scope=incident.scope,
        reason=incident.reason,
        event_at=_now(),
        opened_at=incident.opened_at,
        run_id=incident.run_ids[-1],
        links=(("run", "https://dagster-dev.example.test/runs/run-2"),),
    )
    return OutboxEntry(notification)


def _write(bucket: FakeBucket, state: AlertState, evaluated_at: datetime | None = None) -> None:
    AlertStore(bucket, BASE).write_state(state, evaluated_at=evaluated_at or _now())


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


def _app(settings: Settings, store: Any, **kwargs: Any) -> TestClient:
    configured = _api_settings(settings, alerts_state_base_uri=BASE)
    return TestClient(create_app(configured, alert_state_store=store, **kwargs))


@pytest.fixture
def api(settings: Settings, bucket: FakeBucket) -> TestClient:
    return _app(settings, bucket)


def _get(api: TestClient, mint: Mint, path: str = INCIDENTS) -> dict[str, Any]:
    response = api.get(path, headers=_as(mint, "data-engineer"))
    assert response.status_code == 200, response.text
    return response.json()


def _only(api: TestClient, mint: Mint) -> dict[str, Any]:
    body = _get(api, mint)
    assert body["state"]["status"] == "available", body
    (incident,) = body["incidents"]
    return incident


@pytest.mark.parametrize(
    ("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403), ("viewer", 403)]
)
def test_the_route_admits_data_engineer_and_above(api: TestClient, mint: Mint, role: str, status: int) -> None:
    assert api.get(INCIDENTS, headers=_as(mint, role)).status_code == status


def test_a_request_without_a_token_is_denied(api: TestClient) -> None:
    assert api.get(INCIDENTS).status_code == 401


def test_a_stage_that_is_not_enabled_is_refused(api: TestClient, mint: Mint) -> None:
    assert api.get(INCIDENTS.replace("/dev/", "/staging/"), headers=_as(mint, "platform-admin")).status_code == 404


def test_a_stage_the_role_is_not_granted_is_refused_as_the_pipeline_health_route_refuses_it(
    settings: Settings, bucket: FakeBucket, mint: Mint
) -> None:
    shipped = tomllib.loads(
        resources.files("openlakeforge_platform_api").joinpath("permissions.toml").read_text(encoding="utf-8")
    )
    for name in ("list_alert_incidents", "list_pipeline_health"):
        shipped["operations"][name]["role_stages"] = {"data-engineer": ["dev"]}
    api = _app(settings, bucket, permissions=PermissionMatrix.from_mapping(shipped, settings.precedence))
    _write(bucket, AlertState(incidents=(_incident(stage="prod"),)))

    incidents = api.get(INCIDENTS.replace("/dev/", "/prod/"), headers=_as(mint, "data-engineer"))
    pipelines = api.get("/api/v1/stages/prod/health/pipelines", headers=_as(mint, "data-engineer"))

    assert incidents.status_code == pipelines.status_code == 403
    assert incidents.json()["detail"] == pipelines.json()["detail"].replace(
        "list_pipeline_health", "list_alert_incidents"
    )
    assert "incidents" not in incidents.json()
    assert api.get(INCIDENTS, headers=_as(mint, "data-engineer")).status_code == 200


def test_a_firing_incident_with_a_queued_notification_is_pending(
    api: TestClient, mint: Mint, bucket: FakeBucket
) -> None:
    incident = _incident()
    state = AlertState(incidents=(incident,), outbox=(_queued(incident),))
    notification_id = state.outbox[0].notification.id
    for name in ("ops", "ops", "chat"):
        state = record_attempt(state, notification_id, name)
    state = record_delivery(state, notification_id, "chat", routed={"ops", "chat"})
    _write(bucket, state)

    found = _only(api, mint)

    assert found == {
        "id": incident.id,
        "condition": "run_failed",
        "severity": "critical",
        "domain": "sales",
        "product": "orders",
        "table": "silver.orders",
        "component": None,
        "opened_at": incident.opened_at.isoformat().replace("+00:00", "Z"),
        "last_seen_at": incident.last_seen_at.isoformat().replace("+00:00", "Z"),
        "summary": "latest run failed",
        "run_ids": ["run-1", "run-2"],
        "delivery": {"status": "pending", "event": "opened", "attempts": 3, "receivers": 2, "delivered": 1},
    }


def test_an_escalated_incident_reads_its_queued_escalation(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    incident = _incident(severity=AlertSeverity.WARNING)
    state = AlertState(incidents=(incident,), outbox=(_queued(incident),))
    state = record_delivery(
        record_attempt(state, state.outbox[0].notification.id, "ops"),
        state.outbox[0].notification.id,
        "ops",
        routed={"ops"},
    )
    escalated = replace(state.incidents[0], severity=AlertSeverity.CRITICAL, notified_severity=AlertSeverity.CRITICAL)
    _write(bucket, replace(state, incidents=(escalated,), outbox=(_queued(escalated, AlertEvent.ESCALATED),)))

    found = _only(api, mint)

    assert found["severity"] == "critical"
    assert found["delivery"] == {
        "status": "pending",
        "event": "escalated",
        "attempts": 0,
        "receivers": 0,
        "delivered": 0,
    }


def test_a_delivered_notification_reads_delivered(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    incident = _incident()
    state = AlertState(incidents=(incident,), outbox=(_queued(incident),))
    notification_id = state.outbox[0].notification.id
    state = record_delivery(record_attempt(state, notification_id, "ops"), notification_id, "ops", routed={"ops"})
    _write(bucket, state)

    assert _only(api, mint)["delivery"] == {
        "status": "delivered",
        "event": "opened",
        "attempts": 1,
        "receivers": 1,
        "delivered": 1,
    }


def test_an_expired_notification_reads_undeliverable_not_delivered(
    api: TestClient, mint: Mint, bucket: FakeBucket
) -> None:
    incident = _incident()
    state = AlertState(incidents=(incident,), outbox=(replace(_queued(incident), runs=12),))
    state = record_attempt(state, state.outbox[0].notification.id, "ops")
    state, expired = expire_outbox(state)
    assert expired and state.outbox == ()
    _write(bucket, state)

    assert _only(api, mint)["delivery"] == {
        "status": "undeliverable",
        "event": "opened",
        "attempts": 1,
        "receivers": 1,
        "delivered": 0,
    }


def test_a_notification_settled_before_outcomes_were_kept_is_unknown(
    api: TestClient, mint: Mint, bucket: FakeBucket
) -> None:
    _write(bucket, AlertState(incidents=(_incident(),)))
    document = json.loads(bucket.objects[STATE_KEY])
    del document["incidents"][0]["delivery"]
    bucket.objects[STATE_KEY] = json.dumps(document).encode()

    delivery = _only(api, mint)["delivery"]

    assert delivery == {"status": "unknown", "event": None, "attempts": None, "receivers": None, "delivered": None}


def test_only_the_stages_own_incidents_are_listed_platform_conditions_included(
    api: TestClient, mint: Mint, bucket: FakeBucket
) -> None:
    service = _incident(2, kind=AlertKind.SERVICE_DEGRADED, component="trino.workloads")
    _write(bucket, AlertState(incidents=(_incident(1, stage="prod"), service, _incident(3, stage="prod"))))

    found = _only(api, mint)

    assert (found["condition"], found["component"], found["domain"]) == ("service_degraded", "trino.workloads", None)


def test_no_incidents_is_an_empty_list_only_from_a_fresh_state(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    evaluated = _now() - timedelta(minutes=4)
    _write(bucket, AlertState(), evaluated)

    body = _get(api, mint)

    assert body["incidents"] == []
    assert body["state"] == {
        "status": "available",
        "reason": "read from the alert evaluator's state",
        "evaluated_at": evaluated.isoformat().replace("+00:00", "Z"),
        "stale": False,
    }


def test_a_state_the_evaluator_stopped_writing_is_stale(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    _write(bucket, AlertState(), _now() - alert_routes.STALE_AFTER - timedelta(minutes=1))

    state = _get(api, mint)["state"]

    assert (state["status"], state["stale"]) == ("available", True)
    assert "has not run since" in state["reason"]


def test_a_state_with_no_evaluation_time_is_stale(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    _write(bucket, AlertState())
    document = json.loads(bucket.objects[STATE_KEY])
    del document["evaluated_at"]
    bucket.objects[STATE_KEY] = json.dumps(document).encode()

    state = _get(api, mint)["state"]

    assert (state["evaluated_at"], state["stale"]) == (None, True)


def test_a_missing_state_is_unavailable_not_empty(api: TestClient, mint: Mint) -> None:
    body = _get(api, mint)

    assert body["incidents"] is None
    assert body["state"] == {
        "status": "unavailable",
        "reason": "the alert evaluator has not written a state yet",
        "evaluated_at": None,
        "stale": None,
    }


def test_a_corrupt_state_is_unavailable_and_the_api_writes_nothing(
    api: TestClient, mint: Mint, bucket: FakeBucket
) -> None:
    bucket.objects[STATE_KEY] = b'{"schema_version": 1, "incidents": "nope"}'
    before = dict(bucket.objects)

    body = _get(api, mint)

    assert (body["state"]["status"], body["incidents"]) == ("unavailable", None)
    assert bucket.objects == before


@pytest.mark.parametrize("stored", ["corrupt", "valid", "missing"])
def test_the_api_never_writes_to_the_store(settings: Settings, mint: Mint, bucket: FakeBucket, stored: str) -> None:
    # `AlertStore.read_state` writes a corrupt copy before answering an empty state; the route must take neither path.
    class ReadOnly(FakeBucket):
        def put_object(self, **kwargs: Any) -> None:
            raise AssertionError("the platform API wrote to the alert state")

    _write(bucket, AlertState(incidents=(_incident(),)))
    store = ReadOnly()
    if stored != "missing":
        store.objects[STATE_KEY] = b"not json" if stored == "corrupt" else bucket.objects[STATE_KEY]

    status = _get(_app(settings, store), mint)["state"]["status"]

    assert status == ("available" if stored == "valid" else "unavailable")


def test_an_unreachable_store_is_unavailable(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    _write(bucket, AlertState(incidents=(_incident(),)))
    bucket.down = True

    body = _get(api, mint)

    assert (body["state"]["status"], body["incidents"]) == ("unavailable", None)
    assert body["state"]["reason"] == "the alert state could not be read"


def test_a_read_past_the_deadline_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, mint: Mint
) -> None:
    class Hung(FakeBucket):
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            time.sleep(1)
            return super().get_object(Bucket=Bucket, Key=Key)

    monkeypatch.setattr(alert_routes, "READ_DEADLINE_SECONDS", 0.2)
    started = time.monotonic()

    body = _get(_app(settings, Hung()), mint)

    assert time.monotonic() - started < 1
    assert (body["state"]["status"], body["incidents"]) == ("unavailable", None)


@pytest.mark.parametrize("configured", ["no evaluator", "no key"])
def test_without_an_evaluator_or_a_key_alerts_are_not_provided(
    settings: Settings, mint: Mint, bucket: FakeBucket, configured: str
) -> None:
    _write(bucket, AlertState(incidents=(_incident(),)))
    if configured == "no evaluator":
        api = TestClient(create_app(_api_settings(settings), alert_state_store=bucket))
    else:
        api = TestClient(create_app(_api_settings(settings, alerts_state_base_uri=BASE)))

    body = _get(api, mint)

    assert (body["state"]["status"], body["incidents"]) == ("not_provided", None)


def test_the_answer_carries_no_receiver_url_header_or_name(api: TestClient, mint: Mint, bucket: FakeBucket) -> None:
    incident = _incident()
    state = AlertState(incidents=(incident,), outbox=(_queued(incident),))
    state = record_attempt(state, state.outbox[0].notification.id, "pager-team-secret")
    _write(bucket, state)
    # The evaluator never stores a URL or a header; a state that somehow did must still not leak one.
    document = json.loads(bucket.objects[STATE_KEY])
    document["receivers"] = [
        {"name": "pager-team-secret", "url": RECEIVER_URL, "headers": {"Authorization": "Bearer x"}}
    ]
    bucket.objects[STATE_KEY] = json.dumps(document).encode()

    text = api.get(INCIDENTS, headers=_as(mint, "platform-admin")).text

    for leaked in ("pager-team-secret", "hooks.example.test", "secret-token", "Authorization", "Bearer", "https://"):
        assert leaked not in text
    assert json.loads(text)["incidents"][0]["delivery"]["attempts"] == 1


@pytest.mark.parametrize("uri", ["s3://ops/alerts", "https://ops/alerts/", "s3:///alerts/"])
def test_the_state_base_must_be_an_s3_prefix(settings: Settings, uri: str) -> None:
    with pytest.raises(SettingsError, match="alerts_state_base_uri"):
        _api_settings(settings, alerts_state_base_uri=uri)
