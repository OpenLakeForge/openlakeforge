"""Silver's Floe outcomes against what Gold really does (#345).

The rule is documented in docs/architecture/floe-validation.md: a `warn` or `reject` check result leaves the
Silver step successful, so Gold runs, and only a Floe run that ends `failed` or `aborted` fails the step and
skips Gold. Silver here is `build_silver_definitions()`, the asset product_dagster ships, built from the project's
real Floe config: the only fixture is what the Floe runner returns. The real asset therefore decides whether the
step fails and which checks it emits, so a dagster-floe upgrade that changed either fails here, not in a cluster.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("dagster")
pytest.importorskip("floe_dagster")

from dagster import AssetKey, asset, materialize  # noqa: E402
from floe_dagster.runner import Runner, RunResult  # noqa: E402
from openlakeforge_core.quality import PipelineOutcome, QualityOutcome  # noqa: E402
from openlakeforge_runtime import product_dagster  # noqa: E402
from openlakeforge_runtime.floe_profile import write_profile  # noqa: E402
from openlakeforge_runtime.floe_quality import (  # noqa: E402
    FloeCheckEvidence,
    FloeEntityEvidence,
    floe_quality_manifest,
)

SILVER = AssetKey(["sales", "orders"])
ENTITY = "sales.orders"

_STAGE_ENV = {
    "OPENLAKEFORGE_STORAGE_REGION": "us-east-1",
    "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "bronze",
    "OPENLAKEFORGE_STORAGE_SILVER_BUCKET": "silver",
    "OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://ops/activations/dev",
    "OPENLAKEFORGE_CATALOG_REST_URI": "http://polaris:8181/api/catalog",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse",
    "OPENLAKEFORGE_CATALOG_TOKEN_URI": "http://polaris:8181/api/catalog/v1/oauth/tokens",
    "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME": "floe-credentials",
    "OPENLAKEFORGE_PROJECT_CODE_IMAGE": "project-code:test",
    "NAMESPACE": "dev",
}


@dataclass(frozen=True)
class _FloeRun:
    """What one `floe run` leaves behind: its `run_finished` event, run summary and entity report."""

    run_status: str
    exit_code: int
    entity_status: str
    report: dict[str, Any]


class _FixtureRunner(Runner):
    """The runner interface dagster-floe calls for a `job_scope: domain` asset, in place of the Kubernetes Job."""

    def __init__(self, outcome: _FloeRun, directory: Path) -> None:
        self._outcome = outcome
        self._directory = directory
        self.entities: list[str] = []

    # dagster-floe 0.6.1 reaches this through `Runner.stream_floe_entities`, which passes its arguments by position.
    def run_floe_entities(
        self, config_uri: str, run_id: str | None, domain: str, entities: list[str], *_: Any, **__: Any
    ) -> RunResult:
        self.entities = entities
        outcome = self._outcome
        report = self._directory / "orders.json"
        report.write_text(json.dumps(outcome.report))
        summary = self._directory / "run.summary.json"
        summary.write_text(json.dumps({"entities": [{"name": ENTITY, "status": outcome.entity_status}]}))
        event = {
            "event": "run_finished",
            "run_id": run_id or "floe-1",
            "status": outcome.run_status,
            "exit_code": outcome.exit_code,
            "summary_uri": str(summary),
            "entity_report_uris": {ENTITY: str(report)},
        }
        return RunResult(stdout=json.dumps(event) + "\n", stderr="", exit_code=outcome.exit_code)


def _report(rule_severity: str, violations: int = 3) -> dict[str, Any]:
    rules = [{"rule": "not_null", "severity": rule_severity, "violations": violations}]
    return {
        "policy": {"severity": rule_severity},
        "files": [{"status": "rejected" if rule_severity == "reject" else "warning", "validation": {"rules": rules}}],
    }


def _rate_report(rate: float, limit: float) -> dict[str, Any]:
    """A `reject` entity with `policy.max_rejection_rate`: floe-python 0.10.2 adds the observed rate and the verdict."""
    report = _report("reject")
    report["policy"] |= {
        "max_rejection_rate": limit,
        "rejection_rate": rate,
        "max_rejection_rate_exceeded": rate > limit,
    }
    return report


_CLEAN = {"policy": {"severity": "reject"}, "files": [{"status": "success"}]}

_REJECTED = _FloeRun("success", 0, "rejected", _report("reject"))
_WARNED = _FloeRun("success", 0, "success_with_warnings", _report("warn"))
_CLEAN_RUN = _FloeRun("success", 0, "success", _CLEAN)
_ABORTED = _FloeRun("aborted", 1, "aborted", _report("abort"))
_FAILED = _FloeRun("failed", 1, "failed", _report("abort"))
# Over its limit Floe ends the entity `aborted` with exit code 2 while the severity stays `reject`.
_OVER_RATE = _FloeRun("aborted", 2, "aborted", _rate_report(0.3, 0.1))
_UNDER_RATE = _FloeRun("success", 0, "rejected", _rate_report(0.02, 0.1))


@pytest.fixture(autouse=True)
def _stage_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(product_dagster, "_floe_profile", lambda: str(write_profile(_STAGE_ENV, tmp_path)))


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: _FloeRun) -> tuple[Any, list[str], list[str]]:
    """Silver then Gold in one run. Returns the run, the steps Gold ran in, and the entities Floe was asked for."""
    runner = _FixtureRunner(outcome, tmp_path)
    monkeypatch.setattr(product_dagster, "build_runner_from_env", lambda: runner)
    ran: list[str] = []

    @asset(deps=[SILVER])
    def gold() -> None:
        ran.append("gold")

    silver = product_dagster.build_silver_definitions().assets
    result = materialize([*silver, gold], selection=[SILVER, AssetKey("gold")], raise_on_error=False)
    return result, ran, runner.entities


def _records(result: Any, outcome: _FloeRun):
    evidence = FloeEntityEvidence(
        "orders",
        tuple(FloeCheckEvidence.from_asset_check(e) for e in result.get_asset_check_evaluations()),
        outcome.report,
    )
    manifest = floe_quality_manifest(
        stage="dev",
        domain="sales",
        product="order_revenue",
        dagster_run_id="r1",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        entities=[evidence],
    )
    return {r.check_id: r for r in manifest.results}


def _silver_check_specs() -> list[Any]:
    assets = product_dagster.build_silver_definitions().assets
    return [spec for a in assets for spec in a.check_specs if spec.asset_key == SILVER]


def test_floes_five_checks_never_block_downstream_assets() -> None:
    specs = _silver_check_specs()

    assert [spec.name for spec in specs] == [
        "floe_cast_error",
        "floe_not_null",
        "floe_unique",
        "floe_schema_mismatch",
        "floe_file_status",
    ]
    assert not any(spec.blocking for spec in specs)


def test_rejected_rows_quarantine_and_gold_still_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, ran, entities = _run(monkeypatch, tmp_path, _REJECTED)

    assert result.success and entities == [ENTITY] and ran == ["gold"]
    record = _records(result, _REJECTED)["floe_not_null"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.QUARANTINED)


def test_a_warning_degrades_quality_and_gold_still_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, ran, entities = _run(monkeypatch, tmp_path, _WARNED)

    assert result.success and entities == [ENTITY] and ran == ["gold"]
    record = _records(result, _WARNED)["floe_not_null"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.WARN, PipelineOutcome.CONTINUED)


@pytest.mark.parametrize("outcome", [_ABORTED, _FAILED], ids=["aborted", "failed"])
def test_an_aborted_floe_run_fails_silver_and_gold_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: _FloeRun
) -> None:
    result, ran, entities = _run(monkeypatch, tmp_path, outcome)

    assert not result.success and entities == [ENTITY] and ran == []
    record = _records(result, outcome)["floe_not_null"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.BLOCKED)


def test_a_clean_run_passes_every_check_and_gold_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, ran, entities = _run(monkeypatch, tmp_path, _CLEAN_RUN)

    assert result.success and entities == [ENTITY] and ran == ["gold"]
    assert {r.outcome for r in _records(result, _CLEAN_RUN).values()} == {QualityOutcome.PASSED}


def test_a_rejection_rate_over_its_limit_fails_silver_and_gold_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, ran, entities = _run(monkeypatch, tmp_path, _OVER_RATE)

    assert not result.success and entities == [ENTITY] and ran == []
    record = _records(result, _OVER_RATE)["floe_file_status"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.BLOCKED)
    assert (record.metrics["max_rejection_rate"], record.metrics["max_rejection_rate_exceeded"]) == (0.1, 1)


def test_a_rejection_rate_under_its_limit_quarantines_and_gold_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, ran, entities = _run(monkeypatch, tmp_path, _UNDER_RATE)

    assert result.success and entities == [ENTITY] and ran == ["gold"]
    record = _records(result, _UNDER_RATE)["floe_not_null"]
    assert (record.outcome, record.pipeline_outcome) == (QualityOutcome.ERROR, PipelineOutcome.QUARANTINED)
    assert (record.metrics["rejection_rate"], record.metrics["max_rejection_rate_exceeded"]) == (0.02, 0)
