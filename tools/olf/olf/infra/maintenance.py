"""Run an Iceberg maintenance job through Dagster and normalise its per-table results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from olf.infra.e2e._dagster import dagster_client
from olf.infra.e2e._maintenance import GOLD_JOB, GOLD_OP, SILVER_JOB, SILVER_OP, run_job
from olf.infra.e2e._shell import E2EConfig

LAYERS = ("silver", "gold")
_JOBS = {"silver": (SILVER_JOB, SILVER_OP), "gold": (GOLD_JOB, GOLD_OP)}


@dataclass(frozen=True)
class TableResult:
    """One table's outcome. `None` means the job does not record that figure, never zero.

    On a dry run `snapshots_expired` counts the candidates and `snapshots_after` equals
    `snapshots_before`. Gold records no snapshot counts and no orphan files, and its dry run
    counts only the snapshots past the window, an upper bound (Trino cannot list orphan files).
    """

    table: str
    snapshots_before: int | None
    snapshots_after: int | None
    snapshots_expired: int | None
    orphan_files: int | None
    orphan_bytes: int | None
    error: str | None


@dataclass(frozen=True)
class MaintenanceReport:
    layer: str
    stage: str
    job: str
    run_id: str
    dry_run: bool
    tables: list[TableResult]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_maintenance(cfg: E2EConfig, *, layer: str, stage: str, dry_run: bool) -> MaintenanceReport:
    """Launch the layer's job, wait for it, and report. A failed run raises `E2EError`."""
    job, op = _JOBS[layer]
    with dagster_client(cfg) as client:
        run_id, metadata = run_job(cfg, client, job, op, dry_run)
    return MaintenanceReport(layer, stage, job, run_id, dry_run, table_results(layer, metadata, dry_run=dry_run))


def table_results(layer: str, metadata: dict[str, Any], *, dry_run: bool) -> list[TableResult]:
    if layer == "silver":
        return [_silver(result, dry_run) for result in metadata.get("tables") or []]
    return [_gold(result, dry_run) for result in metadata.get("results") or []]


def _silver(result: dict[str, Any], dry_run: bool) -> TableResult:
    expired, kept = result["expired_snapshots"], result["kept_snapshots"]
    return TableResult(
        table=result["table"],
        snapshots_before=expired + kept,
        snapshots_after=expired + kept if dry_run else kept,
        snapshots_expired=expired,
        orphan_files=len(result.get("orphan_files") or []),
        orphan_bytes=result.get("orphan_bytes", 0),
        error=None,
    )


def _gold(result: dict[str, Any], dry_run: bool) -> TableResult:
    return TableResult(
        table=f"{result['schema']}.{result['table']}",
        snapshots_before=None,
        snapshots_after=None,
        snapshots_expired=result.get("snapshots_past_window") if dry_run else None,
        orphan_files=None,
        orphan_bytes=None,
        error=result.get("error"),
    )


def render(report: MaintenanceReport) -> str:
    mode = "dry run, nothing deleted" if report.dry_run else "applied"
    header = ("TABLE", "BEFORE", "AFTER", "EXPIRED", "ORPHANS", "ERROR")
    rows = [
        (
            t.table,
            _cell(t.snapshots_before),
            _cell(t.snapshots_after),
            _cell(t.snapshots_expired),
            _cell(t.orphan_files),
            t.error or "",
        )
        for t in report.tables
    ]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = [f"{report.job} on {report.stage} ({mode}), Dagster run {report.run_id}"]
    for row in [header, *rows]:
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)


def _cell(value: int | None) -> str:
    return "-" if value is None else str(value)
