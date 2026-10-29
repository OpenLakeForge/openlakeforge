"""Product-job timings from Dagster and table facts from Trino."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from olf.bench._record import BenchError
from olf.clients.base import ServiceClientError
from olf.clients.dagster import DagsterClient

RUN_STATS_QUERY = """
query RunStats($runId: ID!) {
  runOrError(runId: $runId) {
    __typename
    ... on Run { status startTime endTime stepStats { stepKey status startTime endTime } }
    ... on PythonError { message }
  }
}
"""

Query = Callable[[str], str]


def layer_of(step_key: str) -> str:
    """The lakehouse layer a product-job step belongs to.

    `libs/product_dagster.py` names the Bronze loader `<source>_bronze_resources`
    and the dbt step `<product>_dbt_gold_assets`; a product job selects only
    Bronze, Silver and Gold assets, so every other step is a Floe domain step.
    """
    if step_key.endswith("_bronze_resources"):
        return "bronze"
    if step_key.endswith("_dbt_gold_assets"):
        return "gold"
    return "silver"


def summarize_run(stats: Mapping[str, Any]) -> dict[str, Any]:
    steps = [
        {
            "step": step["stepKey"],
            "layer": layer_of(step["stepKey"]),
            "status": step.get("status"),
            "start": step.get("startTime"),
            "end": step.get("endTime"),
            "seconds": _span(step.get("startTime"), step.get("endTime")),
        }
        for step in stats.get("stepStats") or []
    ]
    layers: dict[str, dict[str, Any]] = {}
    for layer in ("bronze", "silver", "gold"):
        members = [step for step in steps if step["layer"] == layer and step["start"] is not None]
        if not members:
            continue
        # Steps of one layer can overlap (two sources load at once): report the layer's wall-clock span.
        start = min(step["start"] for step in members)
        ends = [step["end"] for step in members if step["end"] is not None]
        end = max(ends) if ends else None
        layers[layer] = {"start": start, "end": end, "seconds": _span(start, end), "steps": len(members)}
    return {
        "status": stats.get("status"),
        "start": stats.get("startTime"),
        "end": stats.get("endTime"),
        "seconds": _span(stats.get("startTime"), stats.get("endTime")),
        "layers": layers,
        "steps": steps,
    }


def run_job(client: DagsterClient, job: str, detail: dict[str, Any], *, timeout_seconds: int) -> None:
    """Launch `job`, wait for it, and fill `detail` with its run and step timings, even when it fails."""
    run_id = client.launch(job)
    detail.update(job=job, run_id=run_id)
    failure: str | None = None
    try:
        client.poll(job, run_id, timeout_seconds=timeout_seconds)
    except ServiceClientError as exc:
        failure = str(exc)
    result = client.graphql(RUN_STATS_QUERY, {"runId": run_id})["runOrError"]
    if result.get("__typename") == "Run":
        detail.update(summarize_run(result))
    if failure:
        raise BenchError(failure)


def column_expression(name: str, data_type: str) -> str:
    """A deterministic text rendering of one column for the row checksum.

    Floating-point aggregates may differ in their last bits between runs, so
    `double` and `real` are rounded; everything else is compared exactly.
    """
    column = '"' + name.replace('"', '""') + '"'
    if data_type in {"double", "real"}:
        return f"format('%.6f', {column})"
    return f"cast({column} as varchar)"


def table_facts(query: Query, catalog: str, schema: str, table: str) -> dict[str, Any]:
    """Rows, an order-independent content checksum, and the Iceberg files and snapshots behind the table."""
    columns = _rows(
        query(
            f"SELECT column_name, data_type FROM {catalog}.information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY ordinal_position"
        ),
        maxsplit=1,
    )
    if not columns:
        raise BenchError(f"{catalog}.{schema}.{table}: Trino lists no columns")
    rendered = ", ".join(column_expression(name, data_type) for name, data_type in columns)
    fqn = f'{catalog}.{schema}."{table}"'
    rows, checksum = _single(
        query(f"SELECT count(*), to_hex(checksum(array_join(ARRAY[{rendered}], '|', '<null>'))) FROM {fqn}"), 2
    )
    files, file_bytes = _single(
        query(f'SELECT count(*), coalesce(sum(file_size_in_bytes), 0) FROM {catalog}.{schema}."{table}$files"'), 2
    )
    (snapshots,) = _single(query(f'SELECT count(*) FROM {catalog}.{schema}."{table}$snapshots"'), 1)
    return {
        "rows": int(rows),
        "checksum": checksum or None,
        "data_files": int(files),
        "data_file_bytes": int(file_bytes),
        "snapshots": int(snapshots),
    }


def _rows(output: str, *, maxsplit: int = -1) -> list[list[str]]:
    return [line.strip().split(",", maxsplit) for line in output.splitlines() if line.strip()]


def _single(output: str, width: int) -> list[str]:
    rows = _rows(output, maxsplit=width - 1)
    if not rows or len(rows[-1]) != width:
        raise BenchError(f"expected one row of {width} value(s) from Trino, got {output!r}")
    return rows[-1]


def _span(start: float | None, end: float | None) -> float | None:
    return round(end - start, 3) if start is not None and end is not None else None
