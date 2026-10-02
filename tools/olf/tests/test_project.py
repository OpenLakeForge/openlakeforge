from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.commands.checks import _project_code
from olf.project import ProjectSpec, validate_project

runner = CliRunner()


def test_project_spec_resolves_canonical_paths_for_a_split_root(tmp_path: Path) -> None:
    project = ProjectSpec(root=tmp_path / "project", distribution_root=tmp_path / "distribution")

    assert project.profile_path == tmp_path / "project/openlakeforge.yaml"
    assert project.code_root == tmp_path / "project/lakehouse_code"
    assert project.lakehouse_path == tmp_path / "project/lakehouse_code/lakehouse.yaml"
    assert project.bronze_root == tmp_path / "project/lakehouse_code/bronze"
    assert project.silver_root == tmp_path / "project/lakehouse_code/silver"
    assert project.gold_root == tmp_path / "project/lakehouse_code/gold"
    assert project.dashboards_root == tmp_path / "project/lakehouse_code/dashboards/superset"
    assert project.pipelines_root == tmp_path / "project/lakehouse_code/pipelines/dagster"
    assert project.schema_root == tmp_path / "distribution/docs/schema"


def test_project_spec_keeps_project_paths_under_a_shared_root(tmp_path: Path) -> None:
    project = ProjectSpec(root=tmp_path, distribution_root=tmp_path)

    assert project.root == project.distribution_root
    assert project.code_root == tmp_path / "lakehouse_code"


def test_external_project_contains_no_distribution_assets(external_project: Path) -> None:
    assert (external_project / "openlakeforge.yaml").is_file()
    assert (external_project / "lakehouse_code").is_dir()
    for path in (".git", "infra", "images", "libs", "packages", "infra/helm", "infra/terraform"):
        assert not (external_project / path).exists()


def test_project_validation_reports_a_valid_external_project(external_project: Path) -> None:
    distribution = Path(__file__).resolve().parents[3]

    report = validate_project(ProjectSpec(root=external_project, distribution_root=distribution))

    assert report.ok
    assert json.loads(report.render_json()) == {
        "schema_version": 1,
        "valid": True,
        "project_root": str(external_project),
        "distribution_root": str(distribution),
        "checks": [
            {"name": "canonical_layout", "ok": True, "detail": "canonical project paths are present"},
            {"name": "profile", "ok": True, "detail": "deployment profile validated"},
            {"name": "descriptors", "ok": True, "detail": "descriptors validated"},
            {"name": "inventory", "ok": True, "detail": "3 product(s) discovered"},
            {"name": "project_assets", "ok": True, "detail": "declared project assets are present"},
        ],
    }


def test_project_validation_reports_missing_profile_without_non_json_output(external_project: Path) -> None:
    (external_project / "openlakeforge.yaml").unlink()
    distribution = Path(__file__).resolve().parents[3]

    result = runner.invoke(app, ["project", "validate", "--project", str(external_project)])

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["valid"] is False
    assert report["checks"][0] == {
        "name": "canonical_layout",
        "ok": False,
        "detail": "missing: openlakeforge.yaml",
    }
    assert report["distribution_root"] == str(distribution)


def test_project_validation_reports_missing_plural_path(external_project: Path) -> None:
    dashboards = external_project / "lakehouse_code/dashboards"
    shutil.rmtree(dashboards)
    distribution = Path(__file__).resolve().parents[3]

    report = validate_project(ProjectSpec(root=external_project, distribution_root=distribution))

    assert not report.ok
    assert report.checks[0].detail == "missing: lakehouse_code/dashboards/superset/"


def test_project_validation_reports_missing_declared_asset(external_project: Path) -> None:
    (external_project / "lakehouse_code/pipelines/dagster/order_revenue.py").unlink()
    distribution = Path(__file__).resolve().parents[3]

    report = validate_project(ProjectSpec(root=external_project, distribution_root=distribution))

    assert not report.ok
    assert report.checks[-1].name == "project_assets"
    assert "lakehouse_code/pipelines/dagster/order_revenue.py" in report.checks[-1].detail


def test_project_validation_reports_invalid_descriptors(external_project: Path) -> None:
    lakehouse = external_project / "lakehouse_code/lakehouse.yaml"
    lakehouse.write_text("apiVersion: invalid\n", encoding="utf-8")
    distribution = Path(__file__).resolve().parents[3]

    report = validate_project(ProjectSpec(root=external_project, distribution_root=distribution))

    assert not report.ok
    assert report.checks[2].name == "descriptors"
    assert report.checks[2].ok is False
    assert report.checks[3].name == "inventory"
    assert report.checks[3].ok is False


def test_project_code_check_uses_external_code_and_distribution_dependencies(
    external_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _VersionInfo(tuple):
        @property
        def major(self) -> int:
            return self[0]

        @property
        def minor(self) -> int:
            return self[1]

    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    monkeypatch.setattr(_project_code.sys, "version_info", _VersionInfo((3, 12, 0, "final", 0)))
    monkeypatch.setattr(_project_code, "_uv_pip_install", lambda **_kwargs: None)
    monkeypatch.setattr(
        _project_code,
        "_run",
        lambda argv, *, cwd, env=None: calls.append((argv, cwd, env or {})),
    )

    _project_code.project_code(str(external_project))

    distribution = Path(__file__).resolve().parents[3]
    argv, cwd, env = calls[-1]
    assert argv[-1] == str(external_project)
    assert cwd == external_project
    assert env["PYTHONPATH"].split(os.pathsep)[-2:] == [str(external_project), str(distribution)]
    assert not (external_project / ".cache").exists()
