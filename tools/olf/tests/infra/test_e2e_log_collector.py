from __future__ import annotations

import contextlib
import gzip
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from openlakeforge_services.logs import LogArchive

from olf.infra.e2e import _log_collector
from olf.infra.e2e._shell import E2EError


def test_a_dropped_and_a_duplicated_line_are_both_named() -> None:
    problems = _log_collector.exactly_once_problems([1, 2, 2, 5], 5)

    assert any("missing 2 of 5: 3-4" in problem for problem in problems)
    assert any("duplicated 1: 2" in problem for problem in problems)
    assert _log_collector.exactly_once_problems([3, 1, 2], 3) == []


def test_only_lines_of_this_runs_token_are_counted() -> None:
    messages = ["tok line 1 ERROR x", "other line 2 ERROR x", "collector started", "tok line 7 ERROR x"]

    assert _log_collector.line_numbers(messages, "tok") == [1, 7]


def test_the_emitter_carries_the_run_label_and_ttl_the_checks_rely_on() -> None:
    job = _log_collector.emitter_manifest(
        "n", "olf-dev", image="img", run_id="r-1", token="tok", lines=3, interval_seconds=0, ttl_seconds=30, fail=True
    )

    assert job["spec"]["ttlSecondsAfterFinished"] == 30
    assert job["spec"]["template"]["metadata"]["labels"]["dagster/run-id"] == "r-1"
    assert job["spec"]["template"]["spec"]["containers"][0]["command"][-1].endswith("exit 1")


def test_lines_that_appear_late_are_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_log_collector.time, "sleep", lambda _s: None)
    reads = iter([[1], [1, 2], [1, 2, 3], [1, 2, 3]])

    _log_collector.await_exactly_once(lambda: next(reads), 3, what="run")


def test_a_line_missing_at_the_deadline_is_a_drop() -> None:
    with pytest.raises(E2EError, match="missing 1 of 3: 3"):
        _log_collector.await_exactly_once(
            lambda: [1, 2], 3, what="run", timeout_seconds=0, poll_seconds=0, settle_seconds=0
        )


def test_a_duplicate_that_arrives_after_completion_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_log_collector.time, "sleep", lambda _s: None)
    reads = iter([[1, 2], [1, 2, 2]])

    with pytest.raises(E2EError, match="duplicated 1: 2"):
        _log_collector.await_exactly_once(lambda: next(reads), 2, what="run")


def test_the_short_lived_drill_sees_the_pod_before_waiting_for_its_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Job controller has not created the pod yet on the first poll: "no pod" must not count as "deleted".
    monkeypatch.setattr(_log_collector.time, "sleep", lambda _s: None)
    monkeypatch.setattr(_log_collector, "_start_emitter", lambda *_a, **_k: ("n", "r-1", "tok"))
    monkeypatch.setattr(_log_collector, "kubectl", lambda *_a, **_k: None)
    observed = iter([[], [], ["pod-a"], ["pod-a"], []])
    seen: list[list[str]] = []

    def pods(_cfg: object, _run_id: str) -> list[str]:
        seen.append(next(observed))
        return seen[-1]

    monkeypatch.setattr(_log_collector, "_pods_of", pods)
    monkeypatch.setattr(_log_collector, "_archive", lambda _cfg: _NullArchive())
    monkeypatch.setattr(_log_collector, "await_exactly_once", lambda *_a, **_k: None)
    monkeypatch.setattr(_log_collector, "check_hour_partitions", lambda *_a: None)

    _log_collector.check_short_lived_pod_output(SimpleNamespace(namespace="olf-dev"))  # type: ignore[arg-type]

    assert seen == [[], [], ["pod-a"], ["pod-a"], []]


class _NullArchive:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc: object) -> None:
        return None


def test_a_slow_emitter_start_is_waited_out_not_reported_as_a_collector_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `kubectl logs` fails while the pod is still being scheduled; the drill keeps polling instead of aborting.
    monkeypatch.setattr(_log_collector.time, "sleep", lambda _s: None)
    monkeypatch.setattr(_log_collector, "_start_emitter", lambda *_a, **_k: ("n", "r-1", "tok"))
    monkeypatch.setattr(_log_collector, "_collector_ready", lambda _cfg: True)
    monkeypatch.setattr(_log_collector, "_archive", lambda _cfg: _NullArchive())
    monkeypatch.setattr(_log_collector, "await_exactly_once", lambda *_a, **_k: None)
    log_reads = iter([E2EError("pod not running after 20s"), E2EError("still pending"), "tok line 30 ERROR x"])
    calls: list[str] = []

    def kubectl(_cfg: object, args: list[str], **_k: object) -> str:
        calls.append(args[0])
        if args[0] == "logs":
            result = next(log_reads)
            if isinstance(result, Exception):
                raise result
            return result
        return ""

    monkeypatch.setattr(_log_collector, "kubectl", kubectl)

    _log_collector.check_collector_restart(  # type: ignore[arg-type]
        SimpleNamespace(namespace="olf-dev", platform_namespace="olf-system")
    )

    assert calls.count("logs") == 3
    assert "delete" in calls


