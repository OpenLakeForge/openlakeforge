"""`latest_due` is the only source of a schedule's expected run, so its edges are pinned here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from openlakeforge_platform_api.schedules import latest_due


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


@pytest.mark.parametrize(
    ("cron", "timezone", "now", "expected"),
    [
        ("0 6 * * *", "UTC", _utc(2026, 11, 16, 12, 0), _utc(2026, 11, 16, 6, 0)),
        ("0 6 * * *", "UTC", _utc(2026, 11, 16, 6, 0), _utc(2026, 11, 16, 6, 0)),
        ("0 6 * * *", "UTC", _utc(2026, 11, 16, 5, 59, 59), _utc(2026, 11, 15, 6, 0)),
        ("* * * * *", "UTC", _utc(2026, 11, 16, 5, 59, 30), _utc(2026, 11, 16, 5, 59)),
        ("* * * * *", "UTC", _utc(2026, 11, 16, 6, 0, 0, 500), _utc(2026, 11, 16, 6, 0)),
        ("0 6 * * *", "Europe/Berlin", _utc(2026, 11, 16, 12, 0), _utc(2026, 11, 16, 5, 0)),
        ("0 6 * * *", "Europe/Berlin", _utc(2026, 7, 16, 12, 0), _utc(2026, 7, 16, 4, 0)),
    ],
)
def test_the_latest_occurrence_is_at_or_before_now(cron: str, timezone: str, now: datetime, expected: datetime) -> None:
    assert latest_due(cron, timezone, now) == expected


def test_a_wall_time_the_clock_skips_comes_due_when_the_gap_ends() -> None:
    # 02:30 does not exist in New York on 2026-03-08; the clock jumps from 02:00 EST to 03:00 EDT.
    assert latest_due("30 2 * * *", "America/New_York", _utc(2026, 3, 8, 12, 0)) == _utc(2026, 3, 8, 7, 0)
    assert latest_due("30 2 * * *", "America/New_York", _utc(2026, 3, 8, 6, 45)) == _utc(2026, 3, 7, 7, 30)


@pytest.mark.parametrize(
    ("cron", "timezone", "now", "expected"),
    [
        # 01:30 happens at 05:30Z and again at 06:30Z on 2026-11-01; Dagster fires the later pass (`_apply_fold`).
        ("30 1 * * *", "America/New_York", _utc(2026, 11, 1, 5, 45), _utc(2026, 10, 31, 5, 30)),
        ("30 1 * * *", "America/New_York", _utc(2026, 11, 1, 6, 29), _utc(2026, 10, 31, 5, 30)),
        ("30 1 * * *", "America/New_York", _utc(2026, 11, 1, 6, 30), _utc(2026, 11, 1, 6, 30)),
        ("30 1 * * *", "America/New_York", _utc(2026, 11, 1, 8, 45), _utc(2026, 11, 1, 6, 30)),
        # 02:00 happens at 00:00Z and again at 01:00Z on 2026-10-25; at 00:30Z Dagster has not fired yet.
        ("0 2 * * *", "Europe/Berlin", _utc(2026, 10, 25, 0, 30), _utc(2026, 10, 24, 0, 0)),
        ("0 2 * * *", "Europe/Berlin", _utc(2026, 10, 25, 1, 0), _utc(2026, 10, 25, 1, 0)),
    ],
)
def test_a_wall_time_the_clock_repeats_comes_due_at_the_pass_dagster_fires(
    cron: str, timezone: str, now: datetime, expected: datetime
) -> None:
    assert latest_due(cron, timezone, now) == expected


@pytest.mark.parametrize(
    ("cron", "now", "expected"),
    [
        # The repeated 01:00-02:00 hour in New York is 05:00Z-06:00Z and then 06:00Z-07:00Z; every pass fires.
        ("0 * * * *", _utc(2026, 11, 1, 5, 59), _utc(2026, 11, 1, 5, 0)),
        ("0 * * * *", _utc(2026, 11, 1, 6, 0), _utc(2026, 11, 1, 6, 0)),
        ("0 * * * *", _utc(2026, 11, 1, 6, 59), _utc(2026, 11, 1, 6, 0)),
        ("0 * * * *", _utc(2026, 11, 1, 7, 0), _utc(2026, 11, 1, 7, 0)),
        ("30 * * * *", _utc(2026, 11, 1, 5, 45), _utc(2026, 11, 1, 5, 30)),
        ("30 * * * *", _utc(2026, 11, 1, 6, 15), _utc(2026, 11, 1, 5, 30)),
        ("30 * * * *", _utc(2026, 11, 1, 6, 45), _utc(2026, 11, 1, 6, 30)),
    ],
)
def test_an_hourly_cron_comes_due_at_every_pass_of_the_repeated_hour(
    cron: str, now: datetime, expected: datetime
) -> None:
    assert latest_due(cron, "America/New_York", now) == expected


@pytest.mark.parametrize(
    ("cron", "timezone"),
    [("30 1 * * *", "America/New_York"), ("0 2 * * *", "Europe/Berlin"), ("30 * * * *", "America/New_York")],
)
def test_the_latest_due_time_never_moves_back_across_the_day_the_clocks_go_back(cron: str, timezone: str) -> None:
    now = _utc(2026, 10, 24, 12, 0)
    previous = latest_due(cron, timezone, now)
    assert previous is not None
    while now < _utc(2026, 11, 2, 12, 0):
        now += timedelta(minutes=5)
        current = latest_due(cron, timezone, now)
        assert current is not None
        assert previous <= current <= now
        previous = current


@pytest.mark.parametrize(
    ("cron", "timezone"),
    [("not a cron", "UTC"), ("['0 1 * * *', '0 2 * * *']", "UTC"), ("0 6 * * *", "Mars/Olympus"), ("0 6 * * *", "")],
)
def test_an_unreadable_cron_or_timezone_is_none_not_a_guess(cron: str, timezone: str) -> None:
    assert latest_due(cron, timezone, _utc(2026, 11, 16, 12, 0)) is None
