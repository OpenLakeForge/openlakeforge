from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from olf.common.deployment.context import DeploymentContext, Profile
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.tooling.toolkit import Toolkit
from olf.infra import contracts as contracts_module
from olf.infra.deployment.local import artifacts

_PIN = "ghcr.io/openlakeforge/openlakeforge/project-code:1.0.0@sha256:" + "a" * 64


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path, profile=profile)
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit() -> Toolkit:
    return Toolkit.default(
        overrides={t: Path(t) for t in ("terraform", "docker", "kind", "kubectl", "helm")}
    )


def test_applied_contract_environment_yields_the_local_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda terraform_dir: None)
    monkeypatch.setattr(
        contracts_module,
        "build_contract_env",
        lambda base, contracts_value, *, repo_root, **_: (
            {"OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev"},
            ["SOME_STALE_VAR"],
        ),
    )
    monkeypatch.setenv("SOME_STALE_VAR", "old-value")
    monkeypatch.delenv("OPENLAKEFORGE_CATALOG_NAME", raising=False)

    with artifacts.applied_contract_environment(config) as env:
        assert env["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"
        assert "SOME_STALE_VAR" not in env
        assert env["NAMESPACE"] == "olf-dev"

    assert "OPENLAKEFORGE_CATALOG_NAME" not in os.environ
    assert os.environ["SOME_STALE_VAR"] == "old-value"


def test_applied_contract_environment_honors_contract_root_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    override = tmp_path / "custom-contracts"
    captured: dict[str, Path] = {}
    monkeypatch.setenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(override))
    monkeypatch.setattr(
        artifacts.contract_env,
        "applied_contract_environment",
        lambda **kwargs: captured.update(kwargs) or _empty_contract_environment(),
    )

    with artifacts.applied_contract_environment(config):
        pass

    assert captured["contract_terraform_dir"] == override



def _fake_provider(repo_root: Path, *, image: str, pushes: list[bool], **extra: Any) -> Any:
    from olf.common.profile import Provider

    def build_project_image(*, push: bool) -> str:
        pushes.append(push)
        return image

    topology = SimpleNamespace(profile_name="legacy", provider=Provider.LOCAL, project=None)
    return SimpleNamespace(
        context=SimpleNamespace(topology=topology, paths=SimpleNamespace(repo_root=repo_root)),
        env={},
        build_project_image=build_project_image,
        **extra,
    )


def _stub_activation(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Everything below `deploy_revision` needs a cluster; what it is handed is the behaviour under test."""
    from olf.infra.deployment import activation, revision_build

    monkeypatch.setattr(
        revision_build,
        "publish_project_revision",
        lambda project, *, image, open_store: SimpleNamespace(revision="sha256:" + "b" * 64, project_code_image=image),
    )
    monkeypatch.setattr(revision_build, "provider_revision_store", lambda *a, **k: nullcontext("store"))
    activated: list[dict[str, Any]] = []
    monkeypatch.setattr(activation, "deploy_revision", lambda provider, **kwargs: activated.append(kwargs))
    return activated


@pytest.mark.parametrize(
    ("repository", "image", "pushes"),
    [
        ("ghcr.io/openlakeforge/openlakeforge/project-code", _PIN, False),
        ("ghcr.io/acme/project-code", "ghcr.io/acme/project-code@sha256:" + "d" * 64, True),
    ],
    ids=["example-image", "customer-project"],
)
def test_artifacts_deploy_activates_a_revision_instead_of_patching_dagster(
    monkeypatch: pytest.MonkeyPatch, external_project: Path, repository: str, image: str, pushes: bool
) -> None:
    activated = _stub_activation(monkeypatch)
    pushed: list[bool] = []
    provider = _fake_provider(
        external_project,
        image=image,
        pushes=pushed,
        config=SimpleNamespace(images=SimpleNamespace(project_code_repository=repository)),
    )

    artifacts.artifacts_deploy(provider)

    assert [call["revision"] for call in activated] == ["sha256:" + "b" * 64]
    assert activated[0]["profile_name"] == "legacy"
    # Without a repository of its own the build stays on this cluster: pushing would overwrite the public example.
    assert pushed == [pushes]


def _fake_contextmanager(on_enter):  # noqa: ANN001, ANN202
    from contextlib import contextmanager

    @contextmanager
    def _cm(config, *, environ=None):  # noqa: ANN001, ARG001
        on_enter()
        yield dict(os.environ)

    return _cm


def _empty_contract_environment():  # noqa: ANN202
    from contextlib import nullcontext

    return nullcontext({})
