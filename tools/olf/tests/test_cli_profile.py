from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from olf.cli import app

runner = CliRunner()

_VALID_PROFILE = """\
apiVersion: openlakeforge.io/v1alpha1
kind: DeploymentProfile

metadata:
  name: fixture-slim

spec:
  provider:
    type: local
  preset: slim
  stages:
    dev:
      enabled: true
"""

_INVALID_PROFILE = """\
apiVersion: openlakeforge.io/v1alpha1
kind: DeploymentProfile

metadata:
  name: fixture

spec:
  provider:
    type: local
  preset: slim
  stages:
    dev:
      enabled: false
"""


def test_profile_validate_succeeds_for_a_valid_profile(tmp_path: Path) -> None:
    (tmp_path / "openlakeforge.yaml").write_text(_VALID_PROFILE, encoding="utf-8")

    result = runner.invoke(app, ["profile", "validate", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "valid" in result.output.lower()


def test_profile_validate_fails_closed_for_an_invalid_profile(tmp_path: Path) -> None:
    (tmp_path / "openlakeforge.yaml").write_text(_INVALID_PROFILE, encoding="utf-8")

    result = runner.invoke(app, ["profile", "validate", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "at least one stage must be enabled" in result.output


def test_profile_validate_fails_when_the_profile_is_missing(tmp_path: Path) -> None:
    result = runner.invoke(app, ["profile", "validate", "--project", str(tmp_path)])

    assert result.exit_code == 1


def test_profile_resolve_json_is_machine_readable_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "openlakeforge.yaml").write_text(_VALID_PROFILE, encoding="utf-8")

    first = runner.invoke(app, ["profile", "resolve", "--project", str(tmp_path), "--json"])
    second = runner.invoke(app, ["profile", "resolve", "--project", str(tmp_path), "--json"])

    assert first.exit_code == 0
    assert first.output == second.output

    payload = json.loads(first.output)
    assert payload["schema_version"] == 1
    assert payload["provider"] == "local"
    assert payload["preset"] == "slim"
    stages = {stage["name"]: stage for stage in payload["stages"]}
    assert stages["dev"]["enabled"] is True
    assert stages["prod"]["enabled"] is False


def test_profile_resolve_text_output_lists_every_stage(tmp_path: Path) -> None:
    (tmp_path / "openlakeforge.yaml").write_text(_VALID_PROFILE, encoding="utf-8")

    result = runner.invoke(app, ["profile", "resolve", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "dev: enabled=True" in result.output
    assert "prod: enabled=False" in result.output


def test_profile_resolve_fails_closed_for_an_invalid_profile(tmp_path: Path) -> None:
    (tmp_path / "openlakeforge.yaml").write_text(_INVALID_PROFILE, encoding="utf-8")

    result = runner.invoke(app, ["profile", "resolve", "--project", str(tmp_path), "--json"])

    assert result.exit_code == 1


def test_profile_resolve_reports_where_the_project_image_comes_from(tmp_path: Path) -> None:
    (tmp_path / "openlakeforge.yaml").write_text(
        _VALID_PROFILE + "  project:\n    image:\n      repository: ghcr.io/acme/code\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["profile", "resolve", "--project", str(tmp_path), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["project"] == {
        "source": "profile",
        "repository": None,
        "image_repository": "ghcr.io/acme/code",
        "pull_secret_name": None,
    }


def test_profile_resolve_rejects_a_malformed_image_override(tmp_path: Path) -> None:
    (tmp_path / "openlakeforge.yaml").write_text(_VALID_PROFILE, encoding="utf-8")

    result = runner.invoke(
        app, ["profile", "resolve", "--project", str(tmp_path)], env={"PROJECT_CODE_IMAGE_REPOSITORY": "acme/code:1"}
    )

    assert result.exit_code == 1
    assert "PROJECT_CODE_IMAGE_REPOSITORY" in result.output
