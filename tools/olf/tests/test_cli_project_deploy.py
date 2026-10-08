"""`olf project deploy` activates a stage under that stage's contract environment.

The revision store is opened under a contract environment that stays in
`os.environ` underneath the one activation applies. Opened for the default
stage, it left DEV's defaulted OpenMetadata catalog database behind, and PROD's
governance seeding refused to run (#208's nightly, run 35876524353).

The contract environment itself is real here, resolved from the checked-in v3
contract; only the cluster, the store, and the activation body are stubbed, the
last replaced by the same nested stage apply `deploy_revision` performs.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cli_project_status import _contract, _topology
from typer.testing import CliRunner

from olf import contracts
from olf.cli import app
from olf.commands import project as project_cmd
from olf.deployment import activation as activation_module
from olf.deployment import contract_env
from olf.deployment.context import DeploymentContext

runner = CliRunner()


def test_prod_activation_sees_prods_openmetadata_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    contract = _contract()
    topology = _topology(contract)
    profile = tmp_path / "openlakeforge.yaml"
    profile.write_text("placeholder: resolved by the stubbed context\n")
    # The contract environment is derived from the project's own descriptors.
    (tmp_path / "lakehouse_code").symlink_to(Path(__file__).resolve().parents[3] / "lakehouse_code")
    # The contract parser reads the distribution's role model from the same root.
    (tmp_path / "release").symlink_to(Path(__file__).resolve().parents[3] / "release")
    monkeypatch.delenv("OPENMETADATA_CATALOG_DATABASE", raising=False)

    def context_for(_profile_file: str, *, stage: str = "", **_kwargs: object) -> DeploymentContext:
        return DeploymentContext.local(
            repo_root=tmp_path, topology=topology, stage=stage or "dev", work_root=tmp_path / "work"
        )

    @contextmanager
    def store(**_kwargs: object):  # noqa: ANN202
        yield object()

    seen: list[str] = []

    def deploy_revision(provider: object, **_kwargs: object) -> SimpleNamespace:
        with contract_env.applied_contract_environment(
            contract_terraform_dir=tmp_path,
            repo_root=tmp_path,
            namespace="olf-prod",
            kube_context="kind-test",
            kubeconfig_path=tmp_path,
            port_forward_log_prefix=tmp_path,
            environ=provider.env,  # type: ignore[attr-defined]
            topology=topology,
            stage="prod",
        ) as environ:
            seen.append(environ["OPENMETADATA_CATALOG_DATABASE"])
        return SimpleNamespace(activation_revision="sha256:x")

    monkeypatch.setattr(project_cmd, "deployment_context_for_profile", context_for)
    monkeypatch.setattr(project_cmd, "_profile_provider", lambda *_a, **_k: SimpleNamespace(env={}))
    monkeypatch.setattr(project_cmd, "_revision_store", store)
    monkeypatch.setattr(contracts, "load_provider_contracts", lambda *_a, **_k: contract)
    monkeypatch.setattr("olf.profile.load_deployment_profile", lambda _path: SimpleNamespace(name="test"))
    monkeypatch.setattr(activation_module, "deploy_revision", deploy_revision)

    result = runner.invoke(app, ["project", "deploy", "-f", str(profile), "--stage", "prod", "--revision", "r"])

    assert result.exit_code == 0, result.output
    assert seen == [contract["stages"]["prod"]["catalog"]["catalog_name"]]


@pytest.mark.parametrize("command", ["build", "deploy"])
@pytest.mark.parametrize("content", [None, "{not json"])
def test_an_unreadable_contract_file_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, content: str | None
) -> None:
    """A named contract file that is missing or malformed fails closed, but as
    a CLI error naming the variable rather than a ProviderContractError traceback."""
    topology = _topology(_contract())
    profile = tmp_path / "openlakeforge.yaml"
    profile.write_text("placeholder: resolved by the stubbed context\n")
    contract_file = tmp_path / "contract.json"
    if content is not None:
        contract_file.write_text(content)
    monkeypatch.setenv(contracts.PROVIDER_CONTRACTS_FILE_ENV, str(contract_file))

    def context_for(_profile_file: str, *, stage: str = "", **_kwargs: object) -> DeploymentContext:
        return DeploymentContext.local(
            repo_root=tmp_path, topology=topology, stage=stage or "dev", work_root=tmp_path / "work"
        )

    monkeypatch.setattr(project_cmd, "deployment_context_for_profile", context_for)
    monkeypatch.setattr(project_cmd, "_profile_provider", lambda *_a, **_k: SimpleNamespace(env={}))
    monkeypatch.setattr("olf.profile.load_deployment_profile", lambda _path: SimpleNamespace(name="test"))
    monkeypatch.setattr(
        "olf.project_revision.build_project_revision", lambda *_a, **_k: SimpleNamespace(revision="r")
    )
    monkeypatch.setattr(
        "olf.commands._project.writable_project_layout",
        lambda _project: SimpleNamespace(project_root=tmp_path, distribution_root=tmp_path, distribution_version="0"),
    )
    args = (
        ["project", "deploy", "-f", str(profile), "--stage", "dev", "--revision", "r"]
        if command == "deploy"
        else ["project", "build", "--project", str(tmp_path), "--image", "img"]
    )

    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert contracts.PROVIDER_CONTRACTS_FILE_ENV in result.output
