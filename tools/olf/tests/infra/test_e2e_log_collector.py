from __future__ import annotations

from types import SimpleNamespace

import pytest

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

    _log_collector.check_short_lived_pod_output(SimpleNamespace(namespace="olf-dev"))  # type: ignore[arg-type]

    assert seen == [[], [], ["pod-a"], ["pod-a"], []]


class _NullArchive:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc: object) -> None:
        return None
