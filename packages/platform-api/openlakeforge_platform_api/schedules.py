"""When a cron schedule last came due, computed from the cron and its timezone alone.

Dagster's ticks cannot say this: a stopped scheduler daemon emits no tick, so a tick-derived due time would stay on the
last day it ran and the missed run would never show.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter


def latest_due(cron: str, timezone: str, now: datetime) -> datetime | None:
    """The latest occurrence at or before `now`, in UTC; None when the cron or timezone cannot be read.

    Dagster evaluates the cron in the schedule's execution timezone. A wall time the clock skips (spring forward)
    comes due when the gap ends, and one it repeats (fall back) comes due once, at its first pass, because the
    scheduler runs it once.
    """
    try:
        zone = ZoneInfo(timezone)
        # croniter's previous occurrence is strictly before its argument, and cron works in whole minutes.
        after = now.astimezone(zone).replace(microsecond=0) + timedelta(seconds=1)
        found: datetime = croniter(cron, after).get_prev(datetime)
    except (ValueError, KeyError, OSError):
        return None
    first_pass = found.replace(fold=0)
    if first_pass.utcoffset() != found.utcoffset():
        found = first_pass
    return found.astimezone(UTC)
