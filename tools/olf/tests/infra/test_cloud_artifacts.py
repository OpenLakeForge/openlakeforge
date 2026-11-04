from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _cloud_support import FakeCloudBackend

from olf.common.deployment.context import DeploymentContext
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.cloud import artifacts
from olf.infra.deployment.cloud.backend import FoundationFacts
from olf.infra.deployment.cloud.config import CloudDeploymentConfig

_FACTS = FoundationFacts(
    cluster_name="eks-openlakeforge-poc",
    kube_context="eks-openlakeforge-poc",
    project_code_repository="123.dkr.ecr.eu-west-1.amazonaws.com/project-code",
    aws_region="eu-west-1",
)


def _config(tmp_path: Path) -> CloudDeploymentConfig:
    context = DeploymentContext.aws(repo_root=tmp_path)
    return CloudDeploymentConfig.from_environment({}, context=context)


def _toolkit() -> Toolkit:
    return Toolkit.default(overrides={t: Path(t) for t in ("terraform", "docker", "kind", "kubectl", "helm")})


def _fake_contract_env(on_enter):  # noqa: ANN001, ANN202
    @contextmanager
    def _cm(**kwargs):  # noqa: ANN003
        on_enter()
        yield dict(os.environ)

    return _cm


def test_applied_authentication_environment_exposes_and_restores_olf_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLF_HOME", "/original")
    monkeypatch.setenv("AWS_PROFILE", "ambient")

    with artifacts._applied_authentication_environment(
        "aws", {"OLF_HOME": "/selected", "AWS_PROFILE": "olf-sso"}
    ):
        assert os.environ["OLF_HOME"] == "/selected"
        assert os.environ["AWS_PROFILE"] == "olf-sso"

    assert os.environ["OLF_HOME"] == "/original"
    assert os.environ["AWS_PROFILE"] == "ambient"



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
    from olf.infra import k8s
    from olf.infra.deployment import activation, revision_build

    monkeypatch.setattr(k8s, "set_project_code_image", lambda *a, **k: pytest.fail("patched Dagster directly"))
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
    "image",
    [
        "123.dkr.ecr.eu-west-1.amazonaws.com/project-code@sha256:" + "d" * 64,
        "ghcr.io/openlakeforge/openlakeforge/project-code:1.0.0@sha256:" + "a" * 64,
    ],
    ids=["customer-project", "example-image"],
)
def test_artifacts_deploy_activates_a_revision_instead_of_patching_dagster(
    monkeypatch: pytest.MonkeyPatch, external_project: Path, image: str
) -> None:
    activated = _stub_activation(monkeypatch)
    pushed: list[bool] = []
    provider = _fake_provider(
        external_project, image=image, pushes=pushed, backend=FakeCloudBackend(scope="aws", transport="direct")
    )

    artifacts.artifacts_deploy(provider)

    assert [call["revision"] for call in activated] == ["sha256:" + "b" * 64]
    # A cloud cluster can only pull from a registry.
    assert pushed == [True]
