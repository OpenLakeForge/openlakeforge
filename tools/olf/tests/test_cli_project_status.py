"""`olf project status` on a running stage.

The command had no coverage at all, which is how it came to call
`release_runs_activation` without an argument that had become required: the
break was a `TypeError` on the ordinary path, invisible because
`olf.commands.project` carries `ignore_errors` in the mypy overrides.

The collaborators that need a cluster, an object store, or a profile on disk
are stubbed. `release_runs_activation` deliberately is not -- it is the call
under test.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from olf import contracts
from olf.cli import app
from olf.commands import project as project_cmd
from olf.deployment.context import DeploymentContext
from olf.profile import StageName, resolve_topology, validate_deployment_profile
from olf.project_activation import ProjectActivation

FIXTURES = Path(__file__).parent / "fixtures"
_IMAGE = "ghcr.io/openlakeforge/project-code@sha256:" + "a" * 64
_DIGEST = "sha256:" + "b" * 64
_ACTIVATION_LABEL = "openlakeforge.io/activation-revision"
# The platform release rebinds these onto the user deployment, so the
# activation compares them and treats a difference as drift.
_GLOBALS = {"postgresqlSecretName": "postgresql-dagster-olf-dev-creds"}

runner = CliRunner()


def _contract() -> dict:
    return json.loads((FIXTURES / "local-provider-contracts-v3.json").read_text())


def _topology(contract: dict):  # noqa: ANN202
    return resolve_topology(
        validate_deployment_profile(
            {
                "apiVersion": "openlakeforge.io/v1alpha1",
                "kind": "DeploymentProfile",
                "metadata": {"name": contract["deployment"]["profile_name"]},
                "spec": {
                    "provider": {"type": "local"},
                    "preset": "full",
                    # Every stage the contract carries: the parser refuses a
                    # topology that does not match the contract's stage set.
                    "stages": {
                        name: {
                            "enabled": True,
                            "capabilities": {
                                "analytics": "reporting" in stage,
                                "governance": "governance" in stage,
                            },
                        }
                        for name, stage in contract["stages"].items()
                    },
                },
            }
        )
    )


@pytest.fixture
def stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """A DEV stage whose pointer and release agree, with the knobs to break either."""
    contract = _contract()
    topology = _topology(contract)
    locations = contract["stages"]["dev"]["orchestration"]["code_locations"]
    context = DeploymentContext.local(
        repo_root=tmp_path, topology=topology, stage="dev", work_root=tmp_path / "work"
    )
    activation = ProjectActivation(
        deployment_profile="acceptance",
        provider="local",
        stage=StageName.DEV,
        project_name="acme",
        project_revision=_DIGEST,
        distribution_version="0.3.0-alpha.1",
        project_code_image=_IMAGE,
        floe_manifest_revision=_DIGEST,
        provider_binding_digest=_DIGEST,
        capabilities={"analytics": False, "governance": False},
    ).resolved()

    state = SimpleNamespace(contract=contract, deployments=None)
    if state.deployments is None:
        state.deployments = [
            {
                "name": entry["name"],
                "deploymentLabels": {_ACTIVATION_LABEL: activation.activation_revision},
                "deploymentAnnotations": {"openlakeforge.io/floe-renderer": "image:ghcr.io/malon64/floe:0.6.11"},
            }
            for entry in locations
        ]

    class _Helm:
        def status(self, release, **kwargs):  # noqa: ANN001, ANN202, ARG002
            return SimpleNamespace(ok=True)

        def get_values(self, release, **kwargs):  # noqa: ANN001, ANN202, ARG002
            return SimpleNamespace(
                ok=True,
                stdout=json.dumps({"deployments": state.deployments, "global": _GLOBALS}),
            )

    provider = SimpleNamespace(
        context=context,
        env={"KUBE_CONTEXT": context.kube_context},
        tools=SimpleNamespace(helm=_Helm()),
        config=SimpleNamespace(floe=SimpleNamespace(image="ghcr.io/malon64/floe:0.6.11", runtime="image")),
    )

    monkeypatch.setattr(project_cmd, "deployment_context_for_profile", lambda *a, **k: context)
    monkeypatch.setattr(project_cmd, "_profile_provider", lambda *a, **k: provider)
    monkeypatch.setattr(project_cmd, "_artifact_transport", lambda *a, **k: object())
    monkeypatch.setattr(contracts, "load_provider_contracts", lambda *a, **k: state.contract)

    from olf import artifact_store, project_activation
    from olf.deployment import activation as activation_module
    from olf.deployment import contract_env

    @contextmanager
    def _env(**kwargs):  # noqa: ANN003, ANN202
        yield {"OPENLAKEFORGE_KUBE_NAMESPACE": kwargs["namespace"]}

    monkeypatch.setattr(contract_env, "applied_contract_environment", _env)
    monkeypatch.setattr(artifact_store, "artifact_bucket", lambda *a, **k: "ops")
    @contextmanager
    def _client(*a, **k):  # noqa: ANN002, ANN003, ANN202, ARG001
        yield object()

    monkeypatch.setattr(artifact_store, "artifact_storage_client", _client)
    monkeypatch.setattr(artifact_store, "S3RevisionStore", lambda *a, **k: object())
    monkeypatch.setattr(project_activation, "active", lambda *a, **k: activation)
    monkeypatch.setattr(
        activation_module, "read_platform_globals", lambda *a, **k: _GLOBALS
    )
    monkeypatch.setattr(activation_module, "_floe_renderer", lambda provider: "image:ghcr.io/malon64/floe:0.6.11")
    return SimpleNamespace(state=state, activation=activation, locations=locations)


def test_status_reports_a_stage_whose_release_matches_its_pointer(stage) -> None:  # noqa: ANN001
    result = runner.invoke(app, ["project", "status", "-f", "openlakeforge.yaml", "--stage", "dev", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)["stages"][0]
    assert report["stage"] == "dev"
    assert report["state"] == "active"


def test_status_reports_drift_when_a_contracted_location_is_missing(stage) -> None:  # noqa: ANN001
    """The release runs the recorded activation on the locations it still has.

    Reporting that as `active` would hide exactly the drift a redeploy exists
    to repair, so status has to compare against the contracted set rather than
    the release alone."""
    stage.state.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        *stage.locations,
        {"name": "acme-dagster", "definitions_module": "lakehouse_code.acme"},
    ]

    result = runner.invoke(app, ["project", "status", "-f", "openlakeforge.yaml", "--stage", "dev", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stages"][0]["state"] == "drifted"


def test_status_fails_cleanly_on_an_unreadable_contract(stage, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from olf.provider_contracts import ProviderContractError

    def _unreadable(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise ProviderContractError("OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE=/gone.json: No such file")

    monkeypatch.setattr(contracts, "load_provider_contracts", _unreadable)

    result = runner.invoke(app, ["project", "status", "-f", "openlakeforge.yaml", "--stage", "dev"])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "/gone.json" in result.output
