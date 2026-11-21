"""Read the pod log archive in both layouts (ADR 0022, decision 5).

v1, written by the polling archiver (`openlakeforge_runtime/k8s_log_archive.py`):
`<base>/k8s/namespace=<ns>/date=<YYYY-MM-DD>/hour=<HH>/k8s-logs-<stamp>.ndjson.gz`.
v2, written by the node collector:
`<base>/v2/namespace=<ns>/date=<YYYY-MM-DD>/hour=<HH>/<stage>-<uuid>.ndjson.gz`, the date and hour of every record in
the object (the collector tags each record with its own and keys the object by the tag). Objects written before hour
partitions sit directly under `date=`. Both are gzipped NDJSON. The caller passes an S3 client: this package holds no
object-store credential and does not depend on boto3 (ADR 0021, decision 4).
"""

from __future__ import annotations

import base64
import bisect
import gzip
import hashlib
import heapq
import io
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from typing import Any, Protocol
from urllib.parse import urlparse

# The writer's record schema; a record of another schema is skipped, not misread.
SCHEMA = "openlakeforge.k8s_log.v1"
SCHEMA_V2 = "openlakeforge.k8s_log.v2"

# Only a name Kubernetes could give a namespace becomes a path segment: no slash, `=` or dot.
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
# A v2 hour partition. Any other sub-prefix of a day, such as the one a collector upgraded with chunks of the date-only
# tag still on disk writes (`hour=<namespace>.<date>[2]/`), is read whole, as the objects directly under `date=` are.
_V2_HOUR = re.compile(r"hour=(\d{2})/")
# Neither layout keeps a severity field, so a line's level is read from its text, and only where the text states it
# in one of the shapes `level_of` recognises; any other line has no level rather than a guessed one.
SEVERITIES = ("debug", "info", "warning", "error", "critical")
_LEVEL_NAME = {"warn": "warning", "fatal": "critical"}
_WORD = r"(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)"
# A leading timestamp as loggers print it: ISO 8601, Python's `asctime`, log4j's `yy/MM/dd`, Fluent Bit's bracketed one.
_STAMP = r"\[?\d{2,4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|\s?[+-]\d{2}:?\d{2}|\s?UTC)?\]?\s*"
# Upper case only when bare, so a message that opens with the word "Error" in prose has no level; any case in brackets.
_LEADING = re.compile(rf"^(?:{_STAMP}(?:[-|]\s*)?)?(?:{_WORD}|\[\s*(?i:{_WORD})\s*\])(?=[\s:|\]-]|$)")
# Python logging's and Dagster's console format: `<asctime> - <logger> - <LEVEL> - <message>`.
_PYTHON = re.compile(rf"^{_STAMP}- \S+ - {_WORD} - ")
# One logfmt token: `key=value` with a bare or double-quoted value (backslash escapes), a quoted string, or a bare
# word. A quoted value is consumed whole, so a `level=` inside it is never read as the line's own key.
_LOGFMT_TOKEN = re.compile(
    r"\s*(?:([A-Za-z_][\w.]*)=(\"(?:[^\"\\]|\\.)*\"|[^\s\"]*)|\"(?:[^\"\\]|\\.)*\"|[^\s\"=]+)(?=\s|$)"
)
_LOGFMT_KEYS = ("level", "lvl")
_JSON_KEYS = ("level", "severity", "levelname", "log.level", "lvl")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FRACTION = re.compile(r"\.(\d+)")
# An object holds only lines written before it was uploaded, so one whose LastModified is before a window's start is
# not read. The line times come from the node's clock (v2's CRI `time`, v1's kubelet timestamp or archive time) and
# LastModified from the store's, truncated to the second; this much disagreement still reads the object. No upper
# bound is taken: during a store outage the collector spools for as long as `store_dir_limit_size` lasts, sized in
# hours, and uploads old files after new ones (ADR 0022, decisions 2 and 5), so a late object can hold early lines.
UPLOAD_CLOCK_SLACK = timedelta(minutes=15)

