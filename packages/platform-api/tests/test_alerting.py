"""The alert evaluator against a fake Dagster, an in-memory ops bucket and a fake notifier."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _alerting_fakes import FakeBucket, settings_mapping
from openlakeforge_core.alerts_state import MAX_OUTBOX_RUNS
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterJob, DagsterRun, DagsterSchedule
from openlakeforge_services.notify import DeliveryResult

from openlakeforge_platform_api.alerting import __main__ as entrypoint
from openlakeforge_platform_api.alerting.run import run_once
from openlakeforge_platform_api.alerting.settings import EvaluatorSettings, ReceiverSetting
from openlakeforge_platform_api.alerting.store import AlertStore, StoreError

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
BASE = "s3://ops/alerts/"


class FakeDagster(DagsterClient):
    def __init__(self, runs: list[DagsterRun] | None = None) -> None:
        super().__init__("http://dagster.invalid/graphql")
        self.runs = runs or []
        self.unreadable = False

    def list_jobs(self) -> list[DagsterJob]:
        self._read()
        return [DagsterJob("loc", "repo", "orders_job", "")]

    def list_schedules(self) -> list[DagsterSchedule]:
        return []

    def list_runs(self, *, job_name: str = "", limit: int = 20) -> list[DagsterRun]:
        return self.runs

    def _read(self) -> None:
        if self.unreadable:
            raise ServiceClientError("boom")

    def finish(self, run_id: str, status: str, at: datetime) -> None:
        stamp = at.timestamp()
        self.runs = [DagsterRun(run_id, "orders_job", status, stamp - 60, stamp - 59, stamp), *self.runs]


@dataclass
class FakeNotifier:
    up: bool = True
    rejecting: bool = False
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Raised by `send` after it is recorded, to stand for the process dying mid-delivery.
    crash: Callable[[], Exception] | None = None

    def send(self, payload: Mapping[str, Any], *, delivery_id: str) -> DeliveryResult:
        self.sent.append((delivery_id, dict(payload)))
        if self.crash:
            raise self.crash()
        if self.rejecting:
            return DeliveryResult("rejected", 404, 1)
        return DeliveryResult("delivered", 200, 1) if self.up else DeliveryResult("retryable", 503, 3)

    @property
    def keys(self) -> list[str]:
        return [key for key, _ in self.sent]


def _settings(*receivers: ReceiverSetting, stages: tuple[str, ...] = ("dev",), **links: Any) -> EvaluatorSettings:
    return EvaluatorSettings.from_mapping(
        {
            "stages": list(stages),
            "dagster_graphql_urls": {stage: f"http://dagster-{stage}/graphql" for stage in stages},
            "state_base_uri": BASE,
            "receivers": [{"name": r.name, "url_env": r.url_env, "stages": list(r.stages)} for r in receivers],
            "links": {"dagster_scheme": "http", "dagster_host_template": "dagster-{stage}.example.test", **links},
        }
    )


def _failed(run_id: str = "run-1") -> DagsterRun:
    stamp = (NOW - timedelta(minutes=30)).timestamp()
    return DagsterRun(run_id, "orders_job", "FAILURE", stamp - 60, stamp - 59, stamp)


def _evaluate(
    settings: EvaluatorSettings,
    bucket: FakeBucket,
    dagster: Mapping[str, FakeDagster],
    notifiers: Mapping[str, FakeNotifier],
    now: datetime = NOW,
) -> Any:
    return run_once(
        settings=settings,
        store=AlertStore(bucket, BASE),
        dagster=dagster,
        notifiers=notifiers,
        now=now,
    )


A = ReceiverSetting("ops", "OPS_URL")


class Killed(Exception):
    """Stands for the process dying."""


def test_two_evaluations_of_the_same_dagster_state_send_one_notification() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    settings = _settings(A)

    _evaluate(settings, bucket, dagster, {"ops": notifier})
    _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert len(notifier.sent) == 1
    payload = notifier.sent[0][1]
    assert payload["event"] == "opened"
    assert payload["condition"] == "run_failed"
    assert payload["links"]["run"] == "http://dagster-dev.example.test/runs/run-1"
    assert bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["outbox"] == []


def test_a_crash_between_the_state_write_and_delivery_redelivers_once_with_the_same_key() -> None:
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    crashing = FakeNotifier(crash=lambda: Killed("killed"))
    settings = _settings(A)

    with pytest.raises(Killed):
        _evaluate(settings, bucket, dagster, {"ops": crashing})
    survivor = FakeNotifier()
    _evaluate(settings, bucket, dagster, {"ops": survivor}, NOW + timedelta(minutes=5))
    _evaluate(settings, bucket, dagster, {"ops": survivor}, NOW + timedelta(minutes=10))

    assert survivor.keys == crashing.keys
    assert len(survivor.sent) == 1


def test_recovery_notifies_once_and_clears_the_incident() -> None:
    bucket, notifier, fake = FakeBucket(), FakeNotifier(), FakeDagster([_failed()])
    settings = _settings(A)
    _evaluate(settings, bucket, {"dev": fake}, {"ops": notifier})

    fake.finish("run-2", "SUCCESS", NOW)
    later = NOW + timedelta(minutes=5)
    _evaluate(settings, bucket, {"dev": fake}, {"ops": notifier}, later)
    _evaluate(settings, bucket, {"dev": fake}, {"ops": notifier}, later + timedelta(minutes=5))

    assert [payload["event"] for _, payload in notifier.sent] == ["opened", "resolved"]
    assert bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["incidents"] == []


def test_a_restart_reloads_the_state_and_sends_no_duplicate() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()
    settings = _settings(A)
    _evaluate(settings, bucket, {"dev": FakeDagster([_failed()])}, {"ops": notifier})

    # A new process: nothing carried over but the bucket.
    _evaluate(settings, bucket, {"dev": FakeDagster([_failed()])}, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert len(notifier.sent) == 1


def test_a_store_outage_delivers_nothing() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()
    bucket.down = True

    with pytest.raises(StoreError):
        _evaluate(_settings(A), bucket, {"dev": FakeDagster([_failed()])}, {"ops": notifier})

    assert notifier.sent == []


def test_a_store_outage_after_the_read_still_delivers_nothing() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()
    original = bucket.get_object

    def read_then_fail(*, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        try:
            return original(Bucket=Bucket, Key=Key)
        finally:
            bucket.down = True

    bucket.get_object = read_then_fail  # type: ignore[method-assign]

    with pytest.raises(StoreError):
        _evaluate(_settings(A), bucket, {"dev": FakeDagster([_failed()])}, {"ops": notifier})

    assert notifier.sent == []


def test_a_receiver_that_is_down_and_then_up_is_sent_to_once_and_the_other_not_again() -> None:
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    steady, flaky = FakeNotifier(), FakeNotifier(up=False)
    settings = _settings(A, ReceiverSetting("chat", "CHAT_URL"))
    notifiers = {"ops": steady, "chat": flaky}

    _evaluate(settings, bucket, dagster, notifiers)
    flaky.up = True
    _evaluate(settings, bucket, dagster, notifiers, NOW + timedelta(minutes=5))
    _evaluate(settings, bucket, dagster, notifiers, NOW + timedelta(minutes=10))

    assert len(steady.sent) == 1
    assert len(flaky.sent) == 2
    assert steady.keys == flaky.keys[:1] == flaky.keys[1:]
    records = bucket.under("alerts/history/v1/")
    assert sorted(r["outcome"] for r in records.values() if r["record"] == "delivery" and r["receiver"] == "chat") == [
        "delivered",
        "retryable",
    ]


def test_a_rejected_delivery_is_not_resent_and_is_recorded_as_rejected() -> None:
    # A 4xx other than 408/429 will not change on retry (ADR 0023, decision 4).
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    rejecting = FakeNotifier(rejecting=True)
    settings = _settings(A)

    for minutes in (0, 5, 10):
        _evaluate(settings, bucket, dagster, {"ops": rejecting}, NOW + timedelta(minutes=minutes))

    assert len(rejecting.sent) == 1
    records = bucket.under("alerts/history/v1/")
    assert [r["outcome"] for r in records.values() if r["record"] == "delivery"] == ["rejected"]
    assert bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["outbox"] == []


def test_a_receiver_only_gets_the_stages_it_names() -> None:
    bucket = FakeBucket()
    settings = _settings(
        ReceiverSetting("prod-pager", "PAGER_URL", ("prod",)),
        ReceiverSetting("everything", "ALL_URL"),
        stages=("dev", "prod"),
    )
    pager, everything = FakeNotifier(), FakeNotifier()
    dagster = {"dev": FakeDagster([_failed("dev-run")]), "prod": FakeDagster()}

    _evaluate(settings, bucket, dagster, {"prod-pager": pager, "everything": everything})
    dagster["prod"].runs = [_failed("prod-run")]
    _evaluate(settings, bucket, dagster, {"prod-pager": pager, "everything": everything}, NOW + timedelta(minutes=5))

    assert [payload["stage"] for _, payload in pager.sent] == ["prod"]
    assert sorted(payload["stage"] for _, payload in everything.sent) == ["dev", "prod"]


def test_a_notification_nobody_is_routed_to_leaves_the_outbox() -> None:
    bucket = FakeBucket()
    settings = _settings(ReceiverSetting("prod-pager", "PAGER_URL", ("prod",)), stages=("dev", "prod"))

    _evaluate(
        settings, bucket, {"dev": FakeDagster([_failed()]), "prod": FakeDagster()}, {"prod-pager": FakeNotifier()}
    )

    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert state["outbox"] == []
    assert len(state["incidents"]) == 1


def test_a_notification_that_cannot_be_delivered_expires_into_an_undeliverable_record() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(up=False), {"dev": FakeDagster([_failed()])}
    settings = _settings(A)

    for run in range(MAX_OUTBOX_RUNS):
        _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5 * run))
    assert not [r for r in bucket.under("alerts/history/v1/").values() if r["record"] == "undeliverable"]
    _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5 * MAX_OUTBOX_RUNS))

    (record,) = (r for r in bucket.under("alerts/history/v1/").values() if r["record"] == "undeliverable")
    assert record["receivers"] == ["ops"]
    assert record["attempts"] == {"ops": MAX_OUTBOX_RUNS + 1}
    assert bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["outbox"] == []
    sends = len(notifier.sent)
    _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(hours=2))
    assert len(notifier.sent) == sends


def test_a_corrupt_state_is_kept_and_what_is_firing_is_notified_again() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()
    bucket.objects["alerts/state/v1/state.json"] = b"{not json"

    _evaluate(_settings(A), bucket, {"dev": FakeDagster([_failed()])}, {"ops": notifier})

    assert len(notifier.sent) == 1
    (kept,) = [key for key in bucket.objects if "/corrupt/" in key]
    assert bucket.objects[kept] == b"{not json"
    assert bucket.objects["alerts/state/v1/state.json"] != b"{not json"


def test_history_names_the_receiver_and_status_but_never_a_url() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()

    _evaluate(_settings(A), bucket, {"dev": FakeDagster([_failed()])}, {"ops": notifier})

    (delivery,) = (r for r in bucket.under("alerts/history/v1/").values() if r["record"] == "delivery")
    assert delivery["receiver"] == "ops"
    assert delivery["http_status"] == 200
    assert delivery["attempts"] == 1
    assert "://" not in json.dumps(bucket.under("alerts/history/v1/"))


def test_an_unreadable_stage_holds_its_open_incidents_until_it_is_the_stage_that_is_reported() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), FakeDagster([_failed()])
    settings = _settings(A)
    _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier})

    dagster.unreadable = True
    for run in range(1, 4):
        _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier}, NOW + timedelta(minutes=5 * run))

    assert [payload["condition"] for _, payload in notifier.sent] == ["run_failed", "stage_unreadable"]
    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert sorted(i["kind"] for i in state["incidents"]) == ["run_failed", "stage_unreadable"]


def test_a_stage_that_reads_again_resolves_its_unreadable_incident_once() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), FakeDagster()
    settings = _settings(A)
    dagster.unreadable = True
    for run in range(3):
        _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier}, NOW + timedelta(minutes=5 * run))
    dagster.unreadable = False

    _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier}, NOW + timedelta(minutes=20))

    assert [payload["event"] for _, payload in notifier.sent] == ["opened", "resolved"]


def test_one_or_two_unreadable_evaluations_do_not_alert() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), FakeDagster()
    settings = _settings(A)
    dagster.unreadable = True
    _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier})
    dagster.unreadable = False
    _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier}, NOW + timedelta(minutes=5))
    dagster.unreadable = True
    _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier}, NOW + timedelta(minutes=10))
    _evaluate(settings, bucket, {"dev": dagster}, {"ops": notifier}, NOW + timedelta(minutes=15))

    assert notifier.sent == []


def test_a_fixed_clock_names_the_incident_and_the_history_day() -> None:
    bucket = FakeBucket()
    store = AlertStore(bucket, BASE, prefix="e2e/alerts")

    run_once(
        settings=_settings(A),
        store=store,
        dagster={"dev": FakeDagster([_failed()])},
        notifiers={"ops": FakeNotifier()},
        now=NOW,
    )

    assert not [key for key in bucket.objects if key.startswith("alerts/")]
    assert any(key.startswith("e2e/alerts/history/v1/date=2026-11-16/") for key in bucket.objects)


def test_the_entrypoint_exits_one_when_the_store_is_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    bucket = FakeBucket()
    bucket.down = True
    monkeypatch.setattr(entrypoint, "_object_store", lambda settings: bucket)
    monkeypatch.setenv("OPS_URL", "https://hooks.example.test/secret-path")
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(settings_mapping()))

    assert entrypoint.main(["--settings", str(path)]) == 1


def test_the_entrypoint_exits_two_without_a_receiver_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("OPS_URL", raising=False)
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(settings_mapping()))

    assert entrypoint.main(["--settings", str(path)]) == 2
