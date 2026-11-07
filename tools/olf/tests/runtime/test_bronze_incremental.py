from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("dlt")
sa = pytest.importorskip("sqlalchemy")

import dlt  # noqa: E402
from openlakeforge_runtime.bronze_dlt import load_dlt_source  # noqa: E402
from openlakeforge_runtime.bronze_incremental import (  # noqa: E402
    CursorNotReached,
    keyset_batches,
    newest_first_pages,
    sql_engine,
)


def _cursor(start: int, initial: int = 0) -> SimpleNamespace:
    return SimpleNamespace(start_value=start, initial_value=initial)


class _Api:
    """Items sorted newest first by `t`, served `page_size` at a time."""

    def __init__(self, items: dict[str, int], page_size: int = 2) -> None:
        self.items, self.page_size, self.pages = items, page_size, []

    def fetch(self, page: int) -> list[dict[str, Any]]:
        self.pages.append(page)
        ordered = sorted(({"code": c, "t": t} for c, t in self.items.items()), key=lambda i: i["t"], reverse=True)
        return ordered[(page - 1) * self.page_size : page * self.page_size]


def _codes(batches: Iterator[list[Any]]) -> list[str]:
    return [item["code"] for batch in batches for item in batch]


def _pages(api: _Api, cursor: SimpleNamespace, max_pages: int = 10) -> Iterator[list[Any]]:
    return newest_first_pages(api.fetch, cursor, cursor_field="t", key="code", page_size=2, max_pages=max_pages)


def test_newest_first_stops_at_the_first_page_that_reaches_the_cursor() -> None:
    api = _Api({"a": 50, "b": 40, "c": 30, "d": 20, "e": 10})

    assert _codes(_pages(api, _cursor(35))) == ["a", "b", "c", "d"]
    assert api.pages == [1, 2]


def test_newest_first_skips_an_item_read_twice_while_paging() -> None:
    api = _Api({"a": 50, "b": 40, "c": 30})
    original = api.fetch

    def fetch(page: int) -> list[dict[str, Any]]:
        if page == 2:  # `b` was edited between the two requests and moved to the top
            return [{"code": "b", "t": 60}, {"code": "c", "t": 30}]
        return original(page)

    assert _codes(newest_first_pages(fetch, _cursor(0), cursor_field="t", key="code", page_size=2, max_pages=5)) == [
        "a",
        "b",
        "c",
    ]


def test_newest_first_fails_closed_when_the_budget_runs_out_before_the_cursor() -> None:
    api = _Api({f"p{i}": 100 - i for i in range(10)})

    with pytest.raises(CursorNotReached, match="t=1"):
        list(_pages(api, _cursor(1), max_pages=2))


def test_newest_first_backfills_the_budget_when_there_is_no_cursor_yet() -> None:
    api = _Api({f"p{i}": 100 - i for i in range(10)})

    assert _codes(_pages(api, _cursor(0), max_pages=2)) == ["p0", "p1", "p2", "p3"]


def test_newest_first_waits_between_requests_only() -> None:
    api, waits = _Api({"a": 3, "b": 2, "c": 1}), []

    list(newest_first_pages(api.fetch, _cursor(0), cursor_field="t", key="code", page_size=2, max_pages=5,
                            interval_seconds=6, sleep=waits.append))  # fmt: skip

    assert waits == [6]


@pytest.fixture
def table(tmp_path: Path) -> Any:
    engine = sql_engine("sqlite", {"database": str(tmp_path / "source.db")})
    with engine.begin() as connection:
        connection.execute(sa.text("create table rna (id integer primary key, urs text)"))
        connection.execute(sa.text("insert into rna values " + ", ".join(f"({i}, 'URS{i}')" for i in range(1, 8))))
    return engine


_QUERY = "select id, urs from rna where id > :last order by id limit :limit"


def test_keyset_batches_read_past_the_cursor_and_cap_a_run(table: Any) -> None:
    with table.connect() as connection:
        batches = list(
            keyset_batches(connection, sa.text(_QUERY), _cursor(2), cursor_field="id", batch_size=2, max_rows=3)
        )

    assert [[row["id"] for row in batch] for batch in batches] == [[3, 4], [5]]


def test_keyset_batches_stop_when_the_table_is_exhausted(table: Any) -> None:
    with table.connect() as connection:
        batches = list(
            keyset_batches(connection, sa.text(_QUERY), _cursor(5), cursor_field="id", batch_size=10, max_rows=100)
        )

    assert [[row["id"] for row in batch] for batch in batches] == [[6, 7]]


def test_sql_engine_ignores_keys_that_are_not_connection_settings(tmp_path: Path) -> None:
    engine = sql_engine("sqlite", {"database": str(tmp_path / "x.db"), "country": "be"})

    assert engine.url.database == str(tmp_path / "x.db")


_SOURCE_YAML = """\
apiVersion: openlakeforge.io/v1beta1
kind: Source
name: shop
displayName: Shop
description: A source built on the incremental helpers.
status: planned
resources:
  - name: products
    load: { mode: incremental, cursor: t, primary_key: [code] }
  - name: orders
    load: { mode: incremental, cursor: id, primary_key: [id] }
"""


def test_connectors_built_on_the_helpers_load_incrementally(tmp_path: Path, table: Any) -> None:
    """The helpers under a real dlt source and `load_dlt_source`: first run, no change, then new rows."""
    root, bucket = tmp_path / "lakehouse_code", tmp_path / "bronze"
    (root / "bronze" / "shop").mkdir(parents=True)
    (root / "bronze" / "shop" / "source.yaml").write_text(_SOURCE_YAML, encoding="utf-8")
    api = _Api({"a": 30, "b": 20, "c": 10})

    @dlt.source(name="shop")
    def shop() -> Any:
        @dlt.resource(name="products")
        def products(t: Any = dlt.sources.incremental("t", initial_value=0)) -> Iterator[Any]:  # noqa: B008
            yield from newest_first_pages(api.fetch, t, cursor_field="t", key="code", page_size=2, max_pages=5)

        @dlt.resource(name="orders")
        def orders(id: Any = dlt.sources.incremental("id", initial_value=0)) -> Iterator[Any]:  # noqa: A002, B008
            with table.connect() as connection:
                yield from keyset_batches(connection, sa.text(_QUERY), id, cursor_field="id", batch_size=3, max_rows=4)

        return products, orders

    def load() -> dict[str, int]:
        environ = {"OPENLAKEFORGE_STAGE": "dev"}
        results = load_dlt_source(
            "shop", shop(), ("products", "orders"), environ=environ, bucket_url=bucket.as_uri(), lakehouse_root=root
        )
        return {name: result.rows for name, result in results.items()}

    assert load() == {"products": 3, "orders": 4}
    api.items["d"] = 40
    assert load() == {"products": 1, "orders": 3}
    assert load() == {"products": 0, "orders": 0}
