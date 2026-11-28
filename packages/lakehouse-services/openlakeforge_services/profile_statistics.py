"""Table and column statistics OpenMetadata's profiler stored, with the age that says whether to trust them.

Statistics are not fields of the table entity in 1.13.6: `GET /tables/name/<fqn>?fields=profile` returns none. They are
time series read from `/tables/<table fqn>/tableProfile` and `/tables/<column fqn>/columnProfile`. A reader gets back
only what the stored metric list allowed (a Confidential product has no min, max or mean). `stale` is set for an
absent or overdue table profile, or when any returned column row is overdue, so a caller never presents old numbers as
current. A column with no row at all carries no numbers (`profiled_at` None) and does not by itself make the table
stale.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from openlakeforge_core import profile_is_stale

from .openmetadata import OpenMetadataClient, fqn_part

# Wide enough that a profile older than the staleness threshold is still found, and reported as stale.
_LOOKBACK = timedelta(days=30)


@dataclass(frozen=True)
class ColumnStatistics:
    name: str
    values_count: float | None
    null_count: float | None
    null_proportion: float | None
    distinct_count: float | None
    # Present only where the product's classification stored them; text columns hold lengths, not values.
    minimum: Any = None
    maximum: Any = None
    mean: float | None = None
    # When this column's row was written: a column with no result in a later run keeps its older row.
    profiled_at: datetime | None = None


@dataclass(frozen=True)
class TableStatistics:
    table_fqn: str
    row_count: float | None
    profiled_at: datetime | None
    stale: bool
    columns: tuple[ColumnStatistics, ...]


def _at(timestamp_ms: float | None) -> datetime | None:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC) if timestamp_ms is not None else None


def _latest(client: OpenMetadataClient, fqn: str, kind: str, *, start: datetime, end: datetime) -> Mapping[str, Any]:
    response = client.request(
        "GET",
        f"/api/v1/tables/{urllib.parse.quote(fqn, safe='')}/{kind}",
        params={"startTs": int(start.timestamp() * 1000), "endTs": int(end.timestamp() * 1000), "limit": 100},
    )
    rows = [row for row in response.get("data") or () if isinstance(row, Mapping) and "timestamp" in row]
    return max(rows, key=lambda row: row["timestamp"], default={})


def read_table_statistics(
    client: OpenMetadataClient, table_fqn: str, columns: Iterable[str], *, now: datetime | None = None
) -> TableStatistics:
    """The latest table and column profiles of `table_fqn`; stale when absent or older than the refresh period.

    A requested column with no row in the lookback comes back with every statistic and `profiled_at` None: no number
    is shown for it, so none can be mistaken for current. It does not make the table stale, because the profiler
    skips column types it cannot compute (so a column can have no row however fresh the table is).
    """
    end = now or datetime.now(UTC)
    start = end - _LOOKBACK
    table = _latest(client, table_fqn, "tableProfile", start=start, end=end)
    timestamp = table.get("timestamp")
    stats = []
    for column in columns:
        row = _latest(client, f"{table_fqn}.{fqn_part(column)}", "columnProfile", start=start, end=end)
        stats.append(
            ColumnStatistics(
                name=column,
                values_count=row.get("valuesCount"),
                null_count=row.get("nullCount"),
                null_proportion=row.get("nullProportion"),
                distinct_count=row.get("distinctCount"),
                minimum=row.get("min"),
                maximum=row.get("max"),
                mean=row.get("mean"),
                profiled_at=_at(row.get("timestamp")),
            )
        )
    return TableStatistics(
        table_fqn=table_fqn,
        row_count=table.get("rowCount"),
        profiled_at=_at(timestamp),
        stale=profile_is_stale(timestamp, now=end)
        or any(
            c.profiled_at is not None and profile_is_stale(c.profiled_at.timestamp() * 1000, now=end) for c in stats
        ),
        columns=tuple(stats),
    )
