"""Backoff, state and summary rules of quality publication (#797)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from openlakeforge_core.quality import QualityRecordError
from openlakeforge_core.quality_sync import (
    BACKOFF_CAP,
    MAX_ATTEMPTS,
    ResultState,
    ResultSync,
    RunSync,
    SyncSummary,
    summarize,
)

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


def test_backoff_doubles_up_to_the_cap_then_gives_up():
    sync = ResultSync("a")
    delays = []
    for _ in range(MAX_ATTEMPTS - 1):
        sync = sync.retry(NOW, "down")
        assert sync.next_attempt_at is not None
        delays.append(sync.next_attempt_at - NOW)
    assert delays[:3] == [timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=4)]
    assert max(delays) == BACKOFF_CAP
    given_up = sync.retry(NOW, "down")
    assert given_up.state is ResultState.FAILED
    assert not given_up.due(NOW + timedelta(days=1))


def test_a_reason_is_one_bounded_line():
    multiline = ResultSync("a").retry(NOW, "x\n" * 1000).reason or ""
    assert "\n" not in multiline
    assert len(ResultSync("a").retry(NOW, "x" * 5000).reason or "") <= 300


def test_state_round_trips_and_refuses_a_foreign_key():
    run = RunSync("dev", "sales", "revenue", "r1", NOW, {"a": ResultSync("a").retry(NOW, "down")})
    assert RunSync.from_json(run.to_json()) == run
    tampered = run.to_json().replace('"stage": "dev"', '"stage": "prod"')
    with pytest.raises(QualityRecordError):
        RunSync.from_json(tampered)


def test_summary_reports_lag_of_the_oldest_unpublished_result():
    old = RunSync("dev", "s", "p", "r1", NOW - timedelta(hours=2), {"a": ResultSync("a")})
    done = RunSync("dev", "s", "p", "r2", NOW - timedelta(hours=5), {"b": ResultSync("b").synced()})
    summary = summarize("dev", [old, done], now=NOW)
    assert (summary.pending, summary.synced, summary.lag_seconds) == (1, 1, 7200)
    assert SyncSummary.from_json(summary.to_json()) == summary
