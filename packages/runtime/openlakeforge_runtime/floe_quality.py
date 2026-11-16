"""Maps the Floe check results of a run to `quality_result` records (#1124).

dagster-floe already turns each entity's `run.json` into five Dagster asset checks and decides which are
failed and which are warnings. This module reads those results, and the entity report only for its policy,
so no Floe rule is applied twice. What it adds is what Dagster cannot say: a failed `reject` check
quarantined rows and the run carried on, while an `abort` stopped it.

It lives in the runtime because the runtime owns the report location (`floe_profile` renders it from
`OPENLAKEFORGE_ARTIFACT_BASE_URI`) and the S3 reader for it, and runs in the pod that holds the check results.
It imports neither Dagster nor Floe, so it takes the check results as plain evidence.

Only counts, the policy word and the report's URI are copied. Floe's `sample_messages`, `top_columns`,
`missing_columns` and `extra_columns` metadata can carry values or schema detail and are never read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from openlakeforge_core.quality import (
    PipelineOutcome,
    QualityManifest,
    QualityOutcome,
    QualityResult,
)

from openlakeforge_runtime.s3_artifacts import read_json_uri

ENGINE = "floe"

# dagster_floe.asset_checks.FLOE_CHECK_NAMES, pinned with floe-python 0.10.1 / dagster-floe 0.6.0.
FLOE_CHECK_IDS = ("floe_cast_error", "floe_not_null", "floe_unique", "floe_schema_mismatch", "floe_file_status")

# Statuses Floe gives a run whose pipeline stopped; the check builders treat both as a failed check.
_TERMINAL_RUN_STATUSES = frozenset({"failed", "aborted"})
_POLICIES = frozenset({"warn", "reject", "abort"})
_UNKNOWN_SEVERITY = "unknown"

# Check metadata key -> metric name. A fixed list, so nothing Floe adds to the metadata reaches a record.
_METRICS = {
    "violations_total": "violations_total",
    "reject_violations": "reject_violations",
    "warn_violations": "warn_violations",
    "files_impacted": "files_impacted",
    "rejected_files": "rejected_files",
    "failed_files": "failed_files",
    "warn_files": "warn_files",
    "files": "files_total",
    "rows": "rows_total",
    "accepted": "accepted_rows",
    "rejected": "rejected_rows",
    "warnings": "warnings_total",
    "errors": "errors_total",
}


@dataclass(frozen=True)
class FloeCheckEvidence:
    """One Floe asset check result as dagster-floe produced it."""

    check_name: str
    passed: bool
    severity: str
    metadata: Mapping[str, Any]

    @classmethod
    def from_asset_check(cls, result: Any) -> FloeCheckEvidence:
        """From a Dagster `AssetCheckResult` or `AssetCheckEvaluation`, whose severity is an enum."""
        severity = getattr(result.severity, "name", result.severity)
        return cls(
            check_name=str(result.check_name),
            passed=bool(result.passed),
            severity=str(severity).upper(),
            metadata=result.metadata or {},
        )


@dataclass(frozen=True)
class FloeEntityEvidence:
    """Everything one Floe entity of a run reported. `report` is its `run.json`, None when it could not be read."""

    entity: str
    checks: tuple[FloeCheckEvidence, ...] = ()
    report: Mapping[str, Any] | None = None


def read_entity_report(
    uri: str | None, *, read: Callable[[str], Mapping[str, Any]] = read_json_uri
) -> Mapping[str, Any] | None:
    """The entity report, or None. A missing report is evidence of a partial run, not a reason to fail the run."""
    if not uri:
        return None
    try:
        return read(uri)
    except Exception:
        return None


def floe_quality_manifest(
    *,
    stage: str,
    domain: str,
    product: str,
    dagster_run_id: str,
    observed_at: datetime,
    entities: Iterable[FloeEntityEvidence],
) -> QualityManifest:
    """All five checks of every entity. Stage and run come from the caller, never from the evidence, so
    evidence of another stage or run cannot be filed under this one."""
    results: list[QualityResult] = []
    for entity in entities:
        for check_id, check in _evidence_by_check(entity):
            results.append(
                _result(
                    entity,
                    check_id,
                    check,
                    stage=stage,
                    domain=domain,
                    product=product,
                    dagster_run_id=dagster_run_id,
                    observed_at=observed_at,
                )
            )
    return QualityManifest(
        stage=stage, domain=domain, product=product, dagster_run_id=dagster_run_id, results=tuple(results)
    )


def _evidence_by_check(entity: FloeEntityEvidence) -> list[tuple[str, FloeCheckEvidence | None]]:
    present = [(c.check_name, c) for c in entity.checks if c.check_name in FLOE_CHECK_IDS]
    seen = {name for name, _ in present}
    return [*present, *((name, None) for name in FLOE_CHECK_IDS if name not in seen)]


def _result(
    entity: FloeEntityEvidence,
    check_id: str,
    check: FloeCheckEvidence | None,
    *,
    stage: str,
    domain: str,
    product: str,
    dagster_run_id: str,
    observed_at: datetime,
) -> QualityResult:
    policy = _policy(entity.report)
    meta = _plain(check.metadata) if check else {}
    if check is None:
        # No check results at all means we cannot tell whether Floe ran; some means this one was deselected.
        outcome = QualityOutcome.NOT_RUN if entity.checks else QualityOutcome.UNKNOWN
        pipeline = PipelineOutcome.UNKNOWN
    else:
        outcome, pipeline = _classify(check, meta, policy, evidenced=_has_evidence(entity.report, meta))
    return QualityResult(
        engine=ENGINE,
        stage=stage,
        domain=domain,
        product=product,
        table=entity.entity,
        check_id=check_id,
        engine_severity=policy or _UNKNOWN_SEVERITY,
        outcome=outcome,
        pipeline_outcome=pipeline,
        observed_at=observed_at,
        dagster_run_id=dagster_run_id,
        engine_run_id=_text(meta.get("run_id")) or _entity_run_id(entity),
        artifact_uri=_safe_uri(_text(meta.get("entity_report_uri"))),
        metrics=_metrics(meta),
    )


def _classify(
    check: FloeCheckEvidence, meta: Mapping[str, Any], policy: str | None, *, evidenced: bool
) -> tuple[QualityOutcome, PipelineOutcome]:
    if check.passed:
        # dagster-floe passes every check when it fell back to the run summary, which proves nothing about rows.
        if evidenced:
            return QualityOutcome.PASSED, PipelineOutcome.CONTINUED
        return QualityOutcome.UNKNOWN, PipelineOutcome.UNKNOWN
    if (_text(meta.get("floe_run_status")) or "").lower() in _TERMINAL_RUN_STATUSES or policy == "abort":
        return QualityOutcome.ERROR, PipelineOutcome.BLOCKED
    if check.severity == "WARN":
        return QualityOutcome.WARN, PipelineOutcome.CONTINUED
    if policy == "reject" or _rejected(meta):
        return QualityOutcome.ERROR, PipelineOutcome.QUARANTINED
    return QualityOutcome.ERROR, PipelineOutcome.UNKNOWN


def _has_evidence(report: Mapping[str, Any] | None, meta: Mapping[str, Any]) -> bool:
    files = report.get("files") if report else None
    return bool(files) and isinstance(files, list) and meta.get("fallback") != "summary_only"


def _rejected(meta: Mapping[str, Any]) -> bool:
    counts = meta.get("file_status_counts")
    return (
        any(_number(meta.get(key)) for key in ("reject_violations", "rejected_files"))
        or isinstance(counts, Mapping)
        and bool(_number(counts.get("rejected")))
    )


def _policy(report: Mapping[str, Any] | None) -> str | None:
    policy = report.get("policy") if report else None
    severity = policy.get("severity") if isinstance(policy, Mapping) else None
    # Checked against the three words Floe defines, so a report cannot put anything else in a record.
    return severity if severity in _POLICIES else None


def _entity_run_id(entity: FloeEntityEvidence) -> str | None:
    return next((r for c in entity.checks if (r := _text(_plain(c.metadata).get("run_id")))), None)


def _metrics(meta: Mapping[str, Any]) -> dict[str, int | float]:
    metrics = {name: n for key, name in _METRICS.items() if (n := _number(meta.get(key))) is not None}
    counts = meta.get("file_status_counts")
    if isinstance(counts, Mapping):
        metrics.update({f"files_{status}": n for status, count in counts.items() if (n := _number(count)) is not None})
    return metrics


def _plain(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Dagster wraps check metadata in `MetadataValue`s whose `.value` is the plain one."""
    return {key: getattr(value, "value", value) for key, value in metadata.items()}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_uri(uri: str | None) -> str | None:
    # A record refuses a presigned or credentialed pointer; a report URI with one is left out, not fatal.
    if uri is None:
        return None
    parts = urlsplit(uri)
    return None if parts.query or parts.fragment or "@" in parts.netloc else uri