# How many objects one read fetches at once. An hour after a store outage or a spool flush can hold hundreds of
# objects, and one GET at a time spent the platform API's whole 20 s request deadline on round trips. It also bounds
# the fetched bodies a read holds before decoding them, and stays below botocore's default of ten pooled connections.
FETCH_AHEAD = 8
# One pool for the process, not one per read: the platform API already runs reads on small pools of its own, and a
# pool per read would multiply the GETs in flight by theirs. A fetch waits on nothing but the store, so a read
# waiting on one cannot deadlock the pool, however the callers' pools nest. Threads start on first use, not at import.
_FETCHES = ThreadPoolExecutor(max_workers=FETCH_AHEAD, thread_name_prefix="log-archive-get")

log = logging.getLogger(__name__)

# Event time in nanoseconds, layout (0 is v2, 1 is v1), archive day, object key digest, record index in it.
Position = tuple[int, int, str, str, int]


class ObjectStore(Protocol):
    """The two calls of a boto3 S3 client the reader makes, with the arguments it passes."""

    def list_objects_v2(  # noqa: N803 - boto3's argument names
        self, *, Bucket: str, Prefix: str = ..., Delimiter: str = ..., ContinuationToken: str = ...
    ) -> Mapping[str, Any]: ...

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...  # noqa: N803


@dataclass(frozen=True)
class ArchivedLogLine:
    archived_at: str
    namespace: str
    pod: str
    container: str
    # The log line with its Kubernetes timestamp, or why the archiver could not read the container.
    line: str | None = None
    error: str | None = None
    # Set for v2 records (the v1 archiver never wrote them), `source` for both.
    stage: str | None = None
    component: str | None = None
    domain: str | None = None
    product: str | None = None
    run_id: str | None = None
    stream: str | None = None
    missing: tuple[str, ...] | None = None
    source: str | None = None
    # When the line was written, as the source recorded it: v2's CRI `time`, or the kubelet timestamp a v1 line
    # starts with. None when there is none, as for a v1 archive error; the archive time never stands in for it.
    timestamp: str | None = None
    # One of SEVERITIES when the message states it (`level_of`), else None.
    level: str | None = None


def _level_name(word: object) -> str | None:
    if not isinstance(word, str):
        return None
    name = _LEVEL_NAME.get(word.lower(), word.lower())
    return name if name in SEVERITIES else None


def level_of(message: str | None) -> str | None:
    """The level the message states, one of SEVERITIES, or None.

    Recognised, in this order: the `level`, `severity`, `levelname`, `log.level` or `lvl` string of a message that is
    a JSON object; a level word opening the message, after an optional timestamp, bare in upper case or bracketed in
    any case (`ERROR ...`, `2026-11-16 09:41:07 WARNING ...`, `[ info] ...`, `ERROR:root:...`); Python logging's
    `<time> - <logger> - <LEVEL> - `; a top-level `level=` or `lvl=` of a line that is logfmt whole. `warn` reads
    as `warning`, `fatal` as `critical`; numeric levels and other words are no level.
    """
    text = (message or "").strip()
    if text.startswith("{"):
        try:
            record = json.loads(text)
        except ValueError:
            record = None
        if isinstance(record, dict):
            return next((name for key in _JSON_KEYS if (name := _level_name(record.get(key)))), None)
    match = _LEADING.match(text) or _PYTHON.match(text)
    if match:
        return _level_name(next(group for group in match.groups() if group))
    return _logfmt_level(text)


def _logfmt_level(text: str) -> str | None:
    """The first top-level `level=` or `lvl=` of a line that tokenises as logfmt whole; None for any other line.

    A line with an unbalanced quote, or a token logfmt cannot hold, is not logfmt, so no key in it is trusted.
    """
    position, value = 0, None
    while position < len(text):
        token = _LOGFMT_TOKEN.match(text, position)
        if token is None or token.end() == position:
            return None
        if value is None and token.group(1) in _LOGFMT_KEYS:
            value = token.group(2).removeprefix('"').removesuffix('"')
        position = token.end()
    return _level_name(value)


def message_of(line: ArchivedLogLine) -> str:
    """The text a search reads: the message, without the kubelet timestamp a v1 line opens with.

    So a v1 copy of a v2 line matches exactly when the v2 record does, and a search for a date matches no line
    by its timestamp. An archive error record has no message.
    """
    text = line.line or ""
    if line.source == "v1" and line.timestamp is not None:
        return text.removeprefix(line.timestamp).removeprefix(" ")
    return text


