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
from _alerting_fakes import POLICY, FakeBucket, declare_everything, publish, settings_mapping
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
            "receivers": [
                {"name": r.name, "url_env": r.url_env, "stages": list(r.stages), "platform": r.platform}
                for r in receivers
            ],
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
    declare_everything(bucket, settings.stages, [r.name for r in settings.receivers])
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
    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert state["outbox"] == []
    assert state["incidents"][0]["delivery"]["outcome"] == "delivered"


RUNBOOK = "https://github.com/OpenLakeForge/openlakeforge/blob/v1.2.3/docs/guides/operations/alerts.md"


def test_a_notification_links_the_runbook_section_of_its_condition() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}

    _evaluate(_settings(A, runbook_base_url=RUNBOOK), bucket, dagster, {"ops": notifier})

    (payload,) = [p for _, p in notifier.sent]
    assert payload["links"]["runbook"] == f"{RUNBOOK}#run_failed"


def test_without_a_runbook_base_a_notification_carries_no_runbook_link() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}

    _evaluate(_settings(A), bucket, dagster, {"ops": notifier})

    assert "runbook" not in notifier.sent[0][1]["links"]


def test_a_notification_links_the_incidents_row_in_the_portal() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}

    _evaluate(_settings(A, portal_url="https://lake.example.test/"), bucket, dagster, {"ops": notifier})

    (payload,) = [p for _, p in notifier.sent]
    assert payload["links"]["portal"] == (
        f"https://lake.example.test/monitoring?stage=dev&incident={payload['incident_id']}"
    )
    # Without the Portal's origin there is no link.
    other, bucket = FakeNotifier(), FakeBucket()
    _evaluate(_settings(A), bucket, dagster, {"ops": other})
    assert "portal" not in other.sent[0][1]["links"]


@pytest.mark.parametrize(
    "portal_url",
    [
        "lake.example.test",
        "ftp://lake.example.test",
        "https://",
        "https://lake.example.test/portal",
        "https://lake.example.test?view=alerts",
        "https://lake.example.test#top",
        "https://lake.example.test?",
        "https://lake.example.test#",
    ],
)
def test_a_portal_url_that_is_not_an_http_origin_is_refused(portal_url: str) -> None:
    with pytest.raises(SettingsError, match="links.portal_url"):
        _settings(A, portal_url=portal_url)


@dataclass
class LandingNotifier(FakeNotifier):
    url: str = "https://github.com/acme/alerts/discussions/9"

    def send(self, payload: Mapping[str, Any], *, delivery_id: str) -> DeliveryResult:
        self.sent.append((delivery_id, dict(payload)))
        return DeliveryResult("delivered", 200, 1, self.url)


def test_where_a_receiver_says_a_notification_landed_is_kept_on_the_incident() -> None:
    bucket, dagster = FakeBucket(), {"dev": FakeDagster([_failed()])}
    landing, failing = LandingNotifier(), FakeNotifier(up=False)
    settings = _settings(A, ReceiverSetting("chat", "CHAT_URL"))

    _evaluate(settings, bucket, dagster, {"ops": landing, "chat": failing})

    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    (entry,) = state["outbox"]
    assert entry["urls"] == {"ops": landing.url}
    assert entry["delivered"] == ["ops"]


def _told(notifier: FakeNotifier) -> list[tuple[str, str, str]]:
    """(stage, condition, event) of everything sent, in order."""
    return [(p["stage"], p["condition"], p["event"]) for _, p in notifier.sent]


def _policy(when: str) -> str:
    """The sample policy with its one global rule's conditions replaced; each line of `when` is a `when` entry."""
    return POLICY.replace("      run_failed: {}\n", "".join(f"      {line}\n" for line in when.splitlines()))


def _succeeded(run_id: str, hours_ago: float) -> DagsterRun:
    stamp = (NOW - timedelta(hours=hours_ago)).timestamp()
    return DagsterRun(run_id, "orders_job", "SUCCESS", stamp - 60, stamp - 59, stamp)


def test_a_condition_no_rule_declares_does_not_alert() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket, alerts=_policy("quality: {}"))

    summary = _evaluate(_settings(A), bucket, dagster, {"ops": notifier})

    assert (summary.notifications, notifier.sent) == (0, [])