PREFIX = "activations/dev/logs/v2/namespace=olf-dev/"


class _Bucket:
    """The listing, paginator and get of a boto3 client over a dict of objects, with S3's delimiter."""

    def __init__(self, objects: dict[str, list[dict[str, Any]]], modified: datetime) -> None:
        self.objects = {
            key: gzip.compress("".join(json.dumps(r) + "\n" for r in rs).encode()) for key, rs in objects.items()
        }
        self.modified = modified

    def list_objects_v2(self, *, Bucket: str, Prefix: str, Delimiter: str = "", **_: str) -> dict[str, Any]:  # noqa: N803
        under = sorted(key for key in self.objects if key.startswith(Prefix))
        nested = [key for key in under if Delimiter and Delimiter in key.removeprefix(Prefix)]
        return {
            "Contents": [{"Key": key, "LastModified": self.modified} for key in under if key not in nested],
            "CommonPrefixes": [
                {"Prefix": p} for p in sorted({Prefix + k.removeprefix(Prefix).split("/")[0] + "/" for k in nested})
            ],
        }

    def get_paginator(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(paginate=lambda **kwargs: [self.list_objects_v2(**kwargs)])

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}


def _record(time: str, message: str, run_id: str = "r-1") -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v2",
        "time": time,
        "namespace": "olf-dev",
        "pod": "p",
        "container": "emit",
        "message": message,
        "run_id": run_id,
        "dedup_key": f"c:{time}",
    }


def test_an_object_is_in_place_only_under_the_date_and_hour_of_every_record() -> None:
    key = f"{PREFIX}date=2026-11-16/hour=09/dev-4f1b6c2e.ndjson.gz"

    assert _log_collector.partition_problems(key, [_record("2026-11-16T09:59:59.9Z", "tok line 1 ")]) == []
    assert "another hour" in _log_collector.partition_problems(key, [_record("2026-11-16T10:00:00Z", "x")])[0]
    flat = f"{PREFIX}date=2026-11-16/dev-4f1b6c2e.ndjson.gz"
    assert "not under date=<day>/hour=<HH>/" in _log_collector.partition_problems(flat, [])[0]


def _with_bucket(monkeypatch: pytest.MonkeyPatch, bucket: _Bucket) -> None:
    @contextlib.contextmanager
    def s3(_cfg: object) -> Iterator[tuple[_Bucket, str]]:
        yield bucket, "ops"

    monkeypatch.setattr(_log_collector, "_s3", s3)


def test_the_drill_fails_when_the_collector_keys_a_line_under_another_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    since = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    cfg = SimpleNamespace(namespace="olf-dev")
    good = {f"{PREFIX}date=2026-11-16/hour=09/dev-a.ndjson.gz": [_record("2026-11-16T09:30:00Z", "tok line 1 ")]}
    _with_bucket(monkeypatch, _Bucket(good, since))
    _log_collector.check_hour_partitions(cfg, "tok", since)  # type: ignore[arg-type]

    lying = {f"{PREFIX}date=2026-11-16/hour=10/dev-a.ndjson.gz": [_record("2026-11-16T09:30:00Z", "tok line 1 ")]}
    _with_bucket(monkeypatch, _Bucket(lying, since))
    with pytest.raises(E2EError, match="another hour"):
        _log_collector.check_hour_partitions(cfg, "tok", since)  # type: ignore[arg-type]


def test_the_drill_reads_a_run_from_the_hours_since_it_started(monkeypatch: pytest.MonkeyPatch) -> None:
    # A line keyed under an hour before the drill's start is not listed: the read finds what a run's read would.
    now = datetime.now(UTC).replace(microsecond=0)
    earlier = now - timedelta(hours=3)
    stamp = f"{now:%Y-%m-%dT%H:%M:%S}Z"
    objects = {
        f"{PREFIX}date={now:%Y-%m-%d}/hour={now:%H}/dev-a.ndjson.gz": [_record(stamp, "tok line 1 ")],
        f"{PREFIX}date={earlier:%Y-%m-%d}/hour={earlier:%H}/dev-b.ndjson.gz": [_record(stamp, "tok line 2 ")],
    }
    archive = LogArchive(_Bucket(objects, now), "s3://ops/activations/dev/logs")
    cfg = SimpleNamespace(namespace="olf-dev")

    assert _log_collector._run_lines(archive, cfg, "r-1", "tok", now) == [1]  # type: ignore[arg-type]  # noqa: SLF001
