"""Scenario A (install and idle footprint) and scenario B (end-to-end ETL) of #328.

Deploy phases run as `olf deploy --phase <p>` subprocesses, so a phase is
timed through the same entry point a user runs and its output lands in its
own log. Pipelines and queries go through the e2e helpers in-process, inside
the stage's applied runtime environment.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olf import log
from olf.bench import _dataset, _pipeline, _resources
from olf.bench._record import BenchError, BenchRecord
from olf.e2e._dagster import dagster_client
from olf.e2e._shell import E2EConfig, E2EError, kubectl
from olf.e2e._trino import stage_catalog_name, trino_query

DEPLOY_PHASES = ("foundation", "prefetch", "platform", "artifacts")
FIRST_QUERY_TIMEOUT_SECONDS = 600

Session = Callable[[Path], AbstractContextManager[E2EConfig]]


@dataclass(frozen=True)
class Target:
    """Where the benchmark deploys and writes."""

    project_root: Path
    cluster_name: str
    kubeconfig_path: Path
    output_dir: Path
    job_timeout_seconds: int = 3600


def olf_command(target: Target, args: Sequence[str], *, project_root: Path, log_name: str) -> None:
    argv = [
        sys.executable,
        "-m",
        "olf.cli",
        *args,
        "--provider",
        "local",
        "--project-root",
        str(project_root),
        "--cluster-name",
        target.cluster_name,
        "--kubeconfig-path",
        str(target.kubeconfig_path),
    ]
    log_path = target.output_dir / "logs" / f"{log_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.step(f"olf {' '.join(args)} (log: {log_path})")
    with log_path.open("w", encoding="utf-8") as handle:
        returncode = subprocess.run(argv, stdout=handle, stderr=subprocess.STDOUT, check=False).returncode
    if returncode != 0:
        raise BenchError(f"olf {' '.join(args)} exited {returncode}; see {log_path}")


def footprint(
    record: BenchRecord,
    target: Target,
    *,
    session: Session,
    clean: bool,
    settle_seconds: int,
    idle_seconds: int,
    sample_interval: float,
) -> None:
    """Scenario A: clean install to first Gold rows, then the installed and steady-state footprint."""
    if clean:
        with record.timed("destroy_before_install", excluded_from_install=True):
            olf_command(target, ["destroy", "--force"], project_root=target.project_root, log_name="destroy")
    with ExitStack() as stack:
        sampler: _resources.Sampler | None = None
        for phase in DEPLOY_PHASES:
            with record.timed(f"deploy_{phase}") as detail:
                start = time.time()
                olf_command(target, ["deploy", "--phase", phase], project_root=target.project_root, log_name=phase)
                # The nodes exist once the foundation is up; usage is sampled from then on.
                if sampler is None:
                    nodes = _resources.kind_nodes(target.cluster_name)
                    sampler = stack.enter_context(_resources.Sampler(nodes, interval=sample_interval))
                else:
                    detail["cluster_usage"] = sampler.window(start, time.time())
        assert sampler is not None
        record.fact(
            "install_total",
            {"seconds": round(sum(m.seconds or 0 for m in record.measurements if m.name.startswith("deploy_")), 3)},
        )
        with session(target.project_root) as cfg:
            with record.timed("time_to_first_query") as detail:
                detail["attempts"] = _first_query(cfg)
            product = cfg.inventory.default_product
            with record.timed("first_pipeline", product=product.id) as detail:
                start = time.time()
                with dagster_client(cfg) as client:
                    _pipeline.run_job(client, product.job_name, detail, timeout_seconds=target.job_timeout_seconds)
                detail["cluster_usage"] = sampler.window(start, time.time())
                detail["gold"] = _gold_facts(cfg, [product.gold_namespace])
                _require_rows(detail["gold"], f"{product.id} Gold")
            record.fact("installed_footprint", _footprint(cfg, nodes))
            with record.timed("settle", seconds_requested=settle_seconds):
                time.sleep(settle_seconds)
            with record.timed("idle_window", seconds_requested=idle_seconds) as detail:
                start = time.time()
                time.sleep(idle_seconds)
                detail["cluster_usage"] = sampler.window(start, time.time())
                detail["sampler_errors"] = sampler.errors
            record.fact("steady_state_footprint", _footprint(cfg, nodes))


def etl(
    record: BenchRecord,
    target: Target,
    *,
    session: Session,
    scale: int,
    delta: int,
    sample_interval: float,
) -> None:
    """Scenario B: a scaled dataset through every product job, a no-change rerun, then a delta reload."""
    nodes = _resources.kind_nodes(target.cluster_name)
    with _resources.Sampler(nodes, interval=sample_interval) as sampler:
        baseline = _cycle(record, target, session=session, sampler=sampler, scale=scale, runs=("initial", "repeat"))
        for label, facts in baseline["repeat"].items():
            if facts.get("checksum") != baseline["initial"][label].get("checksum"):
                raise BenchError(f"{label}: content changed on a no-change rerun")
        if delta:
            _cycle(record, target, session=session, sampler=sampler, scale=scale + delta, runs=("delta",))


def _cycle(
    record: BenchRecord,
    target: Target,
    *,
    session: Session,
    sampler: _resources.Sampler,
    scale: int,
    runs: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Build the project at `scale`, then run every product job once per entry in `runs`."""
    project = _dataset.copy_project(target.project_root, target.output_dir / f"project-x{scale}")
    with record.timed("generate_dataset", scale=scale) as detail:
        tables = _dataset.generate(project, scale=scale)
        detail["tables"] = [vars(table) for table in tables]
        detail["rows"] = sum(table.rows for table in tables)
        detail["bytes"] = sum(table.bytes for table in tables)
    expected = {(table.source, table.resource): table.rows for table in tables}
    with record.timed("deploy_artifacts", scale=scale):
        olf_command(target, ["deploy", "--phase", "artifacts"], project_root=project, log_name=f"artifacts-x{scale}")
    facts: dict[str, dict[str, Any]] = {}
    with session(project) as cfg:
        for run in runs:
            for product in cfg.inventory.products:
                with record.timed(f"{run}_{product.id}", scale=scale, product=product.id) as detail:
                    with dagster_client(cfg) as client:
                        _pipeline.run_job(client, product.job_name, detail, timeout_seconds=target.job_timeout_seconds)
                    for layer in detail.get("layers", {}).values():
                        if layer.get("end") is not None:
                            layer["cluster_usage"] = sampler.window(layer["start"], layer["end"])
            with record.timed(f"{run}_tables", scale=scale) as detail:
                detail["silver"] = _silver_facts(cfg, expected)
                detail["gold"] = _gold_facts(cfg, [product.gold_namespace for product in cfg.inventory.products])
                _require_rows(detail["gold"], "Gold")
                facts[run] = {**detail["silver"], **detail["gold"]}
    return facts