def test_a_project_without_a_policy_gets_no_product_alerts() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket, alerts=None)

    assert _evaluate(_settings(A), bucket, dagster, {"ops": notifier}).notifications == 0
    assert notifier.sent == []


def test_a_policy_that_never_read_cleanly_alerts_nothing_and_is_reported() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket, alerts="kind: not a policy\n")

    summary = _evaluate(_settings(A), bucket, dagster, {"ops": notifier})

    assert summary.policy_unreadable == ("dev",)
    # No run_failed alert, only the platform's own warning.
    assert _told(notifier) == [("dev", "policy_unreadable", "opened")]


def test_a_stage_keeps_its_last_good_policy_when_a_newer_revision_is_unusable() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket)
    settings = _settings(A)
    _evaluate(settings, bucket, dagster, {"ops": notifier})
    dagster["dev"].finish("run-2", "SUCCESS", NOW + timedelta(minutes=2))
    publish(bucket, alerts="kind: not a policy\n", seed="b")

    summary = _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert summary.policy_unreadable == ("dev",)
    # Under an empty policy the incident would have closed silently; the old rules still tell the receiver.
    assert _told(notifier) == [
        ("dev", "run_failed", "opened"),
        ("dev", "run_failed", "resolved"),
        ("dev", "policy_unreadable", "opened"),
    ]


def test_a_rule_that_disappears_resolves_its_incident_without_a_notification() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket)
    settings = _settings(A)
    _evaluate(settings, bucket, dagster, {"ops": notifier})
    publish(bucket, alerts=_policy("quality: {}"), seed="b")

    _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert [p["event"] for _, p in notifier.sent] == ["opened"]
    assert bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["incidents"] == []
    events = [r["event"] for r in bucket.under("alerts/history/").values() if r["record"] == "event"]
    assert sorted(events) == ["opened", "resolved_silently"]


def test_a_severity_the_rule_lowers_is_recorded_in_history_and_sends_nothing() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket)
    settings = _settings(A)
    _evaluate(settings, bucket, dagster, {"ops": notifier})
    publish(bucket, alerts=_policy("run_failed: {severity: warning}"), seed="b")

    _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert [p["event"] for _, p in notifier.sent] == ["opened"]
    changed = [r for r in bucket.under("alerts/history/").values() if r.get("event") == "severity_changed"]
    assert [(r["condition"], r["severity"]) for r in changed] == [("run_failed", "warning")]


def test_a_policy_that_cannot_be_read_and_was_never_cached_holds_the_open_incident() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket)
    settings = _settings(A)
    _evaluate(settings, bucket, dagster, {"ops": notifier})
    del bucket.objects["alerts/state/v1/policy/dev.json"]
    bucket.objects["activations/dev/ACTIVE.json"] = b"\xff\xfe"

    summary = _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert summary.policy_unreadable == ("dev",)
    incidents = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]["incidents"]
    assert sorted(i["kind"] for i in incidents) == ["policy_unreadable", "run_failed"]
    assert _told(notifier) == [("dev", "run_failed", "opened"), ("dev", "policy_unreadable", "opened")]


def test_a_rule_sets_the_severity_of_the_notification() -> None:
    bucket, notifier, dagster = FakeBucket(), FakeNotifier(), {"dev": FakeDagster([_failed()])}
    publish(bucket, alerts=_policy("run_failed: {severity: warning}"))

    _evaluate(_settings(A), bucket, dagster, {"ops": notifier})

    assert [p["severity"] for _, p in notifier.sent] == ["warning"]


