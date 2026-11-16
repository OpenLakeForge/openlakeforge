"""Maps the dbt test results of a run to `quality_result` records (#1125).

A run's `run_results.json` names each test only by `unique_id`; the table, column and severity live in the
`manifest.json` of the same run, so the two are joined here and never against another run's.
The product job writes the quality manifest to the ops bucket (`product_dagster._publish_quality_manifest`).

The artifacts are the ones `product_dagster` uploads to `run-artifacts/dbt/<domain>/<product>/<run>/`, built for
dbt-core 1.11.12 (run-results schema v6, manifest schema v12) with dagster-dbt 0.29.7. `merge_run_results` has
folded every generation's results into the one file, while `manifest.json` is the last invocation's, which holds
the whole project. Data tests (generic and singular) are mapped; unit tests are not.

Selected but not run: the manifest lists every test of the project, so it cannot say which ones this run
selected. The caller passes `selected_tests`, the unique ids `dbt_build.dbt_tests` marks `explicit` less those
`without_deselected_checks` drops, because that is the set `generation_args` hands to dbt. The product job writes
that set to `selected_tests.json` before the build starts, so a build that fails early still leaves it, and
`read_dbt_evidence` reads it when the caller passes none. A selected test with
no result is `not_run` when other tests have results, and `unknown` when none has or the build failed: then we
cannot tell whether it ran. Without `selected_tests`, a test with no result produces no record rather than a guess.

A test with status `fail` or `error` blocks the pipeline. `engine_run_id` is left empty: the merged file holds one
invocation's metadata, not that of the generation a test ran in.

Only `failures` and the execution time are copied. `message` can quote row values and is never read.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping
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

ENGINE = "dbt"

MANIFEST_PATH = "target/manifest.json"
RUN_RESULTS_PATH = "target/run_results.json"
INCOMPLETE_MARKER_PATH = "artifacts_incomplete.json"
SELECTED_TESTS_PATH = "selected_tests.json"

_UNKNOWN_SEVERITY = "unknown"


@dataclass(frozen=True)
class DbtRunEvidence:
    """The dbt artifacts of one Dagster run in one stage, which must all come from that run."""

    manifest: Mapping[str, Any]
    # None when the build wrote no run_results.json.
    run_results: Mapping[str, Any] | None = None
    # `artifacts_incomplete.json`: present only when the dbt build raised.
    incomplete_marker: Mapping[str, Any] | None = None
    selected_tests: frozenset[str] | None = None
    # Where run_results.json is, as the record's pointer to its evidence.
    artifact_uri: str | None = None
    # False when the manifest is the image's stage-neutral one: its schemas lack the run's catalog prefix, so
    # it names tests but not the run's tables.
    table_identity_known: bool = True


def read_dbt_evidence(
    prefix_uri: str | None,
    *,
    selected_tests: Collection[str] | None = None,
    read: Callable[[str], Mapping[str, Any]] = read_json_uri,
) -> DbtRunEvidence | None:
    """The run's artifacts under `prefix_uri`, or None when they cannot tell what dbt did.

    Without the manifest nothing can be named. Without run_results.json and without the marker, a failed
    upload and a build that never ran look alike, so that is None too; with the marker it is evidence of failure.
    """
    if not prefix_uri:
        return None
    base = prefix_uri.rstrip("/")
    manifest = _read(read, f"{base}/{MANIFEST_PATH}")
    run_results = _read(read, f"{base}/{RUN_RESULTS_PATH}")
    marker = _read(read, f"{base}/{INCOMPLETE_MARKER_PATH}")
    if manifest is None or (run_results is None and marker is None):
        return None
    if selected_tests is None:
        selected_tests = _selected_ids(_read(read, f"{base}/{SELECTED_TESTS_PATH}"))
    return DbtRunEvidence(
        manifest=manifest,
        run_results=run_results,
        incomplete_marker=marker,
        selected_tests=None if selected_tests is None else frozenset(selected_tests),
        artifact_uri=_safe_uri(f"{base}/{RUN_RESULTS_PATH}") if run_results is not None else None,
        table_identity_known=not (marker or {}).get("baked_manifest"),
    )


def dbt_quality_manifest(
    *,
    stage: str,
    domain: str,
    product: str,
    dagster_run_id: str,
    observed_at: datetime,
    evidence: DbtRunEvidence,
) -> QualityManifest:
    """Stage and run come from the caller, never from the artifacts, so one run's evidence cannot be filed
    under another."""
    run_results = evidence.run_results or {}
    results_by_id = {
        r["unique_id"]: r
        for r in run_results.get("results", [])
        if isinstance(r, Mapping) and isinstance(r.get("unique_id"), str)
    }
    build_failed = evidence.incomplete_marker is not None
    records: list[QualityResult] = []
    for unique_id, test in _tests(evidence.manifest).items():
        result = results_by_id.get(unique_id)
        if result is None:
            if evidence.selected_tests is None or unique_id not in evidence.selected_tests:
                continue
            outcome, pipeline = _missing(has_results=bool(results_by_id), build_failed=build_failed)
        else:
            outcome, pipeline = _classify(result.get("status"), test)
        table = _table(evidence.manifest, test) if evidence.table_identity_known else None
        records.append(
            QualityResult(
                engine=ENGINE,
                stage=stage,
                domain=domain,
                product=product,
                table=table,
                column=_text(test.get("column_name")) if table else None,
                check_id=test["name"],
                engine_severity=_severity(test) or _UNKNOWN_SEVERITY,
                outcome=outcome,
                pipeline_outcome=pipeline,
                observed_at=observed_at,
                dagster_run_id=dagster_run_id,
                # merge_run_results folds every generation's results into the last invocation's file and keeps
                # only that invocation's metadata, so its id would be wrong for tests of earlier generations.
                engine_run_id=None,
                artifact_uri=evidence.artifact_uri,
                metrics=_metrics(result or {}),
            )
        )
    return QualityManifest(
        stage=stage, domain=domain, product=product, dagster_run_id=dagster_run_id, results=tuple(records)
    )


def _classify(status: Any, test: Mapping[str, Any]) -> tuple[QualityOutcome, PipelineOutcome]:
    if status == "pass":
        return QualityOutcome.PASSED, PipelineOutcome.CONTINUED
    if status == "warn":
        return QualityOutcome.WARN, PipelineOutcome.CONTINUED
    if status == "fail":
        # dagster-dbt 0.29.7 reports a failed test as an ERROR-severity check, and dbt's non-zero exit makes
        # invocation.stream() raise DagsterDbtCliRuntimeError, so the step fails; build_by_generation also
        # skips the test's dependents. dbt reports `fail` only for severity error (severity warn reports
        # `warn`), so another severity here is a manifest that disagrees with its results: not claimed as a block.
        blocked = (_severity(test) or "").lower() == "error"
        return QualityOutcome.ERROR, PipelineOutcome.BLOCKED if blocked else PipelineOutcome.UNKNOWN
    if status == "error":
        # dbt_build.failed_nodes_in treats `error` like `fail`: build_by_generation blocks the test's dependents
        # and raises, and dbt exits non-zero. The marker is only written when an invocation produced no
        # run_results.json, so its absence says nothing about whether the build stopped.
        return QualityOutcome.ERROR, PipelineOutcome.BLOCKED
    if status == "skipped":
        return QualityOutcome.NOT_RUN, PipelineOutcome.UNKNOWN
    return QualityOutcome.UNKNOWN, PipelineOutcome.UNKNOWN


def _missing(*, has_results: bool, build_failed: bool) -> tuple[QualityOutcome, PipelineOutcome]:
    # The marker lists invocation ordinals, which skip generations that never started and so cannot be mapped
    # back to a test: every selected test without a result is unknown, not only those of a listed generation.
    if build_failed or not has_results:
        return QualityOutcome.UNKNOWN, PipelineOutcome.UNKNOWN
    return QualityOutcome.NOT_RUN, PipelineOutcome.UNKNOWN


def _tests(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    project = (manifest.get("metadata") or {}).get("project_name")
    return {
        unique_id: node
        for unique_id, node in (manifest.get("nodes") or {}).items()
        if node.get("resource_type") == "test" and node.get("package_name") == project and node.get("name")
    }


def _table(manifest: Mapping[str, Any], test: Mapping[str, Any]) -> str | None:
    """The test's attached node, else its only model or source. A multi-node test (relationships) is the
    attached model's, so the other nodes it reads are never consulted."""
    target = test.get("attached_node")
    if not target:
        parents = [n for n in (test.get("depends_on") or {}).get("nodes", []) if not n.startswith("macro.")]
        target = parents[0] if len(parents) == 1 else None
    node = (manifest.get("nodes") or {}).get(target) or (manifest.get("sources") or {}).get(target)
    if not node:
        return None
    name = node.get("alias") or node.get("identifier") or node.get("name")
    schema = node.get("schema")
    return f"{schema}.{name}" if schema and name else _text(name)


def _severity(test: Mapping[str, Any]) -> str | None:
    value = (test.get("config") or {}).get("severity")
    return value if isinstance(value, str) and value else None


def _metrics(result: Mapping[str, Any]) -> dict[str, int | float]:
    metrics: dict[str, int | float] = {}
    failures = result.get("failures")
    if isinstance(failures, int) and not isinstance(failures, bool):
        metrics["failures"] = failures
    seconds = result.get("execution_time")
    if isinstance(seconds, int | float) and not isinstance(seconds, bool) and math.isfinite(seconds):
        metrics["execution_time_seconds"] = seconds
    return metrics


def _read(read: Callable[[str], Mapping[str, Any]], uri: str) -> Mapping[str, Any] | None:
    try:
        return read(uri)
    except Exception:
        return None


def _selected_ids(document: Mapping[str, Any] | None) -> list[str] | None:
    ids = (document or {}).get("selected_tests")
    return ids if isinstance(ids, list) and all(isinstance(i, str) for i in ids) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_uri(uri: str) -> str | None:
    parts = urlsplit(uri)
    return None if parts.query or parts.fragment or "@" in parts.netloc else uri
