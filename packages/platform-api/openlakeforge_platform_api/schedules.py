"""When a cron schedule last came due, computed from the cron and its timezone alone.

Dagster's ticks cannot say this: a stopped scheduler daemon emits no tick, so a tick-derived due time would stay on the
last day it ran and the missed run would never show.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

# Dagster applies its daylight-saving rules only to wall times in these hours (dagster/_utils/schedules.py).
_DST_HOURS = frozenset({1, 2, 3})
# A daylight-saving shift is at most an hour, so wall times this far before `now` cannot be due after a later one.
_LOOKBACK = timedelta(hours=2)


def _exists(wall: datetime) -> bool:
    return wall.astimezone(UTC).astimezone(wall.tzinfo).replace(tzinfo=None) == wall.replace(tzinfo=None)


def _ambiguous(wall: datetime) -> bool:
    return wall.replace(fold=0).utcoffset() != wall.replace(fold=1).utcoffset()


def _fire_times(wall: datetime, hourly: bool) -> list[datetime]:
    """The instants Dagster fires for one wall time of the cron, mirroring dagster/_utils/schedules.py."""
    if not _exists(wall):
        if hourly or wall.hour not in _DST_HOURS:
            return [] if hourly else [wall]
        # The gap's end: Dagster's `apply_post_transition` moves to the hour start plus the shift.
        return [wall.replace(minute=0, second=0, microsecond=0)]
    if _ambiguous(wall):
        # `_apply_fold` takes the later pass; an hourly cron keeps firing, so both passes are due.
        return [wall.replace(fold=0), wall.replace(fold=1)] if hourly else [wall.replace(fold=1)]
    return [wall]


def latest_due(cron: str, timezone: str, now: datetime) -> datetime | None:
    """The latest time Dagster fires the schedule at or before `now`, in UTC; None when the cron or timezone is bad.

    Dagster evaluates the cron in the schedule's execution timezone, and this mirrors how it picks fire times
    (`dagster/_utils/schedules.py`). A wall time the clock skips (spring forward) comes due when the gap ends. One it
    repeats (fall back) comes due at its later pass, `fold=1` (`_apply_fold`), so while that pass is still ahead of
    `now` the latest due time is the previous occurrence. A cron that fires every hour fires at every pass instead.
    """
    try:
        zone = ZoneInfo(timezone)
        local_now = now.astimezone(zone).replace(tzinfo=None)
        parts = croniter(cron).expanded
        hourly = len(parts[1]) == 1 and parts[1][0] == "*"
        # Like Dagster, walk the cron in naive wall time and start an hour late so a repeated hour is whole.
        start = local_now.replace(second=0, microsecond=0) + timedelta(hours=1, minutes=1)
        walker = croniter(cron, start)
        best: datetime | None = None
        while True:
            wall: datetime = walker.get_prev(datetime)
            for fired in _fire_times(wall.replace(tzinfo=zone), hourly):
                instant = fired.astimezone(UTC)
                if instant <= now and (best is None or instant > best):
                    best = instant
            if best is not None and wall < local_now - _LOOKBACK:
                return best
    except (ValueError, KeyError, OSError, IndexError):
        return None


def next_due(cron: str, timezone: str, after: datetime) -> datetime | None:
    """The first time Dagster fires the schedule strictly after `after`, in UTC; None when the cron or timezone is bad.

    The mirror of `latest_due`, with the same wall-time walk and daylight-saving rules, so the two agree on which
    instants are fire times.
    """
    try:
        zone = ZoneInfo(timezone)
        local_after = after.astimezone(zone).replace(tzinfo=None)
        parts = croniter(cron).expanded
        hourly = len(parts[1]) == 1 and parts[1][0] == "*"
        # Start an hour early so a repeated hour is whole, as `latest_due` starts an hour late.
        start = local_after.replace(second=0, microsecond=0) - timedelta(hours=1)
        walker = croniter(cron, start)
        best: datetime | None = None
        while True:
            wall: datetime = walker.get_next(datetime)
            for fired in _fire_times(wall.replace(tzinfo=zone), hourly):
                instant = fired.astimezone(UTC)
                if instant > after and (best is None or instant < best):
                    best = instant
            if best is not None and wall > local_after + _LOOKBACK:
                return best
    except (ValueError, KeyError, OSError, IndexError):
        return None
