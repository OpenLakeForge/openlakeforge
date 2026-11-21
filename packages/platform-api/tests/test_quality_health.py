"""Quality signals joined to each pipeline's signals, read from the per-run manifests in the ops bucket."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _alerting_fakes import FakeBucket, declare_everything, everything, publish
from conftest import Mint
from fastapi.testclient import TestClient
from openlakeforge_core import parse_alert_policy, resolve_policy
from openlakeforge_core.quality import (
    PipelineOutcome,
    QualityManifest,
    QualityOutcome,
    QualityResult,
    manifest_key,
)
from openlakeforge_services.dagster import DagsterClient, DagsterJob, DagsterRun, DagsterSchedule
from test_alerting import NOW, A, FakeNotifier, _settings
from test_health import PIPELINES, STAGE, ScheduledDagster
from test_reads import _as
from test_reads import _settings as _api_settings

from openlakeforge_platform_api.alerting.run import run_once
from openlakeforge_platform_api.alerting.store import AlertStore
from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.pipeline_health import DOMAIN_TAG, PRODUCT_TAG, read_stage_pipelines
from openlakeforge_platform_api.quality_health import QualityReader
from openlakeforge_platform_api.settings import Settings, SettingsError

BASE = "s3://ops/activations/dev/"
PREFIX = "activations/dev/"
JOB = "orders_pipeline"
# The definition tags of the product job `JOB`; its runs' tags are not read for its domain or product.
PRODUCT_JOB = {DOMAIN_TAG: "sales", PRODUCT_TAG: "orders"}


class FakeDagster(DagsterClient):
    def __init__(self, runs: list[DagsterRun], job: str = JOB, tags: dict[str, str] | None = None) -> None:
        super().__init__("http://dagster.invalid/graphql")
        self.runs, self.job = runs, job
        self.tags = PRODUCT_JOB if tags is None else tags

    def list_jobs(self) -> list[DagsterJob]:
        return [DagsterJob("loc", "repo", self.job, "", self.tags)]

    def list_schedules(self) -> list[DagsterSchedule]:
        return []

    def list_runs(self, *, job_name: str = "", limit: int = 20) -> list[DagsterRun]:
        return self.runs


def _run(run_id: str = "run-1", status: str = "SUCCESS") -> DagsterRun:
    stamp = (NOW - timedelta(minutes=30)).timestamp()
    return DagsterRun(run_id, JOB, status, stamp - 60, stamp - 59, stamp, {})


def _result(
    outcome: QualityOutcome,
    pipeline_outcome: PipelineOutcome = PipelineOutcome.CONTINUED,
    *,
    check: str = "not_null",
    table: str | None = "orders",
    run_id: str = "run-1",
) -> QualityResult:
    return QualityResult(
        engine="floe",
        stage="dev",
        domain="sales",
        product="orders",
        table=table,
        check_id=check,
        engine_severity="error",
        outcome=outcome,
        pipeline_outcome=pipeline_outcome,
        observed_at=NOW - timedelta(minutes=20),
        dagster_run_id=run_id,
    )


def _declared() -> FakeBucket:
    bucket = FakeBucket()
    declare_everything(bucket, ("dev",))
    return bucket


def _store(*results: QualityResult, run_id: str = "run-1", complete: bool = True) -> FakeBucket:
    bucket = FakeBucket()
    if results:
        manifest = QualityManifest("dev", "sales", "orders", run_id, results, complete=complete)
        bucket.objects[manifest.ops_key] = manifest.to_json().encode()
    return bucket


def _quality(
    bucket: FakeBucket, run: DagsterRun | None = None, tags: dict[str, str] | None = None
) -> dict[str, str] | None:
    """The quality signal's state and reason for the pipeline's latest run, or None when it carries none.

    `tags` are the job definition's, the product job's by default."""
    dagster = FakeDagster([run or _run()], tags=tags)
    _, (pipeline,) = read_stage_pipelines(dagster, "dev", NOW, QualityReader(bucket, BASE))
    found = [signal for signal in pipeline.signals if signal.kind.value == "quality"]
    return {"state": found[0].state.value, "reason": found[0].reason} if found else None


def _state(bucket: FakeBucket, run: DagsterRun | None = None, tags: dict[str, str] | None = None) -> str | None:
    found = _quality(bucket, run, tags)
    return found["state"] if found else None


def test_a_warn_reads_warning() -> None:
    assert _state(_store(_result(QualityOutcome.WARN))) == "warning"


def test_a_blocking_error_reads_critical() -> None:
    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED)

    assert _quality(_store(blocked)) == {"state": "critical", "reason": "a blocking quality check failed"}


def test_an_error_that_only_quarantined_rows_reads_warning() -> None:
    quarantined = _result(QualityOutcome.ERROR, PipelineOutcome.QUARANTINED)

    assert _state(_store(quarantined)) == "warning"


def test_all_checks_passing_reads_healthy() -> None:
    assert _state(_store(_result(QualityOutcome.PASSED))) == "healthy"


def test_no_manifest_reads_unknown_never_healthy() -> None:
    assert _state(_store()) == "unknown"


def test_the_worst_outcome_of_the_manifest_wins() -> None:
    bucket = _store(
        _result(QualityOutcome.PASSED, check="a"),
        _result(QualityOutcome.WARN, check="b"),
        _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, check="c"),
    )

    assert _state(bucket) == "critical"


def test_a_check_that_never_reported_does_not_make_a_pass_healthy() -> None:
    bucket = _store(_result(QualityOutcome.PASSED, check="a"), _result(QualityOutcome.NOT_RUN, check="b"))

    assert _state(bucket) == "unknown"


def test_a_failure_still_stands_beside_a_check_that_never_reported() -> None:
    bucket = _store(_result(QualityOutcome.NOT_RUN, check="a"), _result(QualityOutcome.WARN, check="b"))

    assert _state(bucket) == "warning"


def test_a_manifest_without_table_identity_still_counts() -> None:
    # The baked dbt manifest names no table (#1166): the result carries none, and the failure is still the product's.
    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, table=None)

    assert _state(_store(blocked)) == "critical"


def test_a_manifest_older_than_the_evidence_bound_reads_unknown() -> None:
    old = QualityResult(
        **{**_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED).__dict__, "observed_at": NOW - timedelta(days=30)}
    )

    assert _state(_store(old)) == "unknown"


def test_the_latest_run_is_the_one_read_not_an_earlier_manifest() -> None:
    bucket = _store(_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-1"), run_id="run-1")

    assert _state(bucket, _run("run-2")) == "unknown"


def test_a_manifest_that_is_not_json_or_not_this_runs_reads_unknown() -> None:
    corrupt = FakeBucket()
    corrupt.objects[PREFIX + manifest_key("sales", "orders", "run-1")] = b"{not json"
    elsewhere = _store(_result(QualityOutcome.PASSED, run_id="run-9"), run_id="run-9")
    elsewhere.objects[PREFIX + manifest_key("sales", "orders", "run-1")] = elsewhere.objects.popitem()[1]

    assert _state(corrupt) == "unknown"
    assert _state(elsewhere) == "unknown"


def test_a_store_that_cannot_be_read_reads_unknown() -> None:
    bucket = _store(_result(QualityOutcome.PASSED))
    bucket.down = True

    assert _state(bucket) == "unknown"


@pytest.mark.parametrize("tag", [DOMAIN_TAG, PRODUCT_TAG])
@pytest.mark.parametrize("value", ["../other", "a/b", "", "sales\n", "%2e%2e"])
def test_a_tag_that_is_not_a_plain_name_never_becomes_a_key_segment(tag: str, value: str) -> None:
    requested: list[str] = []

    class Spy(FakeBucket):
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            requested.append(Key)
            return super().get_object(Bucket=Bucket, Key=Key)

    assert _state(Spy(), tags={**PRODUCT_JOB, tag: value}) == "unknown"
    assert requested == []


def test_a_job_that_is_not_a_product_pipeline_carries_no_quality_signal() -> None:
    assert _quality(_store(), tags={}) is None


def test_a_job_deployed_before_the_product_tag_carries_no_quality_signal_rather_than_a_guess() -> None:
    # The job is named like a product pipeline and its manifest is there, but no product is read from the name.
    assert _quality(_store(_result(QualityOutcome.WARN)), tags={DOMAIN_TAG: "sales"}) is None


def test_without_a_reader_a_pipeline_carries_no_quality_signal() -> None:
    _, (pipeline,) = read_stage_pipelines(FakeDagster([_run()]), "dev", NOW)

    assert {signal.kind.value for signal in pipeline.signals} == {
        "execution",
        "latest_success",
        "expected_run",
        "freshness",
    }


def test_the_alert_evaluator_opens_a_quality_incident_from_a_blocking_error_manifest() -> None:
    state, notifier = _declared(), FakeNotifier()
    manifests = QualityReader(_store(_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED)), BASE)

    run_once(
        settings=_settings(A),
        store=AlertStore(state, "s3://ops/alerts/"),
        dagster={"dev": FakeDagster([_run()])},
        notifiers={"ops": notifier},
        now=NOW,
        quality={"dev": manifests},
    )

    (payload,) = [payload for _, payload in notifier.sent]
    assert (payload["event"], payload["condition"], payload["severity"]) == ("opened", "quality", "critical")
    assert payload["links"]["run"] == "http://dagster-dev.example.test/runs/run-1"


@pytest.mark.parametrize("tags", [PRODUCT_JOB, {DOMAIN_TAG: "sales"}, {}], ids=["product", "domain-only", "untagged"])
def test_a_jobs_domain_and_product_never_re_key_its_incidents(tags: dict[str, str]) -> None:
    state, notifier = _declared(), FakeNotifier()
    manifests = QualityReader(_store(_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED)), BASE)

    run_once(
        settings=_settings(A),
        store=AlertStore(state, "s3://ops/alerts/"),
        dagster={"dev": FakeDagster([_run(status="FAILURE")], tags=tags)},
        notifiers={"ops": notifier},
        now=NOW,
        quality={"dev": manifests},
    )

    opened = {payload["condition"]: payload for _, payload in notifier.sent}
    # The fingerprints of `Scope(stage="dev", product=JOB)` before #1272: the scope names the job, whatever its tags.
    pinned = {"run_failed": "1c0286ead372c62a", "quality": "4ba0651cd835f470"}
    # Only a job that names its product has a quality signal to open an incident from.
    expected = pinned if tags == PRODUCT_JOB else {"run_failed": pinned["run_failed"]}
    assert {condition: payload["incident_id"].split("-")[0] for condition, payload in opened.items()} == expected
    assert {(payload.get("domain"), payload["product"]) for payload in opened.values()} == {(None, JOB)}


def test_the_alert_evaluator_opens_nothing_for_a_pipeline_whose_manifest_passed_or_is_missing() -> None:
    for bucket in (_store(_result(QualityOutcome.PASSED)), _store()):
        notifier = FakeNotifier()
        run_once(
            settings=_settings(A),
            store=AlertStore(_declared(), "s3://ops/alerts/"),
            dagster={"dev": FakeDagster([_run()])},
            notifiers={"ops": notifier},
            now=NOW,
            quality={"dev": QualityReader(bucket, BASE)},
        )

        assert notifier.sent == []


@pytest.mark.parametrize(
    "results",
    [[None], [[]], "text", {"a": 1}, [{"version": 1, "metrics": "x"}]],
    ids=["null-entry", "list-entry", "string", "object", "partial-entry"],
)
def test_a_manifest_with_malformed_results_reads_unknown(results: object) -> None:
    document = json.loads(
        _store(_result(QualityOutcome.PASSED)).objects[PREFIX + manifest_key("sales", "orders", "run-1")]
    )
    document["results"] = results
    bucket = FakeBucket()
    bucket.objects[PREFIX + manifest_key("sales", "orders", "run-1")] = json.dumps(document).encode()

    assert _state(bucket) == "unknown"


def _two_runs(*statuses: str) -> list[DagsterRun]:
    return [_run(f"run-{n}", status) for n, status in reversed(list(enumerate(statuses, 1)))]


def _states(runs: list[DagsterRun], *manifests: tuple[str, QualityResult], complete: bool = True) -> str | None:
    bucket = FakeBucket()
    for run_id, outcome in manifests:
        stored = QualityManifest("dev", "sales", "orders", run_id, (outcome,), complete=complete)
        bucket.objects[stored.ops_key] = stored.to_json().encode()
    _, (pipeline,) = read_stage_pipelines(FakeDagster(runs), "dev", NOW, QualityReader(bucket, BASE))
    found = [signal for signal in pipeline.signals if signal.kind.value == "quality"]
    return found[0].state.value if found else None


def test_a_running_run_with_every_check_so_far_passing_is_not_healthy() -> None:
    # The retry (run-2) has cleared Silver; the failed run-1's blocking error still speaks.
    failed = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-1")
    partial = _result(QualityOutcome.PASSED, run_id="run-2")

    assert _states(_two_runs("FAILURE", "STARTED"), ("run-1", failed), ("run-2", partial)) == "critical"
    assert (
        _states(
            _two_runs("SUCCESS", "STARTED"),
            ("run-1", _result(QualityOutcome.PASSED, run_id="run-1")),
            ("run-2", partial),
        )
        == "healthy"
    )


def test_a_successful_run_with_an_incomplete_passing_manifest_reads_unknown() -> None:
    silver_only = _result(QualityOutcome.PASSED, run_id="run-2")
    old = _result(QualityOutcome.PASSED, run_id="run-1")

    assert _states(_two_runs("SUCCESS"), ("run-1", old), complete=False) == "unknown"
    # A newer success is not walked past to an older complete one.
    assert _states(_two_runs("SUCCESS", "SUCCESS"), ("run-1", old), ("run-2", silver_only), complete=False) == "unknown"
    assert _state(_store(_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED), complete=False)) == "critical"


def test_a_failed_run_with_a_silver_only_passing_manifest_is_not_healthy() -> None:
    silver_only = _result(QualityOutcome.PASSED, run_id="run-1")

    assert _states(_two_runs("FAILURE"), ("run-1", silver_only)) is None


@pytest.mark.parametrize("retry", ["STARTED", "FAILURE"])
def test_a_failure_between_an_old_success_and_a_silver_only_retry_still_speaks(retry: str) -> None:
    old = _result(QualityOutcome.PASSED, run_id="run-1")
    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-2")
    silver_only = _result(QualityOutcome.PASSED, run_id="run-3")
    manifests = (("run-1", old), ("run-2", blocked), ("run-3", silver_only))

    assert _states(_two_runs("SUCCESS", "FAILURE", retry), *manifests) == "critical"


def test_a_success_after_a_run_whose_manifest_is_unreadable_cannot_read_healthy() -> None:
    old = _result(QualityOutcome.PASSED, run_id="run-1")
    bucket = _store(old)
    bucket.objects[PREFIX + manifest_key("sales", "orders", "run-2")] = b"{not json"

    def read(bucket: FakeBucket, *statuses: str) -> str | None:
        runs = _two_runs("SUCCESS", *statuses)
        _, (pipeline,) = read_stage_pipelines(FakeDagster(runs), "dev", NOW, QualityReader(bucket, BASE))
        return next((s.state.value for s in pipeline.signals if s.kind.value == "quality"), None)

    assert read(bucket, "FAILURE") == "unknown"
    assert read(bucket, "STARTED") == "unknown"
    # A run still in progress with no manifest yet hides nothing.
    assert read(_store(old), "STARTED") == "healthy"


def _signal_of(runs: list[DagsterRun], bucket: FakeBucket) -> dict[str, str | None] | None:
    _, (pipeline,) = read_stage_pipelines(FakeDagster(runs), "dev", NOW, QualityReader(bucket, BASE))
    found = next((s for s in pipeline.signals if s.kind.value == "quality"), None)
    return {"state": found.state.value, "run": found.run, "reason": found.reason} if found else None


@pytest.mark.parametrize(("status", "verb"), [("FAILURE", "failed"), ("CANCELED", "was canceled")])
@pytest.mark.parametrize("manifest", ["absent", "incomplete"])
def test_a_run_that_ended_before_its_checks_finished_hides_an_older_pass(status: str, verb: str, manifest: str) -> None:
    bucket = _store(_result(QualityOutcome.PASSED, run_id="run-1"))
    if manifest == "incomplete":
        partial = QualityManifest("dev", "sales", "orders", "run-2", (_result(QualityOutcome.PASSED, run_id="run-2"),))
        bucket.objects[partial.ops_key] = partial.to_json().encode()

    assert _signal_of(_two_runs("SUCCESS", status), bucket) == {
        "state": "unknown",
        "run": "run-2",
        "reason": f"the run {verb} before its quality checks finished",
    }


def test_a_run_that_ended_before_its_checks_finished_with_no_success_before_it_reads_unknown() -> None:
    assert _signal_of(_two_runs("FAILURE"), FakeBucket()) == {
        "state": "unknown",
        "run": "run-1",
        "reason": "the run failed before its quality checks finished",
    }


def test_a_failure_older_than_a_run_that_did_not_finish_its_checks_still_speaks() -> None:
    old = _result(QualityOutcome.PASSED, run_id="run-1")
    bucket = _store(old)
    blocked = QualityManifest(
        "dev", "sales", "orders", "run-2", (_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-2"),)
    )
    bucket.objects[blocked.ops_key] = blocked.to_json().encode()

    found = _signal_of(_two_runs("SUCCESS", "FAILURE", "FAILURE"), bucket)

    assert found is not None and (found["state"], found["run"]) == ("critical", "run-2")


def test_a_run_in_progress_leaves_the_last_complete_success_answering() -> None:
    bucket = _store(_result(QualityOutcome.PASSED, run_id="run-1"))

    found = _signal_of(_two_runs("SUCCESS", "STARTED"), bucket)

    assert found is not None and (found["state"], found["run"]) == ("healthy", "run-1")


def test_a_run_that_did_not_finish_its_checks_never_resolves_a_quality_incident() -> None:
    store, notifier = AlertStore(_declared(), "s3://ops/alerts/"), FakeNotifier()
    old = _result(QualityOutcome.PASSED, run_id="run-1")
    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-2")

    def evaluate(runs: list[DagsterRun], *results: QualityResult, minutes: int) -> list[str]:
        bucket = FakeBucket()
        for result in results:
            manifest = QualityManifest("dev", "sales", "orders", result.dagster_run_id, (result,), complete=True)
            bucket.objects[manifest.ops_key] = manifest.to_json().encode()
        notifier.sent.clear()
        run_once(
            settings=_settings(A),
            store=store,
            dagster={"dev": FakeDagster(runs)},
            notifiers={"ops": notifier},
            now=NOW + timedelta(minutes=minutes),
            quality={"dev": QualityReader(bucket, BASE)},
        )
        return [f"{p['event']}:{p['condition']}" for _, p in notifier.sent if p["condition"] == "quality"]

    assert evaluate(_two_runs("SUCCESS", "FAILURE"), old, blocked, minutes=0) == ["opened:quality"]
    # run-3 failed before writing a manifest. With run-2's manifest gone too (retention, a store reset), the walk
    # used to pass over both and let run-1's pass resolve the incident.
    assert evaluate(_two_runs("SUCCESS", "FAILURE", "FAILURE"), old, blocked, minutes=5) == []
    assert evaluate(_two_runs("SUCCESS", "FAILURE", "FAILURE"), old, minutes=10) == []
    passed = _result(QualityOutcome.PASSED, run_id="run-4")
    assert evaluate(_two_runs("SUCCESS", "FAILURE", "FAILURE", "SUCCESS"), old, passed, minutes=15) == [
        "resolved:quality"
    ]


def test_a_failed_run_with_a_blocking_error_still_reads_critical() -> None:
    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-1")

    assert _states(_two_runs("FAILURE"), ("run-1", blocked)) == "critical"


def test_a_running_run_with_no_finished_run_before_it_carries_no_quality_signal() -> None:
    partial = _result(QualityOutcome.PASSED, run_id="run-1")

    assert _states(_two_runs("STARTED"), ("run-1", partial)) is None


def test_a_running_run_still_reports_a_blocking_error_and_a_warning() -> None:
    passed = _result(QualityOutcome.PASSED, run_id="run-1")
    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-2")
    warned = _result(QualityOutcome.WARN, run_id="run-2")

    assert _states(_two_runs("SUCCESS", "STARTED"), ("run-1", passed), ("run-2", blocked)) == "critical"
    assert _states(_two_runs("SUCCESS", "STARTED"), ("run-1", passed), ("run-2", warned)) == "warning"


def test_a_finished_run_after_a_running_one_resolves_through_its_own_manifest() -> None:
    passed = _result(QualityOutcome.PASSED, run_id="run-2")

    assert _states(_two_runs("FAILURE", "SUCCESS"), ("run-2", passed)) == "healthy"


def test_a_running_retry_does_not_resolve_an_open_quality_incident_and_a_completed_one_does() -> None:
    state, notifier = _declared(), FakeNotifier()
    store = AlertStore(state, "s3://ops/alerts/")
    failed = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-1")

    def evaluate(
        runs: list[DagsterRun], *results: QualityResult, at: datetime, complete: bool = True, corrupt: str = ""
    ) -> list[str]:
        bucket = FakeBucket()
        if corrupt:
            bucket.objects[PREFIX + manifest_key("sales", "orders", corrupt)] = b"{not json"
        for result in results:
            manifest = QualityManifest("dev", "sales", "orders", result.dagster_run_id, (result,), complete=complete)
            bucket.objects[manifest.ops_key] = manifest.to_json().encode()
        notifier.sent.clear()
        run_once(
            settings=_settings(A),
            store=store,
            dagster={"dev": FakeDagster(runs)},
            notifiers={"ops": notifier},
            now=at,
            quality={"dev": QualityReader(bucket, BASE)},
        )
        return [
            f"{payload['event']}:{payload['condition']}"
            for _, payload in notifier.sent
            if payload["condition"] == "quality"
        ]

    assert evaluate(_two_runs("FAILURE"), failed, at=NOW) == ["opened:quality"]
    partial = _result(QualityOutcome.PASSED, run_id="run-2")
    assert evaluate(_two_runs("FAILURE", "STARTED"), failed, partial, at=NOW + timedelta(minutes=5)) == []
    # An old success before the failure does not speak for a Silver-only retry either.
    old = _result(QualityOutcome.PASSED, run_id="run-1")
    failed2 = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-2")
    silver_only = _result(QualityOutcome.PASSED, run_id="run-3")
    for retry in ("STARTED", "FAILURE"):
        runs = _two_runs("SUCCESS", "FAILURE", retry)
        assert evaluate(runs, old, failed2, silver_only, at=NOW + timedelta(minutes=6)) == []
    # A retry that failed (dbt worker died) after a passing Silver step leaves a Silver-only manifest.
    assert evaluate(_two_runs("FAILURE", "FAILURE"), failed, partial, at=NOW + timedelta(minutes=7)) == []
    # Still open: the retry failing in dbt does not notify a second time.
    blocked_again = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-2")
    assert evaluate(_two_runs("FAILURE", "FAILURE"), failed, blocked_again, at=NOW + timedelta(minutes=10)) == []
    # A success whose manifest the run never completed is not a recovery.
    unfinished = _result(QualityOutcome.PASSED, run_id="run-3")
    assert (
        evaluate(_two_runs("FAILURE", "FAILURE", "SUCCESS"), unfinished, at=NOW + timedelta(minutes=12), complete=False)
        == []
    )
    passed = _result(QualityOutcome.PASSED, run_id="run-3")
    assert evaluate(_two_runs("FAILURE", "FAILURE", "SUCCESS"), passed, at=NOW + timedelta(minutes=15)) == [
        "resolved:quality"
    ]


def test_an_unreadable_failed_manifest_keeps_an_open_incident_open_and_silent() -> None:
    store = AlertStore(_declared(), "s3://ops/alerts/")
    notifier = FakeNotifier()
    old = _result(QualityOutcome.PASSED, run_id="run-1")
    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-2")
    runs = _two_runs("SUCCESS", "FAILURE")

    def evaluate(*results: QualityResult, corrupt: str = "", minutes: int) -> list[str]:
        bucket = FakeBucket()
        for result in results:
            manifest = QualityManifest("dev", "sales", "orders", result.dagster_run_id, (result,), complete=True)
            bucket.objects[manifest.ops_key] = manifest.to_json().encode()
        if corrupt:
            bucket.objects[PREFIX + manifest_key("sales", "orders", corrupt)] = b"{not json"
        notifier.sent.clear()
        run_once(
            settings=_settings(A),
            store=store,
            dagster={"dev": FakeDagster(runs)},
            notifiers={"ops": notifier},
            now=NOW + timedelta(minutes=minutes),
            quality={"dev": QualityReader(bucket, BASE)},
        )
        return [p["event"] for _, p in notifier.sent if p["condition"] == "quality"]

    def open_quality() -> list[str]:
        return [i.kind.value for i in store.read_state(NOW + timedelta(hours=1)).incidents if i.kind.value == "quality"]

    assert evaluate(old, blocked, minutes=0) == ["opened"]
    assert evaluate(old, corrupt="run-2", minutes=5) == []
    assert open_quality() == ["quality"]
    assert evaluate(old, blocked, minutes=10) == []
    assert open_quality() == ["quality"]


class QualityDagster(ScheduledDagster):
    """A Dagster whose one job is a product pipeline with a run."""

    def __init__(self) -> None:
        super().__init__()
        stamp = time.time() - 1800
        self.runs = [
            {
                "runId": "run-1",
                "jobName": JOB,
                "status": "SUCCESS",
                "creationTime": stamp - 60,
                "startTime": stamp - 59,
                "endTime": stamp,
                "tags": [],
            }
        ]

    def answer(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "query Jobs" in query:
            tags = [{"key": key, "value": value} for key, value in PRODUCT_JOB.items()]
            repository = {
                "name": "__repository__",
                "location": {"name": "loc"},
                "jobs": [{"name": JOB, "description": "", "tags": tags}],
            }
            return {"repositoriesOrError": {"__typename": "RepositoryConnection", "nodes": [repository]}}
        return super().answer(query, variables)


@pytest.fixture
def quality_dagster() -> Iterator[QualityDagster]:
    fake = QualityDagster()
    yield fake
    fake.close()


def _api(settings: Settings, dagster: QualityDagster, readers: dict[str, QualityReader]) -> TestClient:
    configured = _api_settings(settings, dagster_graphql_urls={"dev": f"{dagster.url}/graphql"})
    return TestClient(create_app(configured, quality_readers=readers))


def _blocking_manifest(observed: datetime) -> FakeBucket:
    result = QualityResult(
        **{**_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED).__dict__, "observed_at": observed}
    )
    return _store(result)


def test_the_pipeline_route_carries_the_quality_signal_and_the_integration_is_available(
    settings: Settings, mint: Mint, quality_dagster: QualityDagster
) -> None:
    bucket = _blocking_manifest(datetime.now(UTC) - timedelta(minutes=20))
    api = _api(settings, quality_dagster, {"dev": QualityReader(bucket, BASE)})

    body = api.get(PIPELINES, headers=_as(mint, "data-engineer")).json()
    stage = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    (pipeline,) = body["pipelines"]
    quality = next(signal for signal in pipeline["signals"] if signal["kind"] == "quality")
    assert (quality["state"], quality["run"], quality["due"]) == ("critical", "run-1", None)
    assert {item["name"]: item["status"] for item in body["integrations"]}["quality"] == "available"
    assert {item["name"]: item["status"] for item in stage["integrations"]}["quality"] == "available"
    assert stage["state"] == "critical"


def test_without_a_reader_the_quality_integration_is_not_provided_and_no_signal_is_added(
    settings: Settings, mint: Mint, quality_dagster: QualityDagster
) -> None:
    body = _api(settings, quality_dagster, {}).get(PIPELINES, headers=_as(mint, "data-engineer")).json()

    (pipeline,) = body["pipelines"]
    assert "quality" not in {signal["kind"] for signal in pipeline["signals"]}
    assert {item["name"]: item["status"] for item in body["integrations"]}["quality"] == "not_provided"


def test_the_route_answers_carry_no_check_names(
    settings: Settings, mint: Mint, quality_dagster: QualityDagster
) -> None:
    api = _api(settings, quality_dagster, {"dev": QualityReader(_blocking_manifest(datetime.now(UTC)), BASE)})

    for path in (PIPELINES, STAGE):
        assert "not_null" not in api.get(path, headers=_as(mint, "data-engineer")).text


def test_a_quality_prefix_for_a_stage_that_is_not_enabled_is_refused(settings: Settings) -> None:
    with pytest.raises(SettingsError, match="not enabled"):
        _api_settings(settings, quality_manifests={"staging": "s3://ops/activations/staging/"})
    with pytest.raises(SettingsError, match="ending in /"):
        _api_settings(settings, quality_manifests={"dev": "s3://ops/activations/dev"})


def test_a_warn_before_a_non_blocking_error_in_one_manifest_still_reports_the_error() -> None:
    warn = _result(QualityOutcome.WARN, check="a_warn")
    error = _result(QualityOutcome.ERROR, PipelineOutcome.CONTINUED, check="b_error")

    for results in ((warn, error), (error, warn)):
        _, (pipeline,) = read_stage_pipelines(FakeDagster([_run()]), "dev", NOW, QualityReader(_store(*results), BASE))
        (quality,) = [s for s in pipeline.signals if s.kind.value == "quality"]

        assert (quality.state.value, quality.outcome) == ("warning", QualityOutcome.ERROR)


def _level_error_signal(runs: list[DagsterRun], stored: list[QualityManifest]):
    bucket = FakeBucket()
    for manifest in stored:
        bucket.objects[manifest.ops_key] = manifest.to_json().encode()
    policy = parse_alert_policy(
        {
            "apiVersion": "openlakeforge.io/v1beta1",
            "kind": "AlertPolicy",
            "routes": {"ops": "ops"},
            "defaults": {"notify": ["ops"]},
            "rules": [{"match": {"domain": "*", "product": "*"}, "when": {"quality": {"level": "error"}}}],
        }
    )
    resolved = resolve_policy(policy, {"domains": []}, "dev")
    _, (pipeline,) = read_stage_pipelines(FakeDagster(runs), "dev", NOW, QualityReader(bucket, BASE), resolved)
    return [signal for signal in pipeline.signals if signal.kind.value == "quality"]


def test_level_error_does_not_let_a_newer_runs_warn_hide_an_older_runs_blocking_error() -> None:
    failed = QualityManifest(
        "dev",
        "sales",
        "orders",
        "run-1",
        (_result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-1"),),
        complete=True,
    )
    retry = QualityManifest(
        "dev", "sales", "orders", "run-2", (_result(QualityOutcome.WARN, run_id="run-2"),), complete=False
    )

    (signal,) = _level_error_signal(_two_runs("FAILURE", "STARTED"), [failed, retry])

    assert (signal.state.value, signal.run) == ("critical", "run-1")


@pytest.mark.parametrize("order", [("a_warn", "b_error"), ("a_error", "b_warn")])
def test_level_error_reads_a_warn_beside_a_non_blocking_error_as_the_error_whatever_the_order(
    order: tuple[str, str],
) -> None:
    checks = {
        "a_warn": _result(QualityOutcome.WARN, check="a_warn"),
        "b_warn": _result(QualityOutcome.WARN, check="b_warn"),
        "a_error": _result(QualityOutcome.ERROR, PipelineOutcome.CONTINUED, check="a_error"),
        "b_error": _result(QualityOutcome.ERROR, PipelineOutcome.CONTINUED, check="b_error"),
    }
    manifest = QualityManifest("dev", "sales", "orders", "run-1", tuple(checks[name] for name in order), complete=True)

    (signal,) = _level_error_signal([_run()], [manifest])

    assert (signal.state.value, signal.outcome) == ("warning", QualityOutcome.ERROR)


def test_level_error_reads_a_complete_manifest_of_only_warns_as_a_pass_and_an_incomplete_one_as_unknown() -> None:
    warn = _result(QualityOutcome.WARN)

    (complete,) = _level_error_signal(
        [_run()], [QualityManifest("dev", "sales", "orders", "run-1", (warn,), complete=True)]
    )
    (partial,) = _level_error_signal(
        [_run()], [QualityManifest("dev", "sales", "orders", "run-1", (warn,), complete=False)]
    )

    assert (complete.state.value, partial.state.value) == ("healthy", "unknown")


@pytest.mark.parametrize("extra", [(), (QualityOutcome.PASSED,)], ids=["warn only", "passed and warn"])
def test_level_error_a_complete_retry_with_only_warns_resolves_the_blocking_error_incident(
    extra: tuple[QualityOutcome, ...],
) -> None:
    bucket = FakeBucket()
    publish(bucket, alerts=everything().replace("quality: {}", "quality: {level: error}"))
    notifier, store, manifests = FakeNotifier(), AlertStore(bucket, "s3://ops/alerts/"), FakeBucket()

    def tick(runs: list[DagsterRun], stored: QualityManifest, minutes: int) -> list[str]:
        manifests.objects[stored.ops_key] = stored.to_json().encode()
        notifier.sent.clear()
        run_once(
            settings=_settings(A),
            store=store,
            dagster={"dev": FakeDagster(runs)},
            notifiers={"ops": notifier},
            now=NOW + timedelta(minutes=minutes),
            quality={"dev": QualityReader(manifests, BASE)},
        )
        return [f"{p['event']}:{p['severity']}" for _, p in notifier.sent if p["condition"] == "quality"]

    blocked = _result(QualityOutcome.ERROR, PipelineOutcome.BLOCKED, run_id="run-1")
    assert tick(
        _two_runs("FAILURE"), QualityManifest("dev", "sales", "orders", "run-1", (blocked,), complete=True), 0
    ) == ["opened:critical"]
    results = tuple(
        _result(outcome, run_id="run-2", check=f"check_{index}")
        for index, outcome in enumerate((*extra, QualityOutcome.WARN))
    )
    retry = QualityManifest("dev", "sales", "orders", "run-2", results, complete=True)
    assert tick(_two_runs("FAILURE", "SUCCESS"), retry, 5) == ["resolved:critical"]
