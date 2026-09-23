"""`olf project deploy` resolves every contract environment for its target stage.

The revision store is opened under a contract environment that stays in
`os.environ` underneath the one activation applies. Opened for the default
stage, it left DEV's defaulted OpenMetadata catalog database behind, and PROD's
governance seeding refused to run (#208's nightly, run 35876524353).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.commands import project as project_cmd
from olf.deployment import activation as activation_module
from olf.deployment import contract_env

runner = CliRunner()


def test_deploy_opens_the_revision_store_under_the_target_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "openlakeforge.yaml").write_text("placeholder: resolved by the stubbed context\n")
    stages: list[str] = []

    def context_for(profile_file: str, *, stage: str = "", **_kwargs: object) -> SimpleNamespace:
        stages.append(stage)
        return SimpleNamespace(
            namespace=f"olf-{stage}",
            kube_context="kind-test",
            topology=None,
            stage=stage,
            paths=SimpleNamespace(
                platform_terraform_dir=tmp_path, kubeconfig_path=tmp_path, port_forward_log_prefix=tmp_path
            ),
        )

    @contextmanager
    def no_op(**_kwargs: object):  # noqa: ANN202
        yield {}

    @contextmanager
    def store(**_kwargs: object):  # noqa: ANN202
        yield object()

    monkeypatch.setattr(project_cmd, "deployment_context_for_profile", context_for)
    monkeypatch.setattr(project_cmd, "_profile_provider", lambda *_a, **_k: SimpleNamespace(env={}))
    monkeypatch.setattr(project_cmd, "_revision_store", store)
    monkeypatch.setattr(contract_env, "applied_contract_environment", no_op)
    monkeypatch.setattr("olf.profile.load_deployment_profile", lambda _path: SimpleNamespace(name="test"))
    monkeypatch.setattr(
        activation_module, "deploy_revision", lambda *_a, **_k: SimpleNamespace(activation_revision="sha256:x")
    )

    result = runner.invoke(
        app,
        ["project", "deploy", "-f", str(tmp_path / "openlakeforge.yaml"), "--stage", "prod", "--revision", "r"],
    )

    assert result.exit_code == 0, result.output
    assert stages and set(stages) == {"prod"}