def _silver_facts(cfg: E2EConfig, expected: dict[tuple[str, str], int]) -> dict[str, Any]:
    """Every Floe entity's Silver table, checked against the rows the generator wrote."""
    catalog = stage_catalog_name(cfg)
    facts = {}
    mismatches = []
    for domain in cfg.inventory.domains:
        for entity in cfg.inventory.floe_entities(domain):
            table = _pipeline.table_facts(_query(cfg), catalog, domain.silver_namespace, entity.name)
            table["expected_rows"] = expected.get((entity.source, entity.resource))
            facts[f"{domain.silver_namespace}.{entity.name}"] = table
            if table["rows"] != table["expected_rows"]:
                mismatches.append(
                    f"{domain.silver_namespace}.{entity.name}: {table['rows']} != {table['expected_rows']}"
                )
    if mismatches:
        raise BenchError("Silver rows differ from the generated input: " + "; ".join(mismatches))
    return facts


def _gold_facts(cfg: E2EConfig, schemas: Sequence[str]) -> dict[str, Any]:
    catalog = stage_catalog_name(cfg)
    facts = {}
    for schema in schemas:
        listed = trino_query(cfg, f"SHOW TABLES IN {catalog}.{schema}")
        for table in sorted(line.strip() for line in listed.splitlines() if line.strip()):
            facts[f"{schema}.{table}"] = _pipeline.table_facts(_query(cfg), catalog, schema, table)
    return facts


def _require_rows(tables: dict[str, Any], label: str) -> None:
    empty = sorted(name for name, table in tables.items() if table["rows"] <= 0)
    if not tables or empty:
        raise BenchError(f"{label} tables without rows: {', '.join(empty) or 'none listed'}")


def _query(cfg: E2EConfig) -> _pipeline.Query:
    return lambda sql: trino_query(cfg, sql)


def _first_query(cfg: E2EConfig) -> int:
    deadline = time.monotonic() + FIRST_QUERY_TIMEOUT_SECONDS
    attempts = 0
    while True:
        attempts += 1
        try:
            trino_query(cfg, "SELECT 1")
            return attempts
        except E2EError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _footprint(cfg: E2EConfig, nodes: list[str]) -> dict[str, Any]:
    pods = kubectl(cfg, ["get", "pods", "-A", "-o", "json"], capture=True)
    return {
        "pods": _resources.pod_census(pods),
        "storage": _resources.node_storage(nodes),
        "usage_now": vars(_resources.node_usage(nodes)),
    }
