import contextlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import E2E_INVENTORY, e2e_cfg
from openlakeforge_services.dagster import DagsterClient

from olf.infra.e2e import _maintenance
from olf.infra.e2e._shell import E2EError

_TABLE = re.compile(r'"([^"]+)"\."([^"$]+)\$snapshots"')


def _metadata_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    events = [{"__typename": "RunStartEvent"}, {"__typename": "ExecutionStepOutputEvent", "metadataEntries": entries}]
    return {"data": {"logsForRun": {"__typename": "EventConnection", "events": events}}}


def _entries(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Dagster's typed GraphQL entries for `metadata`, as the op's `add_output_metadata` records them."""
    entries = []
    for label, value in metadata.items():
        if isinstance(value, bool):
            entries.append({"__typename": "BoolMetadataEntry", "label": label, "boolValue": value})
        elif isinstance(value, int):
            entries.append({"__typename": "IntMetadataEntry", "label": label, "intRepr": str(value)})
        else:
            entries.append({"__typename": "JsonMetadataEntry", "label": label, "jsonString": json.dumps(value)})
    return entries


def test_run_output_metadata_decodes_the_typed_entries() -> None:
    tables = [{"entity": "orders", "expired_snapshots": 0, "kept_snapshots": 2}]
    response = _metadata_response(
        [
            *_entries({"dry_run": True, "orphan_bytes": 2**40, "tables": tables}),
            {"__typename": "TextMetadataEntry", "label": "note", "text": "hi"},
        ]
    )
    client = DagsterClient("http://dagster/graphql", request_json=lambda _query, _variables=None: response)

    assert _maintenance.run_output_metadata(client, "run-1") == {
        "dry_run": True,
        "orphan_bytes": 2**40,
        "tables": tables,
        "note": "hi",
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"data": {"logsForRun": {"__typename": "PythonError", "message": "boom"}}}, "boom"),
        (_metadata_response([]), "no output metadata"),
    ],
)
def test_run_output_metadata_fails_without_metadata(response: dict[str, Any], message: str) -> None:
    client = DagsterClient("http://dagster/graphql", request_json=lambda _query, _variables=None: response)

    with pytest.raises(E2EError, match=message):
        _maintenance.run_output_metadata(client, "run-1")


def test_table_state_counts_snapshots_and_rows_in_one_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    queries: list[str] = []

    def trino_query(_cfg: object, sql: str) -> str:
        queries.append(sql)
        return "sales_silver,orders,3,120\nsales_gold,daily,1,7\n"

    monkeypatch.setattr(_maintenance, "trino_query", trino_query)
    tables = [("sales_silver", "orders"), ("sales_gold", "daily")]

    assert _maintenance.table_state(e2e_cfg(tmp_path), tables) == {
        ("sales_silver", "orders"): (3, 120),
        ("sales_gold", "daily"): (1, 7),
    }
    (sql,) = queries
    assert _TABLE.findall(sql) == tables


def test_silver_results_are_matched_by_schema_not_only_by_entity() -> None:
    """Two domains may both have an `orders` entity; each table is checked against its own report."""
    other = ("crm_silver", "orders")
    metadata = {
        "tables": [
            {"entity": "orders", "table": "sales_silver.orders", "expired_snapshots": 0, "kept_snapshots": 2},
            {"entity": "orders", "table": "crm_silver.orders", "expired_snapshots": 0, "kept_snapshots": 3},
        ]
    }
    state = {_KEY: (2, 5), other: (3, 7)}

    _maintenance.check_silver_result(metadata, state, state, dry_run=True)


def test_base_tables_lists_only_iceberg_tables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    queries: list[str] = []

    def trino_query(_cfg: object, sql: str) -> str:
        queries.append(sql)
        return "daily\nweekly\n"

    monkeypatch.setattr(_maintenance, "trino_query", trino_query)

    assert _maintenance.base_tables(e2e_cfg(tmp_path), "sales_gold") == ["daily", "weekly"]
    (sql,) = queries
    assert "information_schema.tables" in sql
    assert "table_schema = 'sales_gold'" in sql
    assert "table_type = 'BASE TABLE'" in sql


def test_table_state_fails_when_a_table_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_maintenance, "trino_query", lambda _cfg, _sql: "sales_silver,orders,3,120\n")

    with pytest.raises(E2EError, match="expected"):
        _maintenance.table_state(e2e_cfg(tmp_path), [("sales_silver", "orders"), ("sales_gold", "daily")])


_KEY = ("sales_silver", "orders")


def _silver(expired: int, kept: int) -> dict[str, Any]:
    return {
        "tables": [
            {"entity": "orders", "table": "sales_silver.orders", "expired_snapshots": expired, "kept_snapshots": kept}
        ]
    }


@pytest.mark.parametrize(
    ("metadata", "after", "dry_run", "message"),
    [
        (_silver(2, 10), {_KEY: (12, 5)}, True, None),
        (_silver(2, 10), {_KEY: (10, 5)}, False, None),
        (_silver(2, 10), {_KEY: (10, 5)}, True, "has 10 snapshots, expected 12"),
        (_silver(2, 10), {_KEY: (12, 5)}, False, "has 12 snapshots, expected 10"),
        (_silver(0, 11), {_KEY: (12, 5)}, True, "Floe counted 0 expired \\+ 11 kept"),
        (_silver(0, 12), {_KEY: (12, 4)}, False, "changed row counts"),
        ({"tables": []}, {_KEY: (12, 5)}, True, "no result for sales_silver.orders"),
    ],
)
def test_check_silver_result(
    metadata: dict[str, Any], after: dict[tuple[str, str], tuple[int, int]], dry_run: bool, message: str | None
) -> None:
    before = {_KEY: (12, 5)}
    with pytest.raises(E2EError, match=message) if message else contextlib.nullcontext():
        _maintenance.check_silver_result(metadata, before, after, dry_run=dry_run)


_GOLD = ("sales_gold", "daily")


def _gold(**result: Any) -> dict[str, Any]:
    return {"results": [{"schema": "sales_gold", "table": "daily", **result}]}


def test_gold_dry_run_returns_its_candidates_and_must_remove_nothing() -> None:
    before = {_GOLD: (12, 5)}
    metadata = _gold(dry_run=True, snapshots_past_window=3)

    assert _maintenance.check_gold_result(metadata, before, before, dry_run=True, past_window={}) == {_GOLD: 3}
    with pytest.raises(E2EError, match="went from 12 to 11 snapshots"):
        _maintenance.check_gold_result(metadata, before, {_GOLD: (11, 5)}, dry_run=True, past_window={})


@pytest.mark.parametrize(("remaining", "ok"), [(12, True), (9, True), (8, False), (13, False)])
def test_gold_real_run_removes_at_most_the_dry_run_candidates(remaining: int, ok: bool) -> None:
    before, after = {_GOLD: (12, 5)}, {_GOLD: (remaining, 5)}
    with contextlib.nullcontext() if ok else pytest.raises(E2EError, match=f"went from 12 to {remaining}"):
        _maintenance.check_gold_result(_gold(dry_run=False), before, after, dry_run=False, past_window={_GOLD: 3})


@pytest.mark.parametrize(
    ("metadata", "message"),
    [(_gold(dry_run=False, error="Access Denied"), "failed: Access Denied"), ({"results": []}, "no result")],
)
def test_gold_result_fails_on_a_table_error_or_a_missing_table(metadata: dict[str, Any], message: str) -> None:
    before = {_GOLD: (12, 5)}
    with pytest.raises(E2EError, match=message):
        _maintenance.check_gold_result(metadata, before, before, dry_run=False, past_window={})


class _FakeStack:
    """Dagster and Trino for the full proof: each table holds 2 snapshots, none past the window."""

    def __init__(self, *, report_dry_run: bool | None = None) -> None:
        self.launches: list[tuple[str, Any]] = []
        self.report_dry_run = report_dry_run
        self.silver = {
            (domain.silver_namespace, entity.name): (2, 5)
            for domain in E2E_INVENTORY.domains
            for entity in E2E_INVENTORY.floe_entities(domain)
        }
        self.gold = {(product.gold_namespace, "mart"): (2, 5) for product in E2E_INVENTORY.products}

    def base_tables(self, _cfg: object, schema: str) -> list[str]:
        return [table for (namespace, table) in [*self.silver, *self.gold] if namespace == schema]

    def trino_query(self, _cfg: object, sql: str) -> str:
        state = {**self.silver, **self.gold}
        return "\n".join(f"{s},{t},{state[s, t][0]},{state[s, t][1]}" for s, t in _TABLE.findall(sql))

    def launch_and_poll(self, _cfg: object, _client: object, job: str, *, run_config: Any) -> str:
        self.launches.append((job, run_config))
        return f"run-{len(self.launches)}"

    def run_output_metadata(self, _client: object, run_id: str) -> dict[str, Any]:
        job, run_config = self.launches[int(run_id.removeprefix("run-")) - 1]
        (op_config,) = (op["config"] for op in run_config["ops"].values())
        dry_run = op_config["dry_run"] if self.report_dry_run is None else self.report_dry_run
        if job == _maintenance.SILVER_JOB:
            tables = [
                {"entity": t, "table": f"{s}.{t}", "expired_snapshots": 0, "kept_snapshots": 2} for s, t in self.silver
            ]
            return {"dry_run": dry_run, "tables": tables}
        results = [{"schema": s, "table": t, "dry_run": dry_run, "snapshots_past_window": 0} for s, t in self.gold]
        return {"dry_run": dry_run, "results": results}


def _install(monkeypatch: pytest.MonkeyPatch, stack: _FakeStack) -> None:
    monkeypatch.setattr(_maintenance, "dagster_client", lambda _cfg: contextlib.nullcontext(object()))
    for name in ("base_tables", "trino_query", "launch_and_poll", "run_output_metadata"):
        monkeypatch.setattr(_maintenance, name, getattr(stack, name))


def test_maintenance_runs_each_job_dry_then_for_real_then_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stack = _FakeStack()
    _install(monkeypatch, stack)

    _maintenance.check_iceberg_maintenance(e2e_cfg(tmp_path))

    assert stack.launches == [
        (job, {"ops": {op: {"config": {"dry_run": dry_run}}}})
        for job, op in (
            (_maintenance.SILVER_JOB, _maintenance.SILVER_OP),
            (_maintenance.GOLD_JOB, _maintenance.GOLD_OP),
        )
        for dry_run in (True, False, False)
    ]


def test_maintenance_fails_when_a_rerun_changes_a_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stack = _FakeStack()
    _install(monkeypatch, stack)
    real_runs = 0

    def run_output_metadata(client: object, run_id: str) -> dict[str, Any]:
        nonlocal real_runs
        metadata = stack.run_output_metadata(client, run_id)
        if metadata["dry_run"] is False:
            real_runs += 1
            if real_runs == 2:  # the Silver rerun expires a snapshot the first real run left
                key = next(iter(stack.silver))
                stack.silver[key] = (1, stack.silver[key][1])
                metadata["tables"][0]["expired_snapshots"] = 1
                metadata["tables"][0]["kept_snapshots"] = 1
        return metadata

    monkeypatch.setattr(_maintenance, "run_output_metadata", run_output_metadata)

    with pytest.raises(E2EError, match="silver_maintenance rerun changed tables"):
        _maintenance.check_iceberg_maintenance(e2e_cfg(tmp_path))


def test_maintenance_fails_when_a_run_ignores_its_dry_run_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stack = _FakeStack(report_dry_run=False)
    _install(monkeypatch, stack)

    with pytest.raises(E2EError, match="silver_maintenance run run-1 reported dry_run=False, launched with True"):
        _maintenance.check_iceberg_maintenance(e2e_cfg(tmp_path))
    assert len(stack.launches) == 1


def test_maintenance_needs_materialised_tables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stack = _FakeStack()
    stack.gold = {}
    _install(monkeypatch, stack)

    with pytest.raises(E2EError, match="Trino lists none"):
        _maintenance.check_iceberg_maintenance(e2e_cfg(tmp_path))
    assert stack.launches == []
