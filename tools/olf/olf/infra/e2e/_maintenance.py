"""Iceberg maintenance proof: a dry run, then a real run, of the Silver and Gold maintenance jobs.

The jobs keep their retention defaults (7 days, 10 snapshots), and both engines
refuse less, so a freshly deployed stack has nothing old enough to expire: on
kind the real run proves the procedures execute with the stage's credentials
and change nothing they must not, not that deletion happens.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from openlakeforge_services.dagster import DagsterClient

from olf.common import log
from olf.infra import trino
from olf.infra.e2e._dagster import dagster_client, launch_and_poll
from olf.infra.e2e._shell import E2EConfig, E2EError
from olf.infra.e2e._trino import stage_catalog_name, trino_query

# The runtime's own job and op names (openlakeforge_runtime/product_dagster.py).
SILVER_JOB, SILVER_OP = "silver_maintenance", "maintain_silver"
GOLD_JOB, GOLD_OP = "gold_maintenance", "maintain_gold"

# (schema, table) -> (snapshots, rows)
TableState = dict[tuple[str, str], tuple[int, int]]

_RUN_OUTPUT_METADATA = """
query RunOutputMetadata($runId: ID!) {
  logsForRun(runId: $runId) {
    __typename
    ... on EventConnection {
      events {
        __typename
        ... on ExecutionStepOutputEvent {
          stepKey
          metadataEntries {
            __typename
            label
            ... on IntMetadataEntry { intRepr }
            ... on BoolMetadataEntry { boolValue }
            ... on JsonMetadataEntry { jsonString }
            ... on TextMetadataEntry { text }
          }
        }
      }
    }
    ... on PythonError { message }
  }
}
"""


def check_iceberg_maintenance(cfg: E2EConfig) -> None:
    log.step("Running Silver and Gold maintenance, dry run then for real...")
    silver = _silver_tables(cfg)
    gold = _gold_tables(cfg)
    if not silver or not gold:
        raise E2EError("maintenance needs materialised Silver and Gold tables; Trino lists none.")
    with dagster_client(cfg) as client:
        state = table_state(cfg, silver)
        for dry_run in (True, False):
            metadata = _run(cfg, client, SILVER_JOB, SILVER_OP, dry_run)
            after = table_state(cfg, silver)
            check_silver_result(metadata, state, after, dry_run=dry_run)
            state = after
        state = table_state(cfg, gold)
        past_window: dict[tuple[str, str], int] = {}
        for dry_run in (True, False):
            metadata = _run(cfg, client, GOLD_JOB, GOLD_OP, dry_run)
            after = table_state(cfg, gold)
            past_window = check_gold_result(metadata, state, after, dry_run=dry_run, past_window=past_window)
            state = after


def _prefix(cfg: E2EConfig) -> str:
    return trino.glue_namespace_prefix(cfg.env, stage_catalog_name(cfg))


def base_tables(cfg: E2EConfig, schema: str) -> list[str]:
    """The Iceberg tables in `schema`; views have no snapshots, and the jobs skip them too."""
    sql = (
        f"SELECT table_name FROM {stage_catalog_name(cfg)}.information_schema.tables "
        f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE' ORDER BY table_name"
    )
    return [line.strip() for line in trino_query(cfg, sql).splitlines() if line.strip()]


def _silver_tables(cfg: E2EConfig) -> list[tuple[str, str]]:
    prefix = _prefix(cfg)
    return [
        (f"{prefix}{domain.silver_namespace}", table)
        for domain in cfg.inventory.domains
        for table in base_tables(cfg, f"{prefix}{domain.silver_namespace}")
    ]


def _gold_tables(cfg: E2EConfig) -> list[tuple[str, str]]:
    prefix = _prefix(cfg)
    return [
        (f"{prefix}{product.gold_namespace}", table)
        for product in cfg.inventory.products
        for table in base_tables(cfg, f"{prefix}{product.gold_namespace}")
    ]


def _run(cfg: E2EConfig, client: DagsterClient, job: str, op: str, dry_run: bool) -> dict[str, Any]:
    run_id = launch_and_poll(cfg, client, job, run_config={"ops": {op: {"config": {"dry_run": dry_run}}}})
    metadata = run_output_metadata(client, run_id)
    if metadata.get("dry_run") is not dry_run:
        raise E2EError(f"{job} run {run_id} reported dry_run={metadata.get('dry_run')!r}, launched with {dry_run}.")
    return metadata


def run_output_metadata(client: DagsterClient, run_id: str) -> dict[str, Any]:
    """The output metadata of the run's step, by label, decoded from Dagster's typed entries."""
    result = client.graphql(_RUN_OUTPUT_METADATA, {"runId": run_id})["logsForRun"]
    if result.get("__typename") != "EventConnection":
        raise E2EError(f"Dagster could not read the events of run {run_id}: {result.get('message', result)}")
    metadata: dict[str, Any] = {}
    for event in result.get("events", []):
        for entry in event.get("metadataEntries") or []:
            metadata[entry["label"]] = _entry_value(entry)
    if not metadata:
        raise E2EError(f"run {run_id} recorded no output metadata.")
    return metadata


def _entry_value(entry: Mapping[str, Any]) -> Any:
    kind = entry["__typename"]
    if kind == "IntMetadataEntry":
        return int(entry["intRepr"])
    if kind == "BoolMetadataEntry":
        return entry["boolValue"]
    if kind == "JsonMetadataEntry":
        return json.loads(entry["jsonString"])
    return entry.get("text")


def table_state(cfg: E2EConfig, tables: list[tuple[str, str]]) -> TableState:
    """Each table's snapshot and row counts, in one Trino query."""
    catalog = stage_catalog_name(cfg)
    selects = [
        f"SELECT '{schema}', '{table}', "
        f'(SELECT count(*) FROM {catalog}."{schema}"."{table}$snapshots"), '
        f'(SELECT count(*) FROM {catalog}."{schema}"."{table}")'
        for schema, table in tables
    ]
    state: TableState = {}
    for line in trino_query(cfg, " UNION ALL ".join(selects)).splitlines():
        if line.strip():
            schema, table, snapshots, rows = line.strip().split(",")
            state[(schema, table)] = (int(snapshots), int(rows))
    if set(state) != set(tables):
        raise E2EError(f"Trino returned counts for {sorted(state)}, expected {sorted(tables)}.")
    return state


