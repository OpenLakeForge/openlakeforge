"""`latest_due` is the only source of a schedule's expected run, so its edges are pinned here."""

from __future__ import annotations

from datetime import UTC, datetime

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
def test_the_latest_occurrence_is_at_or_before_now(
    cron: str, timezone: str, now: datetime, expected: datetime
) -> None:
    assert latest_due(cron, timezone, now) == expected


def test_a_wall_time_the_clock_skips_comes_due_when_the_gap_ends() -> None:
    # 02:30 does not exist in New York on 2026-03-08; the clock jumps from 02:00 EST to 03:00 EDT.
    assert latest_due("30 2 * * *", "America/New_York", _utc(2026, 3, 8, 12, 0)) == _utc(2026, 3, 8, 7, 0)
    assert latest_due("30 2 * * *", "America/New_York", _utc(2026, 3, 8, 6, 45)) == _utc(2026, 3, 7, 7, 30)


def test_a_wall_time_the_clock_repeats_comes_due_once_at_its_first_pass() -> None:
    # 01:30 happens at 05:30Z and again at 06:30Z on 2026-11-01; the second pass must not be a new due time.
    first = _utc(2026, 11, 1, 5, 30)
    assert latest_due("30 1 * * *", "America/New_York", _utc(2026, 11, 1, 5, 45)) == first
    assert latest_due("30 1 * * *", "America/New_York", _utc(2026, 11, 1, 6, 45)) == first
    assert latest_due("30 1 * * *", "America/New_York", _utc(2026, 11, 1, 8, 45)) == first


@pytest.mark.parametrize(
    ("cron", "timezone"),
    [("not a cron", "UTC"), ("['0 1 * * *', '0 2 * * *']", "UTC"), ("0 6 * * *", "Mars/Olympus"), ("0 6 * * *", "")],
)
def test_an_unreadable_cron_or_timezone_is_none_not_a_guess(cron: str, timezone: str) -> None:
    assert latest_due(cron, timezone, _utc(2026, 11, 16, 12, 0)) is None
