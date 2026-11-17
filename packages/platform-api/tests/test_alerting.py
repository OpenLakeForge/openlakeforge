"""The alert evaluator against a fake Dagster, an in-memory ops bucket and a fake notifier."""

from __future__ import annotations

import json
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import certifi
import pytest
from _alerting_fakes import FakeBucket, publish, settings_mapping
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from openlakeforge_core.alerts_state import MAX_OUTBOX_RUNS
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterJob, DagsterRun, DagsterSchedule
from openlakeforge_services.notify import DeliveryResult

from openlakeforge_platform_api import quality_health
from openlakeforge_platform_api.alerting import __main__ as entrypoint
from openlakeforge_platform_api.alerting.run import run_once
from openlakeforge_platform_api.alerting.settings import EvaluatorSettings, ReceiverSetting
from openlakeforge_platform_api.alerting.store import AlertStore, StoreError
from openlakeforge_platform_api.settings import SettingsError

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
BASE = "s3://ops/alerts/"


class FakeDagster(DagsterClient):
    def __init__(self, runs: list[DagsterRun] | None = None) -> None:
        super().__init__("http://dagster.invalid/graphql")
        self.runs = runs or []
        self.unreadable = False
        # A Dagster that answers but has no code location loaded lists no job.
        self.unloaded = False

    def list_jobs(self) -> list[DagsterJob]:
        self._read()
        return [] if self.unloaded else [DagsterJob("loc", "repo", "orders_job", "")]

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


def test_the_stage_policy_is_read_but_does_not_change_what_alerts_yet() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    activation = publish(bucket, alerts="kind: not a policy\n")
    # The policy names no `run_failed` rule and cannot even be parsed, yet the failed run still notifies.
    summary = _evaluate(_settings(A), bucket, dagster, {"ops": notifier})

    assert summary.policy_unreadable == ("dev",)
    assert [payload["condition"] for _, payload in notifier.sent] == ["run_failed"]
    assert activation.activation_revision


def test_one_stage_with_unusable_objects_does_not_stop_the_others() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()
    dagster = {"dev": FakeDagster(), "prod": FakeDagster([_failed()])}
    bucket.objects["activations/dev/ACTIVE.json"] = b"\xff\xfe"

    summary = _evaluate(_settings(A, stages=("dev", "prod")), bucket, dagster, {"ops": notifier})

    assert summary.policy_unreadable == ("dev",)
    assert [
        payload["scope"]["stage"] if "scope" in payload else payload.get("stage") for _, payload in notifier.sent
    ] == ["prod"]


def test_a_clean_policy_read_is_not_reported_unreadable() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket)

    summary = _evaluate(_settings(A), bucket, dagster, {"ops": notifier})

    assert summary.policy_unreadable == ()
    assert len(notifier.sent) == 1


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


def _events(bucket: FakeBucket) -> list[dict[str, Any]]:
    return [r for r in bucket.under("alerts/history/v1/").values() if r["record"] == "event"]


def _state(bucket: FakeBucket) -> dict[str, Any]:
    return bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]


