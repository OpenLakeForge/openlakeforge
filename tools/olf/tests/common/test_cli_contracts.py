from __future__ import annotations

import pytest
from typer.testing import CliRunner

from olf.cli import app

runner = CliRunner()


def test_contracts_env_surfaces_a_toolchain_failure_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """`olf contracts env` calls load_provider_contracts() outside any
    DeploymentError boundary of its own - a managed-toolchain provisioning
    failure (#127) resolving terraform must still surface the same clean
    CLI failure every other olf command produces, not a raw traceback."""
    from olf.common.deployment.errors import ToolchainError

    def _raise(terraform_dir: str):  # noqa: ANN202
        raise ToolchainError("terraform", reason="digest mismatch")

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", _raise)

    result = runner.invoke(app, ["infra", "contracts", "env"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, ToolchainError)
    assert "digest mismatch" in result.output
