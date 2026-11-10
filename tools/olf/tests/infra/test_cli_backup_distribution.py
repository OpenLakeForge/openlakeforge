"""`olf infra backup` from an installed olf: `release/` lives in the distribution payload, not the project (#412).

The restore drill runs from a customer project outside any checkout; the role model the contract parser
checks must come from the payload the context resolved, not from the working directory.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import CONFORMANCE_PROFILE, E2E_REPO_ROOT
from typer.testing import CliRunner

from olf.cli import app
from olf.common import contract_state
from olf.common.deployment.context import DeploymentContext
from olf.common.profile import StageName
from olf.infra.backup import _backup
from olf.infra.backup import _cluster as cluster_module
from olf.infra.commands import _shared
from olf.infra.commands import backup as backup_cmd

runner = CliRunner()
_CONTRACT = json.loads((Path(__file__).parents[1] / "fixtures" / "conformance-provider-contracts.json").read_text())


@pytest.fixture
def installed_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    payload = tmp_path / "distributions" / "0.6.0" / "abc123" / "payload"
    (payload / "release").mkdir(parents=True)
    shutil.copy(E2E_REPO_ROOT / "release/identity-roles.yaml", payload / "release/identity-roles.yaml")
    project = tmp_path / "customer-project"
    project.mkdir()
    shutil.copy(CONFORMANCE_PROFILE, project / "openlakeforge.yaml")
    monkeypatch.chdir(project)
    for name in ("OLF_DISTRIBUTION_ROOT", "OPENLAKEFORGE_REPO_ROOT", "OPENLAKEFORGE_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLF_BACKUP_PASSPHRASE", "correct horse battery")
    return payload, project


def _run_backup(monkeypatch: pytest.MonkeyPatch, payload: Path, project: Path, output: Path) -> object:
    topology = _shared.deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value).topology
    context = DeploymentContext.local(
        repo_root=project, distribution_root=payload, topology=topology, work_root=project / "work"
    )
    seen: dict[str, object] = {}

    @contextmanager
    def runtime_environment(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield SimpleNamespace()

    @contextmanager
    def operations(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield object()

    def run_backup(_output: Path, **kwargs: object) -> SimpleNamespace:
        seen["plan"] = kwargs["plan"]
        return SimpleNamespace(databases=[], buckets=[], secrets=[])

    monkeypatch.setattr(backup_cmd, "deployment_context_for_profile", lambda *_a, **_k: context)
    monkeypatch.setattr(backup_cmd, "applied_runtime_environment", runtime_environment)
    monkeypatch.setattr(contract_state, "load_provider_contracts", lambda *_a, **_k: _CONTRACT)
    monkeypatch.setattr(cluster_module, "cluster_operations", operations)
    monkeypatch.setattr(_backup, "run_backup", run_backup)
    result = runner.invoke(app, ["infra", "backup", "--output", str(output), "-f", str(project / "openlakeforge.yaml")])
    return result, seen


def test_backup_reads_the_role_model_from_the_distribution_payload(
    monkeypatch: pytest.MonkeyPatch, installed_layout: tuple[Path, Path], tmp_path: Path
) -> None:
    payload, project = installed_layout
    assert not (project / "release").exists()

    result, seen = _run_backup(monkeypatch, payload, project, tmp_path / "backup")  # type: ignore[misc]

    assert result.exit_code == 0, result.output
    assert "plan" in seen


def test_backup_names_the_payload_path_when_its_role_model_is_missing(
    monkeypatch: pytest.MonkeyPatch, installed_layout: tuple[Path, Path], tmp_path: Path
) -> None:
    payload, project = installed_layout
    (payload / "release/identity-roles.yaml").unlink()

    result, _ = _run_backup(monkeypatch, payload, project, tmp_path / "backup")  # type: ignore[misc]

    assert result.exit_code != 0
    assert str(payload / "release/identity-roles.yaml") in result.output