def _unchanged_rows(job: str, before: TableState, after: TableState) -> None:
    changed = [
        f"{s}.{t}: {before[s, t][1]} -> {after[s, t][1]}" for s, t in before if before[s, t][1] != after[s, t][1]
    ]
    if changed:
        raise E2EError(f"{job} changed row counts: {', '.join(changed)}")


def check_silver_result(metadata: Mapping[str, Any], before: TableState, after: TableState, *, dry_run: bool) -> None:
    """Floe's per-table report agrees with the catalog, and only a real run removes snapshots."""
    # Floe names each result's physical table `<namespace>.<table>`; entity names repeat across domains.
    results = {tuple(result["table"].rsplit(".", 1)): result for result in metadata.get("tables") or []}
    _unchanged_rows(SILVER_JOB, before, after)
    for (schema, table), (snapshots, _rows) in before.items():
        result = results.get((schema, table))
        if result is None:
            raise E2EError(f"{SILVER_JOB} reported no result for {schema}.{table}.")
        expired, kept = result["expired_snapshots"], result["kept_snapshots"]
        if expired + kept != snapshots:
            raise E2EError(
                f"{SILVER_JOB}: {schema}.{table} has {snapshots} snapshots, "
                f"Floe counted {expired} expired + {kept} kept."
            )
        expected = snapshots if dry_run else kept
        if after[schema, table][0] != expected:
            raise E2EError(
                f"{SILVER_JOB} (dry_run={dry_run}): {schema}.{table} has {after[schema, table][0]} snapshots, "
                f"expected {expected}."
            )


def check_gold_result(
    metadata: Mapping[str, Any],
    before: TableState,
    after: TableState,
    *,
    dry_run: bool,
    past_window: Mapping[tuple[str, str], int],
) -> dict[tuple[str, str], int]:
    """Every Gold table was maintained without error; return the dry run's per-table candidates.

    A dry run removes nothing. A real run removes at most the snapshots the dry run
    counted past the window, an upper bound (gold_maintenance.maintain_gold).
    """
    results = {(result["schema"], result["table"]): result for result in metadata.get("results") or []}
    _unchanged_rows(GOLD_JOB, before, after)
    counted: dict[tuple[str, str], int] = {}
    for key, (snapshots, _rows) in before.items():
        result = results.get(key)
        if result is None:
            raise E2EError(f"{GOLD_JOB} reported no result for {key[0]}.{key[1]}.")
        if "error" in result:
            raise E2EError(f"{GOLD_JOB}: {key[0]}.{key[1]} failed: {result['error']}")
        remaining = after[key][0]
        if dry_run:
            counted[key] = int(result["snapshots_past_window"])
            floor = snapshots
        else:
            floor = max(1, snapshots - past_window.get(key, 0))
        if not floor <= remaining <= snapshots:
            raise E2EError(
                f"{GOLD_JOB} (dry_run={dry_run}): {key[0]}.{key[1]} went from {snapshots} to {remaining} snapshots."
            )
    return counted