def test_freshness_bounds_alert_on_the_age_of_the_last_success_with_no_schedule() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()
    dagster = {"dev": FakeDagster([_succeeded("run-1", 27)])}
    publish(bucket, alerts=_policy("freshness: {warn_after: 26h, critical_after: 30h}"))
    settings = _settings(A)

    _evaluate(settings, bucket, dagster, {"ops": notifier})
    dagster["dev"].runs = [_succeeded("run-1", 31)]
    _evaluate(settings, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert [(p["event"], p["condition"], p["severity"]) for _, p in notifier.sent] == [
        ("opened", "stale_output", "warning"),
        ("escalated", "stale_output", "critical"),
    ]


def test_one_stage_with_unusable_objects_does_not_stop_the_others() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier()
    dagster = {"dev": FakeDagster(), "prod": FakeDagster([_failed()])}
    bucket.objects["activations/dev/ACTIVE.json"] = b"\xff\xfe"

    summary = _evaluate(_settings(A, stages=("dev", "prod")), bucket, dagster, {"ops": notifier})

    assert summary.policy_unreadable == ("dev",)
    assert sorted(_told(notifier)) == [("dev", "policy_unreadable", "opened"), ("prod", "run_failed", "opened")]


def test_an_unexpected_error_reading_one_stage_does_not_stop_the_others() -> None:
    import json as _json

    bucket, notifier = FakeBucket(), FakeNotifier()
    dagster = {"dev": FakeDagster(), "prod": FakeDagster([_failed()])}
    activation = publish(bucket, stage="dev")
    key = f"activations/dev/revisions/sha256/{activation.activation_revision.removeprefix('sha256:')}/ACTIVATION.json"
    record = _json.loads(bucket.objects[key])
    record["capabilities"] = None
    bucket.objects[key] = _json.dumps(record).encode()

    summary = _evaluate(_settings(A, stages=("dev", "prod")), bucket, dagster, {"ops": notifier})

    assert summary.policy_unreadable == ("dev",)
    assert sorted(_told(notifier)) == [("dev", "policy_unreadable", "opened"), ("prod", "run_failed", "opened")]


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
    assert [c for _, c, _ in _told(steady)] == ["run_failed", "policy_unreadable"]
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
    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert state["outbox"] == []
    # Settled, but nobody was told: the open incident must not read as delivered.
    assert state["incidents"][0]["delivery"] == {
        "event": "opened",
        "outcome": "undeliverable",
        "receivers": 1,
        "delivered": 0,
        "attempts": 1,
        "targets": [{"receiver": "ops", "status": "rejected", "attempts": 1, "url": None}],
    }


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
    assert [incident["delivery"]["outcome"] for incident in state["incidents"]] == ["unrouted"]


def test_a_notification_whose_failing_receiver_is_removed_from_the_routes_is_unrouted_not_delivered() -> None:
    bucket, notifier = FakeBucket(), FakeNotifier(up=False)
    dagster = {"dev": FakeDagster([_failed()]), "prod": FakeDagster()}

    _evaluate(_settings(A, stages=("dev", "prod")), bucket, dagster, {"ops": notifier})
    rerouted = _settings(ReceiverSetting("ops", "OPS_URL", ("prod",)), stages=("dev", "prod"))
    _evaluate(rerouted, bucket, dagster, {"ops": notifier}, NOW + timedelta(minutes=5))

    assert len(notifier.sent) == 1
    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert state["outbox"] == []
    # The receiver taken out of the routes is no longer one of its receivers, so none is listed.
    assert state["incidents"][0]["delivery"] == {
        "event": "opened",
        "outcome": "unrouted",
        "receivers": 1,
        "delivered": 0,
        "attempts": 1,
    }


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
    state = bucket.under("alerts/state/v1/state.json")["alerts/state/v1/state.json"]
    assert state["outbox"] == []
    assert state["incidents"][0]["delivery"]["outcome"] == "undeliverable"
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
    declare_everything(bucket, ("dev",))

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

    declare_everything(bucket, settings.stages, [r.name for r in settings.receivers])
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

    declare_everything(bucket, settings.stages, [r.name for r in settings.receivers])
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


def test_the_platform_api_url_and_its_token_file_come_together() -> None:
    both = {"platform_api_url": "http://platform-api.olf-system:8080", "platform_api_token_file": "/t/token"}

    parsed = EvaluatorSettings.from_mapping({**settings_mapping(), **both})

    assert (parsed.platform_api_url, parsed.platform_api_token_file) == tuple(both.values())
    assert EvaluatorSettings.from_mapping(settings_mapping()).platform_api_url is None
    for broken in (
        {"platform_api_url": both["platform_api_url"]},
        {"platform_api_token_file": both["platform_api_token_file"]},
        {**both, "platform_api_url": "platform-api:8080"},
    ):
        with pytest.raises(SettingsError):
            EvaluatorSettings.from_mapping({**settings_mapping(), **broken})


_GITHUB_RECEIVER = {
    "name": "team",
    "type": "github-discussions",
    "repository": "my-org/olf-alerts",
    "category": "Alerts",
    "api_url": "https://api.github.com",
    "app_id_env": "TEAM_APP_ID",
    "private_key_env": "TEAM_PRIVATE_KEY",
}


def test_a_receiver_without_a_type_is_a_webhook() -> None:
    parsed = EvaluatorSettings.from_mapping(settings_mapping())

    assert [(r.name, r.type, r.url_env) for r in parsed.receivers] == [("ops", "webhook", "OPS_URL")]


def test_a_github_discussions_receiver_is_parsed_with_its_destination_and_variables() -> None:
    parsed = EvaluatorSettings.from_mapping({**settings_mapping(), "receivers": [_GITHUB_RECEIVER]})

    receiver = parsed.receivers[0]
    assert (receiver.type, receiver.repository, receiver.category, receiver.api_url) == (
        "github-discussions",
        "my-org/olf-alerts",
        "Alerts",
        "https://api.github.com",
    )
    assert (receiver.app_id_env, receiver.private_key_env) == ("TEAM_APP_ID", "TEAM_PRIVATE_KEY")


@pytest.mark.parametrize(
    "receiver",
    [
        {"name": "ops", "type": "email", "url_env": "X"},
        {"name": "ops", "type": "webhook"},
        {**_GITHUB_RECEIVER, "repository": ""},
        {**_GITHUB_RECEIVER, "private_key_env": ""},
    ],
)
def test_settings_refuse_a_receiver_that_lacks_what_its_type_needs(receiver: dict[str, Any]) -> None:
    with pytest.raises(SettingsError, match="receiver 'ops'|receiver 'team'"):
        EvaluatorSettings.from_mapping({**settings_mapping(), "receivers": [receiver]})


def _notifiers_for(*receivers: dict[str, Any]) -> dict[str, Any]:
    return entrypoint._notifiers(EvaluatorSettings.from_mapping({**settings_mapping(), "receivers": list(receivers)}))


def test_each_receiver_type_gets_its_own_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_URL", "https://hooks.example.test/ops")
    monkeypatch.setenv("TEAM_APP_ID", "12345")
    monkeypatch.setenv("TEAM_PRIVATE_KEY", "not-a-real-key")

    notifiers = _notifiers_for({"name": "ops", "url_env": "OPS_URL"}, _GITHUB_RECEIVER)

    assert {name: type(notifier).__name__ for name, notifier in notifiers.items()} == {
        "ops": "WebhookNotifier",
        "team": "GitHubDiscussionsNotifier",
    }


@pytest.mark.parametrize("missing", ["TEAM_APP_ID", "TEAM_PRIVATE_KEY"])
def test_a_missing_github_variable_names_the_receiver_and_variable_not_a_value(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("TEAM_APP_ID", "12345")
    monkeypatch.setenv("TEAM_PRIVATE_KEY", "secret-key-material")
    monkeypatch.delenv(missing)

    with pytest.raises(SettingsError) as error:
        _notifiers_for(_GITHUB_RECEIVER)

    assert f"receiver 'team': {missing} is not set" in str(error.value)
    assert "secret-key-material" not in str(error.value)


def test_a_missing_webhook_variable_still_names_the_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPS_URL", raising=False)

    with pytest.raises(SettingsError, match="receiver 'ops': OPS_URL is not set"):
        _notifiers_for({"name": "ops", "url_env": "OPS_URL"})


def test_a_github_receiver_the_notifier_refuses_is_a_settings_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_APP_ID", "12345")
    monkeypatch.setenv("TEAM_PRIVATE_KEY", "k")

    with pytest.raises(SettingsError, match="repository must be owner/name"):
        _notifiers_for({**_GITHUB_RECEIVER, "repository": "no-slash"})


class _RecordingNotifier:
    def __init__(self, result: DeliveryResult) -> None:
        self.result = result
        self.sent: list[tuple[dict[str, Any], str]] = []

    def send(self, payload: Mapping[str, Any], *, delivery_id: str) -> DeliveryResult:
        self.sent.append((dict(payload), delivery_id))
        return self.result


def _test_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    result: DeliveryResult,
    *flags: str,
    mapping: dict[str, Any] | None = None,
) -> tuple[int, _RecordingNotifier, str]:
    notifier = _RecordingNotifier(result)

    def untouched(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test send must not read or write state, history or Dagster")

    for name in ("_object_store", "_quality_readers", "_health_reader", "run_once", "AlertStore", "DagsterClient"):
        monkeypatch.setattr(entrypoint, name, untouched)
    monkeypatch.setattr(entrypoint, "_notifier", lambda receiver, ca_file: notifier)
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(mapping or settings_mapping()))
    code = entrypoint.main(["--settings", str(path), *flags])
    return code, notifier, capsys.readouterr().out


def test_a_test_send_posts_one_synthetic_payload_and_touches_no_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, notifier, out = _test_send(
        monkeypatch, tmp_path, capsys, DeliveryResult("delivered", 201, 1), "--test-receiver", "ops"
    )

    assert code == 0
    assert json.loads(out) == {"receiver": "ops", "status": "delivered", "http_status": 201, "attempts": 1}
    (payload, delivery_id), = notifier.sent
    assert payload["schema"] == "openlakeforge.alert.v1"
    assert (payload["event"], payload["stage"], payload["summary"]) == ("test", "dev", "OpenLakeForge alert test")
    assert payload["incident_id"].startswith("test-")
    assert delivery_id == f"{payload['incident_id']}:test"


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (DeliveryResult("rejected", 403, 1), 3),
        (DeliveryResult("retryable", 503, 3), 3),
        (DeliveryResult("delivered", 200, 1), 0),
    ],
)
def test_a_test_send_exits_zero_only_when_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], result: DeliveryResult, code: int
) -> None:
    exit_code, _, out = _test_send(monkeypatch, tmp_path, capsys, result, "--test-receiver", "ops")

    assert exit_code == code
    assert json.loads(out)["status"] == result.status


