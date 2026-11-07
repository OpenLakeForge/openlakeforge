"""`olf infra restore` hands each stage back its restored revision instead of deploying the checkout (#930)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.common.profile import Provider, StageName
from olf.infra.backup import _restore as backup_restore
from olf.infra.commands import restore as restore_command
from olf.infra.deployment import revision_build
from olf.infra.deployment.engine import DeploymentPhase


class _Platform:
    """What the cluster holds: which phases ran, and which revision each stage runs (None: nothing)."""

    def __init__(self, pointers: dict[StageName, str | None]) -> None:
        self.pointers = pointers
        self.phases: list[DeploymentPhase] = []
        self.running: dict[StageName, str] = {}
        self.checkout_builds = 0


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> _Platform:
    stages = (StageName.DEV, StageName.PROD)
    state = _Platform({StageName.DEV: "sha256:restored-dev", StageName.PROD: None})

    def context(*_a: object, stage: str = "", **_k: object) -> SimpleNamespace:
        return SimpleNamespace(
            provider=Provider.LOCAL,
            stage=StageName(stage or "dev"),
            enabled_stages=stages,
            namespace_for=lambda s: f"olf-{s.value}",
            shared_namespace="olf-system",
            paths=SimpleNamespace(repo_root=Path("/project")),
        )

    class Engine:
        def __init__(self, ctx: SimpleNamespace) -> None:
            self.provider = SimpleNamespace(context=ctx)

        def deploy(self, phase: DeploymentPhase) -> None:
            state.phases.append(phase)
            if phase in (DeploymentPhase.ALL, DeploymentPhase.ARTIFACTS):
                state.checkout_builds += 1
                state.running.update({stage: "checkout" for stage in stages})

    def reactivate(provider: SimpleNamespace) -> object | None:
        stage = provider.context.stage
        if state.pointers[stage] is None:
            return None
        state.running[stage] = state.pointers[stage]
        return object()

    @contextmanager
    def runtime(_context: object, *, env: str):  # noqa: ANN202
        yield {}

    monkeypatch.setattr(restore_command, "_build_context", context)
    monkeypatch.setattr(restore_command, "_build_engine", lambda ctx, var_file: Engine(ctx))
    monkeypatch.setattr(restore_command, "applied_runtime_environment", runtime)
    monkeypatch.setattr(backup_restore, "restore", lambda *_a, **_k: None)
    monkeypatch.setattr(revision_build, "reactivate_active_revision", reactivate)
    monkeypatch.setenv("OLF_BACKUP_PASSPHRASE", "pw")
    return state


def test_each_stage_runs_its_restored_revision_and_the_checkout_is_not_deployed(platform: _Platform) -> None:
    result = CliRunner().invoke(app, ["infra", "restore", "--from", "/backup"])

    assert result.exit_code == 0, result.output
    assert platform.phases == [DeploymentPhase.PLATFORM]
    assert platform.checkout_builds == 0
    # dev has a pointer and runs it; prod has none and is left alone.
    assert platform.running == {StageName.DEV: "sha256:restored-dev"}
    assert "Stage prod has no active project revision" in result.output
