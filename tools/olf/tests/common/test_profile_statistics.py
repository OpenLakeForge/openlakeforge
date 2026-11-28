from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from openlakeforge_services.profile_statistics import read_table_statistics

NOW = datetime(2026, 11, 28, 12, tzinfo=UTC)
TABLE = "polaris.lakehouse_dev.sales_core_gold.fct_order_lines"


def _ms(age: timedelta) -> int:
    return int((NOW - age).timestamp() * 1000)


class _Client:
    """Serves the two profile endpoints, as {path suffix: rows}; records every request."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, path: str, *, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        assert method == "GET"
        self.requests.append((path, params))
        for suffix, data in self.rows.items():
            if path.endswith(suffix):
                return {"data": data}
        return {"data": []}


def test_the_latest_table_and_column_profiles_are_read_from_the_time_series_endpoints():
    client = _Client(
        {
            "/tableProfile": [
                {"timestamp": _ms(timedelta(days=3)), "rowCount": 10},
                {"timestamp": _ms(timedelta(hours=2)), "rowCount": 12},
            ],
            "order_date/columnProfile": [
                {"timestamp": _ms(timedelta(hours=2)), "nullCount": 0, "distinctCount": 4, "min": "2026-01-01"},
            ],
        }
    )

    stats = read_table_statistics(client, TABLE, ["order_date", "net_amount"], now=NOW)  # type: ignore[arg-type]

    assert stats.row_count == 12 and not stats.stale
    assert stats.profiled_at == NOW - timedelta(hours=2)
    by_name = {c.name: c for c in stats.columns}
    assert (by_name["order_date"].null_count, by_name["order_date"].minimum) == (0, "2026-01-01")
    assert by_name["net_amount"].null_count is None
    paths = [path for path, _ in client.requests]
    assert paths[0] == f"/api/v1/tables/{TABLE.replace('.', '%2E')}/tableProfile".replace("%2E", ".")
    assert all({"startTs", "endTs"} <= set(params) for _, params in client.requests)


def test_an_absent_or_overdue_profile_is_marked_stale_never_current():
    absent = read_table_statistics(_Client({}), TABLE, [], now=NOW)  # type: ignore[arg-type]
    overdue = read_table_statistics(
        _Client({"/tableProfile": [{"timestamp": _ms(timedelta(days=2)), "rowCount": 5}]}),  # type: ignore[arg-type]
        TABLE,
        [],
        now=NOW,
    )

    assert absent.stale and absent.profiled_at is None and absent.row_count is None
    assert overdue.stale and overdue.row_count == 5


def test_a_recent_table_profile_does_not_hide_a_column_row_that_is_overdue():
    client = _Client(
        {
            "/tableProfile": [{"timestamp": _ms(timedelta(hours=1)), "rowCount": 5}],
            "old/columnProfile": [{"timestamp": _ms(timedelta(days=3)), "nullCount": 0}],
            "new/columnProfile": [{"timestamp": _ms(timedelta(hours=1)), "nullCount": 0}],
        }
    )

    fresh = read_table_statistics(client, TABLE, ["new"], now=NOW)  # type: ignore[arg-type]
    mixed = read_table_statistics(client, TABLE, ["new", "old"], now=NOW)  # type: ignore[arg-type]

    assert not fresh.stale
    assert mixed.stale
    assert {c.name: c.profiled_at is not None for c in mixed.columns} == {"new": True, "old": True}