def test_a_test_send_defaults_to_the_first_stage_the_receiver_hears_and_takes_an_explicit_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mapping = {
        **settings_mapping(),
        "stages": ["dev", "prod"],
        "dagster_graphql_urls": {"dev": "http://d/graphql", "prod": "http://p/graphql"},
        "receivers": [{"name": "ops", "url_env": "OPS_URL", "stages": ["prod"]}],
    }
    ok = DeliveryResult("delivered", 200, 1)

    _, notifier, _ = _test_send(monkeypatch, tmp_path, capsys, ok, "--test-receiver", "ops", mapping=mapping)
    assert notifier.sent[0][0]["stage"] == "prod"

    code, notifier, _ = _test_send(
        monkeypatch, tmp_path, capsys, ok, "--test-receiver", "ops", "--test-stage", "dev", mapping=mapping
    )
    assert code == 2 and notifier.sent == []


def test_a_test_send_to_an_unknown_receiver_exits_two_naming_the_declared_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    code, notifier, out = _test_send(
        monkeypatch, tmp_path, capsys, DeliveryResult("delivered", 200, 1), "--test-receiver", "nope"
    )

    assert code == 2 and notifier.sent == [] and out == ""
    assert "unknown receiver 'nope'" in caplog.text and "ops" in caplog.text


def test_two_tests_in_one_second_do_not_share_a_delivery_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = DeliveryResult("delivered", 200, 1)
    _, first, _ = _test_send(monkeypatch, tmp_path, capsys, ok, "--test-receiver", "ops")
    _, second, _ = _test_send(monkeypatch, tmp_path, capsys, ok, "--test-receiver", "ops")

    assert first.sent[0][1] != second.sent[0][1]


def test_an_empty_test_receiver_is_refused_not_run_as_an_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, notifier, out = _test_send(
        monkeypatch, tmp_path, capsys, DeliveryResult("delivered", 200, 1), "--test-receiver", ""
    )

    assert code == 2 and notifier.sent == [] and out == ""