def _rank(level: str | None) -> int:
    return SEVERITIES.index(level) if level else -1


def _instant(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _nanoseconds(stamp: str, at: datetime) -> int:
    """`at` is parsed to the microsecond; the digits below it are read back from the stamp."""
    seconds = (at.replace(microsecond=0) - _EPOCH) // timedelta(seconds=1)
    fraction = _FRACTION.search(stamp)
    return seconds * 1_000_000_000 + (int(fraction.group(1)[:9].ljust(9, "0")) if fraction else 0)


def _event_nanoseconds(stamp: str, fallback: str) -> int:
    """Event time for either layout, so one event sorts the same wherever it was archived.

    `fallback` is the archive time, used only when `stamp` is no timestamp, as for a v1 archive error.
    """
    at = _instant(stamp)
    if at is None:
        at, stamp = _instant(fallback), fallback
    return _nanoseconds(stamp, at) if at else -1


class _Best:
    """The `limit` earliest candidates seen, and the dedup keys of only those.

    A replayed record sorts exactly where its first copy did, so once the first copy is outside the best
    `limit` the replay is too and needs no memory. State is O(limit), not O(records in the day). A record at or
    before `after` was answered on an earlier page: it is skipped, and a replay of one at the cursor's own time is
    remembered so it is skipped too (the scan meets positions in order, so the first copy always comes first).
    """

    def __init__(self, limit: int, after: Position | None = None) -> None:
        self._limit = limit
        self._after = after
        self._items: list[tuple[Position, str | None, ArchivedLogLine]] = []
        self._dedup: set[str] = set()
        self._answered: set[str] = set()
        # Answered lines at the cursor's own time. A v1 copy of one sorts after its v2 record at the same time, so
        # it can fall past the cursor while the v2 record does not; these keep it out (ADR 0022, decision 5).
        self.at_cursor: list[ArchivedLogLine] = []

    def offer(self, position: Position, dedup: str | None, line: ArchivedLogLine) -> None:
        if dedup is not None and (dedup in self._dedup or dedup in self._answered):
            return
        if self._after is not None and position <= self._after:
            if position[0] == self._after[0]:
                self.at_cursor.append(line)
                if dedup is not None:
                    self._answered.add(dedup)
            return
        if len(self._items) >= self._limit:
            if position >= self._items[-1][0]:
                return
            self._dedup.discard(self._items.pop()[1] or "")
        bisect.insort(self._items, (position, dedup, line), key=lambda item: item[0])
        if dedup is not None:
            self._dedup.add(dedup)

    def sorted(self) -> list[tuple[Position, ArchivedLogLine]]:
        return [(position, line) for position, _, line in self._items]


def _digest(key: str) -> str:
    # Orders ties by object without handing the key to a client inside a cursor: it names the bucket path.
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def encode_position(position: Position) -> str:
    """An opaque continuation cursor for `read_days(after=...)`."""
    return base64.urlsafe_b64encode(json.dumps(list(position), separators=(",", ":")).encode()).decode().rstrip("=")


def decode_position(cursor: str) -> Position:
    """The position `encode_position` wrote; ValueError for anything else."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, TypeError) as exc:
        raise ValueError("not a log cursor") from exc
    shape = (int, int, str, str, int)
    if not (isinstance(raw, list) and len(raw) == 5 and all(type(v) is t for v, t in zip(raw, shape, strict=True))):
        raise ValueError("not a log cursor")
    return (raw[0], raw[1], raw[2], raw[3], raw[4])


def position_before(at: datetime) -> Position:
    """A cursor just before every line written at or after `at`, as for a window cut at a day boundary."""
    at = at.astimezone(UTC)
    # Layout -1 sorts before both real layouts at the same nanosecond.
    return (_nanoseconds(at.isoformat(), at), -1, "", "", 0)


@dataclass(frozen=True)
class _Placement:
    """The fields of a record that its selection reads before its level: who wrote it, for which run, and when."""

    pod: str
    container: str
    component: str | None
    product: str | None
    run_id: str | None
    at: datetime | None


@dataclass(frozen=True)
class _Selection:
    placed: Callable[[_Placement], bool]
    wanted: Callable[[ArchivedLogLine], bool]


@dataclass
class _Scan:
    """One read's object bound and its counts; kept per read, so an archive shared by two reads cannot mix them."""

    not_before: datetime | None
    listed: int = 0
    opened: int = 0


class ArchiveDeadline(Exception):
    """The read passed its deadline and stopped; what it had found is not answered, as it may not be the earliest."""


class ArchiveCancelled(Exception):
    """The caller cancelled the read, as when the client that asked for it went away; nothing is answered."""


@dataclass(frozen=True)
class ArchiveRead:
    lines: list[ArchivedLogLine]
    # The position of the last line, to continue past it with `after`; None when no line was found.
    last: Position | None


class LogArchive:
    def __init__(self, store: ObjectStore, base_uri: str, clock: Callable[[], float] = time.monotonic) -> None:
        parsed = urlparse(base_uri)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError(f"expected an s3:// log base URI, got {base_uri!r}")
        self._store = store
        self._bucket = parsed.netloc
        self._prefix = parsed.path.strip("/")
        self._clock = clock
        self._deadline: float | None = None
        self._cancelled: Callable[[], bool] | None = None

    def read(
        self,
        namespace: str,
        day: date,
        *,
        hour: int | None = None,
        pod: str = "",
        container: str = "",
        component: str = "",
        product: str = "",
        run_id: str = "",
        severity: str = "",
        text: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[ArchivedLogLine]:
        """Lines of one namespace for a UTC day, or one hour of it, oldest first, at most `limit`.

        Every filter selects within the one namespace's partitions; none of them is a path segment, so a
        filter cannot move the listing to another stage. `severity` is a floor (`warning` also returns
        errors) on the line's `level`, so a line with no level is left out by it; `text` keeps the lines whose
        message holds it, ignoring case (`message_of`); `since` and `until` bound the record time. `component`,
        `product` and `run_id` exist only in v2, so setting one leaves v1 out.
        `deadline`, on the archive's clock, stops the scan with ArchiveDeadline between pages, objects and records;
        `cancelled` returning true stops it there with ArchiveCancelled, and no queued fetch of the read starts.
        With `since`, an object uploaded before it (less UPLOAD_CLOCK_SLACK) is not read: it holds no line it selects.
        `hour`, `since` and `until` also pick the v2 hour partitions listed.
        """
        return self.read_days(
            namespace,
            (day,),
            hour=hour,
            pod=pod,
            container=container,
            component=component,
            product=product,
            run_id=run_id,
            severity=severity,
            text=text,
            since=since,
            until=until,
            limit=limit,
            deadline=deadline,
            cancelled=cancelled,
        ).lines

    def read_days(
        self,
        namespace: str,
        days: Sequence[date],
        *,
        hour: int | None = None,
        pod: str = "",
        container: str = "",
        component: str = "",
        product: str = "",
        run_id: str = "",
        severity: str = "",
        text: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
        after: Position | None = None,
        v1_lookback: timedelta | None = None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ArchiveRead:
        """`read` over several UTC days at once: one ordering and one dedup across them, at most `limit` in all.

        A window that crosses midnight, such as a run's, lists each day's partitions; the caller bounds `days`.
        Lines sort by event time, then layout (v2 first), day, object and record, so `after` (the `last` of an
        earlier read with the same filters) continues exactly past it, ties included. A v1 object is partitioned
        by when it was archived, up to `v1_lookback` after its lines were written: with it, v1 is also listed on
        the days that reach past the window's end (`until`, or the end of the last day). The event-time filters
        still apply, so those days add no line outside the window. With `since`, an object uploaded more than
        UPLOAD_CLOCK_SLACK before it is skipped unread, from the LastModified the listing returns.

        v2 lists only the hour partitions of each day that `hour`, `since` and `until` touch, and no day the window
        misses. A v2 object holds the records of the one hour its key names, whenever it was uploaded, so this needs
        no slack hour; objects of a day without hour partitions are listed as before, with the LastModified skip.

        `text` is a filter like the others: it applies before the ordering and the limit, so `last` is the position
        of the last matching line and `after` pages through the matches alone, none skipped or repeated.
        """
        # A slash would move the listing to another partition, such as another stage's namespace.
        if not _NAMESPACE.fullmatch(namespace):
            raise ValueError(f"not a namespace: {namespace!r}")
        if hour is not None and not 0 <= hour <= 23:
            raise ValueError(f"not an hour of the day: {hour}")
        if severity and severity not in SEVERITIES:
            raise ValueError(f"not a severity: {severity!r}")
        floor = SEVERITIES.index(severity) if severity else -1
        needle = text.casefold()
        since, until = (
            None if bound is None else bound if bound.tzinfo else bound.replace(tzinfo=UTC) for bound in (since, until)
        )

        # The filters a record's own fields decide, applied before its level is read from its text: in an hour that a
        # burst of another workload's logging filled, nearly every record fails one of them, and reading the level of
        # each was most of the time a run window's read took.
        def placed(record: _Placement) -> bool:
            return (
                (not pod or record.pod == pod)
                and (not container or record.container == container)
                and (not component or record.component == component)
                and (not product or record.product == product)
                and (not run_id or record.run_id == run_id)
                and (since is None or (record.at is not None and record.at >= since))
                and (until is None or (record.at is not None and record.at < until))
            )

        def wanted(line: ArchivedLogLine) -> bool:
            return (floor < 0 or _rank(line.level) >= floor) and (not needle or needle in message_of(line).casefold())

        selects = _Selection(placed, wanted)

        event_days = sorted(set(days))
        v1_days = event_days
        if event_days and v1_lookback is not None and hour is None:
            end = until or datetime.combine(event_days[-1] + timedelta(days=1), datetime.min.time(), UTC)
            last = max((end + v1_lookback - timedelta(microseconds=1)).date(), event_days[-1])
            v1_days = sorted(
                set(event_days) | {event_days[-1] + timedelta(days=n) for n in range((last - event_days[-1]).days + 1)}
            )

        self._deadline = deadline
        self._cancelled = cancelled
        # Every line `wanted` keeps is at or after `since`, so a skipped object held none of them: the ordering, the
        # dedup and a cursor's page are those of a read of every object.
        scan = _Scan(None if since is None else since - UPLOAD_CLOCK_SLACK)
        try:
            v2 = _Best(limit, after)
            for day in event_days:
                hours = _v2_hours(day, hour, since, until)
                if hours is None or hours:
                    self._v2(namespace, day, hour, hours, selects, v2, scan)
            retained = v2.sorted()
            # ADR 0022, decision 5: a v1 line is dropped only when v2 holds the same line of the same container.
            overlap = {
                (line.pod, line.container, line.archived_at, line.line)
                for line in chain((line for _, line in retained), v2.at_cursor)
            }
            v1 = _Best(limit, after)
            if not (component or product or run_id):
                for day in v1_days:
                    self._v1(namespace, day, hour, selects, overlap, v1, scan)
        finally:
            log.info(
                "log archive read of %s over %d day(s): %d objects listed, %d read",
                namespace,
                len(event_days),
                scan.listed,
                scan.opened,
            )
        merged = heapq.nsmallest(limit, chain(retained, v1.sorted()), key=lambda item: item[0])
        return ArchiveRead(lines=[line for _, line in merged], last=merged[-1][0] if merged else None)

    def _check(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise ArchiveCancelled("the log archive read was cancelled")
        if self._deadline is not None and self._clock() >= self._deadline:
            raise ArchiveDeadline("the log archive read passed its deadline")

    def _v2(
        self,
        namespace: str,
        day: date,
        hour: int | None,
        hours: frozenset[int] | None,
        selects: _Selection,
        best: _Best,
        scan: _Scan,
    ) -> None:
        keys = self._v2_keys(self._partition("v2", namespace, day, None), hours, scan)
        with closing(self._bodies(keys, scan)) as bodies:
            for digest, body in bodies:
                for index, record in enumerate(self._records(body)):
                    if record.get("schema") != SCHEMA_V2 or record["namespace"] != namespace:
                        continue
                    stamp = record["time"]
                    if hour is not None and stamp[11:13] != f"{hour:02d}":
                        continue
                    pod, container, message = record["pod"], record["container"], record["message"]
                    at = _instant(stamp)
                    placement = _Placement(
                        pod, container, record.get("component"), record.get("product"), record.get("run_id"), at
                    )
                    if not selects.placed(placement):
                        continue
                    missing = record.get("missing")
                    line = ArchivedLogLine(
                        archived_at=stamp,
                        namespace=namespace,
                        pod=pod,
                        container=container,
                        line=message,
                        stage=record.get("stage"),
                        component=placement.component,
                        domain=record.get("domain"),
                        product=placement.product,
                        run_id=placement.run_id,
                        stream=record.get("stream"),
                        missing=tuple(missing) if isinstance(missing, list) else (),
                        source="v2",
                        timestamp=stamp,
                        level=level_of(message),
                    )
                    if selects.wanted(line):
                        # A replay after a collector restart writes the same record again.
                        position = (_event_nanoseconds(stamp, stamp), 0, day.isoformat(), digest, index)
                        best.offer(position, record.get("dedup_key"), line)

    def _v1(
        self,
        namespace: str,
        day: date,
        hour: int | None,
        selects: _Selection,
        overlap: set[tuple[str, str, str, str | None]],
        best: _Best,
        scan: _Scan,
    ) -> None:
        keys = self._ordered(self._partition("k8s", namespace, day, hour), scan)
        with closing(self._bodies(keys, scan)) as bodies:
            for digest, body in bodies:
                for index, record in enumerate(self._records(body)):
                    # A run with no pods leaves one marker record naming no pod; it is not a log line.
                    if record.get("schema") != SCHEMA or "pod" not in record:
                        continue
                    text = record.get("line")
                    # The line starts with the kubelet's timestamp, which is when it was written; the
                    # archive time is only when it was copied.
                    stamp, _, rest = (text or "").partition(" ")
                    written = _instant(stamp)
                    archived_at, written_in = record["archived_at"], record["namespace"]
                    pod, container = record["pod"], record["container"]
                    at = written or _instant(archived_at)
                    if not selects.placed(_Placement(pod, container, None, None, None, at)):
                        continue
                    if (pod, container, stamp, rest) in overlap:
                        continue
                    line = ArchivedLogLine(
                        archived_at=archived_at,
                        namespace=written_in,
                        pod=pod,
                        container=container,
                        line=text,
                        error=record.get("archive_error"),
                        source="v1",
                        timestamp=stamp if written else None,
                        level=level_of(rest if written else text),
                    )
                    if selects.wanted(line):
                        position = (_event_nanoseconds(stamp, line.archived_at), 1, day.isoformat(), digest, index)
                        best.offer(position, None, line)

    def _partition(self, layout: str, namespace: str, day: date, hour: int | None) -> str:
        parts = [self._prefix, layout, f"namespace={namespace}", f"date={day.isoformat()}"]
        if hour is not None:
            parts.append(f"hour={hour:02d}")
        return "/".join(part for part in parts if part) + "/"

    def _ordered(self, prefix: str, scan: _Scan) -> list[tuple[str, str]]:
        # Scanned in the order positions sort in, so the first copy of a replayed record is always met first.
        return sorted((_digest(key), key) for key in self._keys(prefix, scan))

    def _v2_keys(self, day_prefix: str, hours: frozenset[int] | None, scan: _Scan) -> list[tuple[str, str]]:
        """The day's objects in `hours` (every hour when None), those directly under it, and any other sub-prefix's.

        One listing with a delimiter finds the day's hour partitions without listing their objects. The keys of the
        whole day are ordered together, as positions sort by day and then object.
        """
        subprefixes: list[str] = []
        keys = list(self._keys(day_prefix, scan, subprefixes))
        for prefix in subprefixes:
            partition = _V2_HOUR.fullmatch(prefix.removeprefix(day_prefix))
            if hours is None or partition is None or int(partition.group(1)) in hours:
                keys.extend(self._keys(prefix, scan))
        return sorted((_digest(key), key) for key in keys)

    def _keys(self, prefix: str, scan: _Scan, subprefixes: list[str] | None = None) -> Iterator[str]:
        """Objects under `prefix`; with `subprefixes`, only those directly under it, and the sub-prefixes go there."""
        token = ""
        while True:
            self._check()
            args: dict[str, str] = {"Bucket": self._bucket, "Prefix": prefix}
            if subprefixes is not None:
                args["Delimiter"] = "/"
            if token:
                args["ContinuationToken"] = token
            page = self._store.list_objects_v2(**args)
            if subprefixes is not None:
                subprefixes.extend(item["Prefix"] for item in page.get("CommonPrefixes") or [])
            for item in page.get("Contents", []):
                if not item["Key"].endswith(".ndjson.gz"):
                    continue
                scan.listed += 1
                # A listing without a datetime LastModified cannot rule the object out, so it is read.
                modified = item.get("LastModified")
                if scan.not_before is not None and isinstance(modified, datetime):
                    if (modified if modified.tzinfo else modified.replace(tzinfo=UTC)) < scan.not_before:
                        continue
                yield item["Key"]
            token = page.get("NextContinuationToken") or ""
            if not page.get("IsTruncated") or not token:
                return

    def _bodies(self, keys: list[tuple[str, str]], scan: _Scan) -> Generator[tuple[str, bytes]]:
        """Each object's digest and gzipped body, in the order of `keys`, fetched up to FETCH_AHEAD at once.

        Only the fetch runs on the pool; decoding and every offer stay on the reading thread in key order, so the
        result, ties and dedup are those of a read one object at a time. No object holds a position known before it
        is read, so every listed object is fetched: the limit cannot end the scan early. A fetch checks the deadline
        before its GET, so none starts after it, and one that fails raises its own exception here, in key order. It
        checks the read's cancellation too, so a cancelled read's queued fetches that `cancel` could not stop in time
        start no GET either.
        """
        deadline, cancelled, clock = self._deadline, self._cancelled, self._clock

        def fetch(key: str) -> bytes:
            if cancelled is not None and cancelled():
                raise ArchiveCancelled("the log archive read was cancelled")
            if deadline is not None and clock() >= deadline:
                raise ArchiveDeadline("the log archive read passed its deadline")
            body: bytes = self._store.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            return body

        upcoming = iter(keys)
        pending: deque[tuple[str, Future[bytes]]] = deque()
        try:
            while True:
                while len(pending) < FETCH_AHEAD and (item := next(upcoming, None)) is not None:
                    self._check()
                    scan.opened += 1
                    pending.append((item[0], _FETCHES.submit(fetch, item[1])))
                if not pending:
                    return
                digest, fetched = pending.popleft()
                yield digest, fetched.result()
        finally:
            # A read that stopped, on its deadline or an error, starts none of the fetches it had queued.
            for _, fetched in pending:
                if fetched.cancel():
                    scan.opened -= 1

    def _records(self, body: bytes) -> Iterator[dict[str, Any]]:
        # Decompressed a line at a time: an object holds up to the collector's 8 MB `total_file_size` of NDJSON, and
        # holding it whole, as bytes, then text, then lines, took over 20 MB per read, enough for two reads at once to
        # pass the platform API's 192 MiB memory limit. Only `\n` ends a record, as NDJSON defines it.
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            for raw in stream:
                self._check()
                if not raw.strip():
                    continue
                record = json.loads(raw.decode("utf-8"))
                if isinstance(record, dict):
                    yield record


def _v2_hours(day: date, hour: int | None, since: datetime | None, until: datetime | None) -> frozenset[int] | None:
    """The hours of `day` that `hour` and `[since, until)` touch: None for all 24, empty when the window misses it."""
    start = datetime.combine(day, datetime.min.time(), UTC)
    first, last = start, start + timedelta(days=1)
    if since is not None:
        first = max(first, since.astimezone(UTC))
    if until is not None:
        last = min(last, until.astimezone(UTC))
    if first >= last:
        return frozenset()
    touched = range(first.hour, (last - timedelta(microseconds=1)).hour + 1)
    if hour is not None:
        return frozenset({hour} & set(touched))
    if len(touched) == 24:
        return None
    return frozenset(touched)
