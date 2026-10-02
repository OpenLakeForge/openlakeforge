"""`load_provider_contracts`'s managed-toolchain resolution behaviour (#127).

Split from `test_contracts.py`, which covers `build_contract_env`/
`render_shell_exports` against fixture contracts and never touches
executable resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olf.contracts import load_provider_contracts
from olf.deployment.errors import ExecutableNotFoundError, ToolchainError
from olf.provider_contracts import ProviderContractError


def test_missing_terraform_executable_is_treated_as_not_applied_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resolver:
        def resolve(self, tool: str):  # noqa: ANN001, ANN202
            raise ExecutableNotFoundError(tool, searched="PATH")

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda: _Resolver())

    assert load_provider_contracts("some/terraform/dir") is None


def test_a_real_toolchain_failure_propagates_instead_of_being_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A digest mismatch, broken download, or malformed catalog is a real
    operational failure, not an "unapplied" signal - a caller that treated
    it as unapplied would silently fall back to defaults (e.g. enabling
    governance/analytics for what should be a slim deployment) instead of
    failing closed."""

    class _Resolver:
        def resolve(self, tool: str):  # noqa: ANN001, ANN202
            raise ToolchainError(tool, reason="digest mismatch")

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda: _Resolver())

    with pytest.raises(ToolchainError):
        load_provider_contracts("some/terraform/dir")


def test_external_state_root_is_translated_into_terraform_state_and_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An installed distribution's state lives under OLF_HOME
    (OPENLAKEFORGE_TERRAFORM_STATE_ROOT/_DATA_ROOT), not next to
    terraform_dir - load_provider_contracts must pass `-state=<path>` and
    `TF_DATA_DIR` exactly as Terraform._run does, or it always reads an
    absent state and returns None even after a real apply."""
    import subprocess

    from olf.tooling.resolver import ExecutableResolver

    captured: dict = {}

    class _Resolver(ExecutableResolver):
        def resolve(self, tool: str):  # noqa: ANN001, ANN202
            return Path("/usr/local/bin/terraform")

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout='{"schema_version": "2.0.0"}', stderr="")

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda environ=None: _Resolver())
    monkeypatch.setattr("olf.contracts.subprocess.run", _fake_run)

    terraform_dir = tmp_path / "environments" / "local"
    environ = {
        "OPENLAKEFORGE_TERRAFORM_DATA_ROOT": str(tmp_path / "terraform-data"),
        "OPENLAKEFORGE_TERRAFORM_STATE_ROOT": str(tmp_path / "state"),
        "PATH": "/usr/bin",
    }

    result = load_provider_contracts(str(terraform_dir), environ=environ)

    assert result == {"schema_version": "2.0.0"}
    argv = captured["argv"]
    assert argv[0] == "/usr/local/bin/terraform"
    assert argv[1] == f"-chdir={terraform_dir}"
    assert "output" in argv
    expected_state = tmp_path / "state" / "platform.tfstate"
    assert f"-state={expected_state}" in argv
    assert not expected_state.parent.exists(), "read-only contract reads must not create state directories"
    assert captured["env"]["TF_DATA_DIR"] == str(tmp_path / "terraform-data" / "platform")


def test_a_native_v3_terraform_output_is_returned_for_a_stage_aware_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The loader hands back whatever version the root exports; selecting a
    stage is `build_contract_env`'s job. A v3 payload without a resolved
    topology still fails closed there rather than here."""
    import subprocess

    from olf.contracts import build_contract_env
    from olf.tooling.resolver import ExecutableResolver

    class _Resolver(ExecutableResolver):
        def resolve(self, tool: str):  # noqa: ANN001, ANN202
            return Path("/usr/local/bin/terraform")

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(argv, 0, stdout='{"schema_version": "3.0.0"}', stderr="")

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda environ=None: _Resolver())
    monkeypatch.setattr("olf.contracts.subprocess.run", _fake_run)

    terraform_dir = tmp_path / "environments" / "local"
    environ = {"PATH": "/usr/bin"}

    contracts = load_provider_contracts(str(terraform_dir), environ=environ)

    assert contracts == {"schema_version": "3.0.0"}
    with pytest.raises(ProviderContractError, match="requires a resolved DeploymentTopology"):
        build_contract_env({}, contracts, repo_root=Path(__file__).resolve().parents[3])


def test_without_external_state_root_behaviour_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import subprocess

    from olf.tooling.resolver import ExecutableResolver

    captured: dict = {}

    class _Resolver(ExecutableResolver):
        def resolve(self, tool: str):  # noqa: ANN001, ANN202
            return Path("/usr/local/bin/terraform")

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout='{"schema_version": "2.0.0"}', stderr="")

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda environ=None: _Resolver())
    monkeypatch.setattr("olf.contracts.subprocess.run", _fake_run)

    terraform_dir = tmp_path / "environments" / "local"
    environ = {"PATH": "/usr/bin"}

    result = load_provider_contracts(str(terraform_dir), environ=environ)

    assert result == {"schema_version": "2.0.0"}
    argv = captured["argv"]
    assert argv == [
        "/usr/local/bin/terraform",
        f"-chdir={terraform_dir}",
        "output",
        "-json",
        "provider_contracts",
    ]
    assert captured["env"] == environ


@pytest.mark.parametrize(
    ("stderr", "not_applied"),
    [
        ('Error: Output "provider_contracts" not found', True),
        ("Error: Unsupported state file format", False),
        ('Error: Backend initialization required, please run "terraform init"', False),
    ],
)
def test_strict_state_reports_only_a_missing_output_as_not_applied(
    monkeypatch: pytest.MonkeyPatch, stderr: str, not_applied: bool
) -> None:
    import subprocess

    from olf.tooling.resolver import ExecutableResolver

    class _Resolver(ExecutableResolver):
        def resolve(self, tool: str):  # noqa: ANN001, ANN202
            return Path("/usr/local/bin/terraform")

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
        raise subprocess.CalledProcessError(1, argv, output="", stderr=stderr)

    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda environ=None: _Resolver())
    monkeypatch.setattr("olf.contracts.subprocess.run", _fake_run)

    assert load_provider_contracts("platform", environ={}) is None
    if not_applied:
        assert load_provider_contracts("platform", environ={}, strict_state=True) is None
    else:
        with pytest.raises(ProviderContractError, match=stderr.split(": ", 1)[1].split(",")[0]):
            load_provider_contracts("platform", environ={}, strict_state=True)
