from __future__ import annotations

from pathlib import Path

import pytest
import typer

from olf import dashboard_checks
from olf.commands.checks import _infra, _project_code, _shared, _structure


def test_dashboard_validation_accepts_the_repository_assets() -> None:
    root = Path(__file__).resolve().parents[3]

    assert dashboard_checks.validate_superset_assets(root) == []


def test_dashboard_validation_rejects_unknown_dataset_column(tmp_path: Path) -> None:
    dataset = tmp_path / "lakehouse_code/dashboards/superset/example/datasets/trino/orders.yaml"
    chart = tmp_path / "lakehouse_code/dashboards/superset/example/charts/orders.yaml"
    dataset.parent.mkdir(parents=True)
    chart.parent.mkdir(parents=True)
    dataset.write_text("uuid: orders\ncolumns:\n  - column_name: order_date\nmetrics:\n  - metric_name: sum__sales\n")
    chart.write_text(
        "viz_type: echarts_timeseries_line\ndataset_uuid: orders\nparams:\n"
        "  x_axis: missing_column\n  metrics:\n    - sum__sales\n"
    )

    errors = dashboard_checks.validate_superset_assets(tmp_path)

    assert any("missing_column" in error for error in errors)


def test_superset_render_requires_ephemeral_reports_volume_and_mount() -> None:
    rendered = """\
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      volumes:
        - name: superset-reports
          emptyDir:
            sizeLimit: 1Gi
      containers:
        - name: superset
          volumeMounts:
            - name: superset-reports
              mountPath: /app/openlakeforge/reports
"""

    _infra._validate_superset_render(rendered)


def test_superset_render_rejects_reports_pvc() -> None:
    rendered = """\
kind: PersistentVolumeClaim
metadata:
  name: superset-reports
"""

    with pytest.raises(typer.Exit):
        _infra._validate_superset_render(rendered)


def test_project_code_cache_digest_includes_source_paths_and_contents(tmp_path: Path) -> None:
    source = tmp_path / "domain" / "model.py"
    source.parent.mkdir()
    source.write_text("value = 1\n")
    first = _project_code._source_tree_digest(source.parent)
    source.write_text("value = 2\n")

    assert _project_code._source_tree_digest(source.parent) != first


def test_project_code_rejects_unsupported_python_before_building_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_project_code.sys, "version_info", (3, 13, 0, "final", 0))

    with pytest.raises(typer.Exit):
        _project_code.project_code(str(tmp_path))


def test_structure_registry_preserves_the_full_non_script_skeleton() -> None:
    required = {
        "CLAUDE.md",
        ".github/workflows/checks.yml",
        "docs/architecture/overview.md",
        "docs/architecture/provider-contracts.md",
        "docs/schema/provider-contracts.schema.json",
        "docs/adr/0008-olf-owns-orchestration-and-toolchain.md",
        "infra/terraform/foundations/aws-eks/outputs.tf",
        "lakehouse_code/silver/sales/contracts/floe/sales.yml",
        "tools/olf/uv.lock",
        "packages/domain-model/openlakeforge_domain/inventory.py",
    }

    assert required <= set(_structure.REQUIRED_PATHS)
    assert not any(path.startswith("scripts/") or path.endswith(".sh") for path in _structure.REQUIRED_PATHS)


def test_missing_required_paths_reports_removed_skeleton_entries(tmp_path: Path) -> None:
    for path in _structure.REQUIRED_PATHS:
        candidate = tmp_path / path
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("required\n")
    removed = {"CLAUDE.md", ".github/workflows/checks.yml", "tools/olf/uv.lock"}
    for path in removed:
        (tmp_path / path).unlink()

    assert set(_structure._missing_required_paths(tmp_path)) == removed


def test_distribution_root_for_prefers_a_complete_separate_checkout(tmp_path: Path) -> None:
    other_checkout = tmp_path / "other-checkout"
    (other_checkout / "infra" / "terraform").mkdir(parents=True)

    assert _shared._distribution_root_for(other_checkout) == other_checkout


def test_distribution_root_for_falls_back_to_the_runtime_payload_for_a_project_only_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "installed-project"
    (project_root / "lakehouse_code").mkdir(parents=True)

    assert _shared._distribution_root_for(project_root) != project_root


def test_release_publication_guard_requires_the_olf_workflow_command(tmp_path: Path) -> None:
    workflow = tmp_path / ".github/workflows/release.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
jobs:
  prepare:
    steps:
      - name: Require a green main commit before publishing
        if: steps.mode.outputs.dry_run == 'false'
        env:
          GH_TOKEN: ${{ github.token }}
        run: >-
          uv run --project tools/olf --locked olf release workflow require-green-main
          --repo "${{ github.repository }}" --sha "${{ github.sha }}"
"""
    )

    assert _structure._release_publication_guard_errors(tmp_path) == []


def test_release_publication_guard_rejects_an_unchecked_shell_step(tmp_path: Path) -> None:
    workflow = tmp_path / ".github/workflows/release.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
jobs:
  prepare:
    steps:
      - name: Require a green main commit before publishing
        if: steps.mode.outputs.dry_run == 'false'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api repos/example/compare/head...main
"""
    )

    assert _structure._release_publication_guard_errors(tmp_path) == [
        "release workflow green-main guard must delegate repository and SHA validation to olf"
    ]