def test_a_quiet_evaluation_still_stamps_when_it_ran() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster()}
    _evaluate(_settings(A), bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    state = _state(bucket)
    assert state["evaluated_at"] == (NOW + timedelta(minutes=5)).isoformat()
    assert (state["incidents"], state["outbox"], notifier.sent) == ([], [], [])


def test_a_failed_state_write_leaves_no_phantom_transition_in_history() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    bucket.failing_writes = ("alerts/state/v1/state.json",)
    with pytest.raises(StoreError):
        _evaluate(_settings(A), bucket, dagster, {"ops": notifier})
    assert _events(bucket) == [] and notifier.sent == []

    bucket.failing_writes = ()
    _evaluate(_settings(A), bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    # Only the transition of the state that committed is recorded, under the id that state carries.
    (opened,) = _events(bucket)
    assert opened["incident_id"] == _state(bucket)["incidents"][0]["id"]
    assert len(notifier.sent) == 1


def test_a_failed_history_write_after_the_state_is_replayed_next_run_under_the_same_id() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    bucket.failing_writes = ("alerts/history/",)
    with pytest.raises(StoreError):
        _evaluate(_settings(A), bucket, dagster, {"ops": notifier})
    committed = _state(bucket)["incidents"][0]["id"]

    bucket.failing_writes = ()
    _evaluate(_settings(A), bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert [e["incident_id"] for e in _events(bucket)] == [committed]
    assert len(notifier.sent) == 1


def test_an_entry_already_held_by_every_receiver_still_routed_leaves_the_outbox() -> None:
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    steady, flaky = FakeNotifier(), FakeNotifier(up=False)
    both = _settings(A, ReceiverSetting("chat", "CHAT_URL"))
    _evaluate(both, bucket, dagster, {"ops": steady, "chat": flaky})
    assert len(_state(bucket)["outbox"]) == 1

    # "chat" is removed from the settings: "ops" already has it, so nothing is left to deliver.
    _evaluate(_settings(A), bucket, dagster, {"ops": steady}, NOW + timedelta(minutes=5))

    assert _state(bucket)["outbox"] == []
    assert len(steady.sent) == 1
    assert not [r for r in bucket.under("alerts/history/v1/").values() if r["record"] == "undeliverable"]


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


def test_an_unloaded_code_location_holds_open_incidents_instead_of_resolving_them() -> None:
    bucket, notifier, fake = FakeBucket(), FakeNotifier(), FakeDagster([_failed()])
    settings = _settings(A)
    _evaluate(settings, bucket, {"dev": fake}, {"ops": notifier})

    fake.unloaded = True
    _evaluate(settings, bucket, {"dev": fake}, {"ops": notifier}, NOW + timedelta(minutes=5))
    fake.unloaded = False
    _evaluate(settings, bucket, {"dev": fake}, {"ops": notifier}, NOW + timedelta(minutes=10))

    # One opening, no silent resolve while the location was unloaded, and no duplicate opening after it returned.
    assert [payload["event"] for _, payload in notifier.sent] == ["opened"]
    events = [r for r in bucket.under("alerts/history/v1/").values() if r["record"] != "delivery"]
    assert [r["event"] for r in events] == ["opened"]


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


class _Clock:
    """A monotonic clock that moves a fixed step each time it is read."""

    def __init__(self, step: float) -> None:
        self.step, self.now = step, 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _three_receivers() -> tuple[EvaluatorSettings, dict[str, FakeNotifier]]:
    names = ("a", "b", "c")
    settings = _settings(*(ReceiverSetting(n, f"{n.upper()}_URL") for n in names))
    return settings, {n: FakeNotifier() for n in names}


def _outbox(bucket: FakeBucket) -> list[dict[str, Any]]:
    return bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["outbox"]


def test_a_run_stops_starting_sends_at_the_budget_and_the_next_run_continues_without_resending() -> None:
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    settings, notifiers = _three_receivers()

    # Reads: start, then one per send. Step 40 against a 100 budget: two sends fit, the third does not start.
    first = run_once(
        settings=settings,
        store=AlertStore(bucket, BASE),
        dagster=dagster,
        notifiers=notifiers,
        now=NOW,
        delivery_budget=100,
        monotonic=_Clock(40),
    )

    assert first.delivered == 2
    assert [len(n.sent) for n in notifiers.values()] == [1, 1, 0]
    (entry,) = _outbox(bucket)
    assert sorted(entry["delivered"]) == ["a", "b"]
    assert entry["runs"] == 0

    second = _evaluate(settings, bucket, dagster, notifiers, NOW + timedelta(minutes=5))

    assert second.delivered == 1
    assert [len(n.sent) for n in notifiers.values()] == [1, 1, 1]
    assert _outbox(bucket) == []


def test_a_starved_entry_still_counts_its_runs_and_expires() -> None:
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    settings, notifiers = _three_receivers()

    for minutes in range(MAX_OUTBOX_RUNS + 1):
        summary = run_once(
            settings=settings,
            store=AlertStore(bucket, BASE),
            dagster=dagster,
            notifiers=notifiers,
            now=NOW + timedelta(minutes=5 * minutes),
            delivery_budget=0,
            monotonic=_Clock(1),
        )

    assert summary.expired == 1
    assert _outbox(bucket) == []
    assert all(n.sent == [] for n in notifiers.values())


def test_a_run_killed_mid_delivery_does_not_resend_what_it_delivered() -> None:
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    settings, notifiers = _three_receivers()
    notifiers["b"].crash = lambda: Killed("killed")

    with pytest.raises(Killed):
        _evaluate(settings, bucket, dagster, notifiers)
    notifiers["b"].crash = None
    _evaluate(settings, bucket, dagster, notifiers, NOW + timedelta(minutes=5))

    assert len(notifiers["a"].sent) == 1
    assert len(notifiers["b"].sent) == 2
    assert len(notifiers["c"].sent) == 1
    assert _outbox(bucket) == []


def _self_signed_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "olf-local-ca")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def test_the_combined_ca_bundle_holds_the_public_roots_and_the_local_ca(tmp_path: Any) -> None:
    local_pem = _self_signed_pem()
    local = tmp_path / "ca.crt"
    local.write_text(local_pem)
    public_pem = Path(certifi.where()).read_text(encoding="ascii")

    path = entrypoint.combined_ca_bundle(str(local), str(tmp_path))

    bundle = Path(path).read_text(encoding="ascii")
    assert public_pem.strip() in bundle
    assert local_pem.strip() in bundle
    assert Path(path).parent == tmp_path
    # Every certificate loads, so a missing newline between the two files would have glued two PEMs together.
    assert len(ssl.create_default_context(cafile=path).get_ca_certs()) == public_pem.count("BEGIN CERTIFICATE") + 1


def _with_quality(**manifests: str) -> dict[str, Any]:
    return {**settings_mapping(), "quality_manifests": manifests}


def test_quality_manifests_must_name_enabled_stages_and_s3_prefixes() -> None:
    parsed = EvaluatorSettings.from_mapping(_with_quality(dev="s3://ops/activations/dev/"))
    assert parsed.quality_manifests == {"dev": "s3://ops/activations/dev/"}
    assert EvaluatorSettings.from_mapping(settings_mapping()).quality_manifests == {}

    with pytest.raises(SettingsError, match="not enabled: prod"):
        EvaluatorSettings.from_mapping(_with_quality(prod="s3://ops/activations/prod/"))
    for bad in ("https://ops/activations/dev/", "s3://ops/activations/dev"):
        with pytest.raises(SettingsError, match="quality_manifests"):
            EvaluatorSettings.from_mapping(_with_quality(dev=bad))


def _run_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mapping: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_run_once(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(notifications=0, delivered=0, failed=0, expired=0, policy_unreadable=())

    monkeypatch.setattr(entrypoint, "_object_store", lambda settings: FakeBucket())
    monkeypatch.setattr(entrypoint, "run_once", fake_run_once)
    monkeypatch.setenv("OPS_URL", "https://hooks.example.test/secret-path")
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(mapping))
    return entrypoint.main(["--settings", str(path)]), captured


def test_the_entrypoint_reads_quality_with_its_own_key_for_each_stage_with_a_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "evaluator-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "evaluator-secret")
    clients: list[dict[str, Any]] = []

    def fake_client(service: str, **kwargs: Any) -> object:
        clients.append(kwargs)
        return object()

    monkeypatch.setattr(quality_health.boto3, "client", fake_client)
    mapping = _with_quality(dev="s3://ops/activations/dev/")
    mapping["stages"] = ["dev", "prod"]
    mapping["dagster_graphql_urls"]["prod"] = "http://dagster-prod/graphql"

    code, captured = _run_entrypoint(monkeypatch, tmp_path, mapping)

    assert code == 0
    assert set(captured["quality"]) == {"dev"}
    assert [(c["aws_access_key_id"], c["aws_secret_access_key"]) for c in clients] == [
        ("evaluator-key", "evaluator-secret")
    ]


def test_the_entrypoint_passes_no_readers_without_manifest_prefixes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    code, captured = _run_entrypoint(monkeypatch, tmp_path, settings_mapping())

    assert code == 0
    assert captured["quality"] == {}


def test_the_entrypoint_exits_two_when_quality_is_configured_but_the_key_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    code, _ = _run_entrypoint(monkeypatch, tmp_path, _with_quality(dev="s3://ops/activations/dev/"))

    assert code == 2
