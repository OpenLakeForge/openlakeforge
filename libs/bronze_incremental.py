"""Building blocks for incremental dlt connectors (#342).

A connector reads its source, and `libs.bronze_dlt.load_dlt_source` lands
what it reads in Bronze as `source.yaml` declares. These helpers cover the two
read patterns most sources fit, so a connector keeps only what is specific to
its source: the request or query, and the row shape.

- `newest_first_pages`: an API that can sort by modification time but not
  filter on it. Read newest first, stop at the cursor.
- `keyset_batches`: a SQL table with a monotonically increasing key. Read past
  the cursor in key order, a bounded number of rows a run.
- `sql_engine`: a SQLAlchemy engine from a source's stage bindings.

Each takes the resource's `dlt.sources.incremental` argument as `cursor`; the
connector still declares it, on the column `source.yaml` names, because
`load_dlt_source` checks the two match.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any


class CursorNotReached(RuntimeError):
    """The page budget ran out before the source reached the cursor."""


def _has_cursor(cursor: Any) -> bool:
    # dlt starts a resource that has never loaded, or whose state a reload
    # dropped, from its declared initial value.
    return cursor.start_value != cursor.initial_value


def newest_first_pages(
    fetch_page: Callable[[int], Sequence[Mapping[str, Any]]],
    cursor: Any,
    *,
    cursor_field: str,
    key: str,
    page_size: int,
    max_pages: int,
    interval_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[list[Mapping[str, Any]]]:
    """Pages of items, newest first, until the first item at or before the cursor.

    `fetch_page(n)` returns page `n` (from 1) of items sorted by `cursor_field`,
    newest first, at most `page_size` of them. An item modified while paging
    moves to page 1 and pushes the rest down, so it can come back on a later
    page: only its first, newer, read is yielded (`key` identifies it).

    The page budget is `max_pages`, with `interval_seconds` between requests
    for an API's rate limit. With a cursor, running out of budget before
    reaching it raises `CursorNotReached`: stopping would move the cursor past
    items never read. With no cursor yet, the budget's pages are the backfill
    and the generator stops there, so a first run loads the newest items even
    when nothing changed recently.
    """
    seen: set[Any] = set()
    for page in range(1, max_pages + 1):
        if page > 1 and interval_seconds:
            sleep(interval_seconds)
        items = fetch_page(page)
        fresh = [item for item in items if item[key] not in seen]
        seen.update(item[key] for item in fresh)
        if fresh:
            yield fresh
        if len(items) < page_size or (items and items[-1][cursor_field] <= cursor.start_value):
            return
    if _has_cursor(cursor):
        raise CursorNotReached(
            f"{max_pages} pages of {page_size} did not reach the cursor {cursor_field}={cursor.start_value}; "
            "raise the page budget or narrow the query"
        )


def keyset_batches(
    connection: Any,
    query: Any,
    cursor: Any,
    *,
    cursor_field: str,
    batch_size: int,
    max_rows: int,
    params: Mapping[str, Any] | None = None,
) -> Iterator[list[dict[str, Any]]]:
    """Rows past the cursor in `cursor_field` order, in batches, at most `max_rows` a run.

    `query` is a SQLAlchemy statement with the bind parameters `:last` and
    `:limit`, of the form `... where <cursor_field> > :last order by
    <cursor_field> limit :limit`; `params` adds the query's own. A run reads up
    to `max_rows`, so a large table backfills one slice per run, and dlt saves
    the cursor only once a batch is loaded, so a killed run resumes from the
    last loaded key.
    """
    last, loaded = cursor.start_value, 0
    while loaded < max_rows:
        limit = min(batch_size, max_rows - loaded)
        result = connection.execute(query, {**(params or {}), "last": last, "limit": limit})
        rows = [dict(row) for row in result.mappings()]
        if not rows:
            return
        yield rows
        last, loaded = rows[-1][cursor_field], loaded + len(rows)
        if len(rows) < limit:
            return


def sql_engine(drivername: str, binding: Mapping[str, Any], *, connect_args: Mapping[str, Any] | None = None) -> Any:
    """A SQLAlchemy engine for a database source from its bound `host`, `port`, `database`, `username`, `password`.

    `binding` holds the source's `runtime.config` and `runtime.secrets` values,
    as dlt resolved them; keys outside those five are ignored, and absent ones
    are left out of the URL. `connect_args` goes to the driver as is, e.g.
    `{"connect_timeout": 30}` for psycopg2.
    """
    import sqlalchemy as sa

    keys = ("host", "port", "database", "username", "password")
    url = sa.URL.create(drivername, **{name: binding[name] for name in keys if name in binding})
    return sa.create_engine(url, connect_args=dict(connect_args or {}))
