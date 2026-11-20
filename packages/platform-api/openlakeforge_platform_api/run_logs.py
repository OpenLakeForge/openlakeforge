"""The archived pod log lines of one run's window (#1326): its start to its end, across UTC days.

The log read of `reads.list_logs` covers one UTC day, so a run that crosses midnight showed only its first. Here the
window comes from the run itself, as `get_run` reads it, and every archive day it touches is read in one ordering. The
window is the run's, not its pods': lines another workload of the stage wrote meanwhile are in it too, unless
`match_run` keeps only the records the collector tagged with this run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Query, Request
from openlakeforge_core.log_archive import V1_LOOKBACK_SECONDS
from openlakeforge_services.dagster import DagsterRun
from openlakeforge_services.logs import ArchiveDeadline, decode_position, encode_position, position_before

from openlakeforge_platform_api.models import RunLogs
from openlakeforge_platform_api.reads import (
    MAX_LOG_LINES,
    _archive_errors,
    _dagster,
    _dagster_errors,
    _log_archive,
    log_line,
)

# A run of up to two whole days spans three UTC days. Each day lists the v2 hour partitions the window touches (and a
# day's objects from before hour partitions, those uploaded since the window's start less `UPLOAD_CLOCK_SLACK`), so
# this caps the read at three times a `list_logs` day; a longer window is answered in pages by `next`. The cap
# counts days of event time. v1 objects are partitioned by archive time, up to V1_LOOKBACK_SECONDS after their lines,
# so v1 alone may list one day more, past the cut, for the lines of its last hour.
MAX_RUN_LOG_DAYS = 3
V1_LOOKBACK = timedelta(seconds=V1_LOOKBACK_SECONDS)
# The run read and the archive read together, as in run_failure: each call has its own timeout, but a slow store
# reading three days would otherwise hold the request for their sum. The archive read also checks it itself between
# pages, objects and records, so a read abandoned here stops soon after instead of holding a worker.
READ_DEADLINE_SECONDS = 20.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# A read abandoned at the deadline keeps its thread until it next checks the deadline or a call's own timeout ends
# it; a small pool caps how many.
_READS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="run-logs-read")


class _Deadline(Exception):
    pass


def _within[T](expires: float, read: Callable[[], T]) -> T:
    def run() -> T:
        if time.monotonic() >= expires:
            raise _Deadline
        return read()

    answer = _READS.submit(run)
    try:
        return answer.result(timeout=max(expires - time.monotonic(), 0))
    except FutureTimeout as exc:
        answer.cancel()
        raise _Deadline from exc


def _utc(at: datetime) -> datetime:
    return at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)


def run_window(run: DagsterRun, now: datetime) -> tuple[datetime, datetime]:
    """The run's start (its creation, before it has started) to its end, or `now` while it has none."""
    start = run.started_at if run.started_at is not None else run.created_at
    end = datetime.fromtimestamp(run.ended_at, UTC) if run.ended_at is not None else now
    return datetime.fromtimestamp(start, UTC), end


def window_days(since: datetime, until: datetime) -> tuple[list[date], datetime]:
    """The UTC days `[since, until)` touches, at most MAX_RUN_LOG_DAYS, and `until` cut to the last of them."""
    first = since.date()
    last = (until - timedelta(microseconds=1)).date()
    if (last - first).days >= MAX_RUN_LOG_DAYS:
        last = first + timedelta(days=MAX_RUN_LOG_DAYS - 1)
        until = datetime.combine(last + timedelta(days=1), datetime.min.time(), UTC)
    return [first + timedelta(days=n) for n in range((last - first).days + 1)], until


def list_run_logs(
    request: Request,
    stage: str,
    run_id: str,
    pod: str = "",
    container: str = "",
    component: str = "",
    severity: Literal["debug", "info", "warning", "error", "critical"] | None = None,
    match_run: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
    after: str | None = Query(  # noqa: B008 - FastAPI's parameter API
        default=None, description="The `next` of the previous page, with the same filters: continues exactly past it."
    ),
    limit: int = Query(default=500, ge=1, le=MAX_LOG_LINES),  # noqa: B008
) -> RunLogs:
    """The stage's archived pod log lines from the run's start to its end, oldest first, across UTC days.

    `since` and `until` narrow the run's window, never widen it. `match_run` keeps only v2 records tagged with the
    run's id, which leaves v1 out. The other filters are `list_logs`'s. A truncated answer carries `next`; pass it
    back as `after` to read on, past lines that share one timestamp included.
    """
    try:
        cursor = decode_position(after) if after else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="`after` is not a cursor this route returned") from exc
    reader, namespace = _log_archive(request, stage)
    client = _dagster(request, stage)
    expires = time.monotonic() + READ_DEADLINE_SECONDS
    with _dagster_errors(stage):
        try:
            run = _within(expires, lambda: client.get_run(run_id))
        except _Deadline as exc:
            raise HTTPException(
                status_code=502, detail=f"Dagster for stage {stage!r} did not answer in {READ_DEADLINE_SECONDS:g} s"
            ) from exc
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} in stage {stage!r}")

    start, end = run_window(run, datetime.now(UTC))
    if since is not None:
        start = max(start, _utc(since))
    if until is not None:
        end = min(end, _utc(until))
    if cursor is not None:
        # The cursor's own microsecond: lines later in it are kept by the position, not by `since`.
        start = max(start, _EPOCH + timedelta(microseconds=cursor[0] // 1000))
    if start >= end:
        return RunLogs(lines=[], truncated=False, since=start.isoformat(), until=end.isoformat(), next=None)
    days, cut = window_days(start, end)

    with _archive_errors(stage):
        try:
            found = _within(
                expires,
                lambda: reader.read_days(
                    namespace,
                    days,
                    pod=pod,
                    container=container,
                    component=component,
                    run_id=run.run_id if match_run else "",
                    severity=severity or "",
                    since=start,
                    until=cut,
                    limit=limit,
                    after=cursor,
                    v1_lookback=V1_LOOKBACK,
                    deadline=expires,
                ),
            )
        except (_Deadline, ArchiveDeadline) as exc:
            raise HTTPException(
                status_code=502, detail=f"stage {stage!r}'s log archive did not answer in {READ_DEADLINE_SECONDS:g} s"
            ) from exc
    if len(found.lines) >= limit and found.last is not None:
        following: str | None = encode_position(found.last)
    elif cut < end:
        following = encode_position(position_before(cut))
    else:
        following = None
    return RunLogs(
        lines=[log_line(line) for line in found.lines],
        truncated=following is not None,
        since=start.isoformat(),
        until=cut.isoformat(),
        next=following,
    )
