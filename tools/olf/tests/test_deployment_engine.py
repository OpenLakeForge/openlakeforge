from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from olf.deployment.context import DeploymentContext, Provider
from olf.deployment.engine import DeploymentEngine, DeploymentPhase, Toolkit, build_provider
from olf.deployment.errors import DeploymentPreconditionError, UnsupportedProviderError
from olf.tooling.aws import AwsCli
from olf.tooling.azure import AzureCli

ROOT = Path(__file__).resolve().parents[3]


def _runnable_project(tmp_path: Path) -> Path:
    """A throwaway project root carrying the real, strictly valid lakehouse_code."""
    shutil.copytree(ROOT / "lakehouse_code", tmp_path / "lakehouse_code")
    return tmp_path


class _FakeProvider:
    def __init__(self, repo_root: Path) -> None:
        self.calls: list[str] = []
        self.context = DeploymentContext.local(repo_root=repo_root)

    def foundation_up(self) -> None:
        self.calls.append("foundation_up")

    def foundation_down(self, *, force: bool = False) -> None:
        self.calls.append(f"foundation_down(force={force})")

    def prepare_images(self) -> None:
        self.calls.append("prepare_images")

    def platform_up(self) -> None:
        self.calls.append("platform_up")

    def platform_down(self) -> None:
        self.calls.append("platform_down")

    def artifacts_deploy(self) -> None:
        self.calls.append("artifacts_deploy")

    def status(self) -> str:
        self.calls.append("status")
        return "report"

    def forward(self) -> None:
        self.calls.append("forward")


def test_deploy_all_runs_phases_in_adr_0008_order(tmp_path: Path) -> None:
    provider = _FakeProvider(_runnable_project(tmp_path))
    engine = DeploymentEngine(provider)

    engine.deploy()

    assert provider.calls == ["foundation_up", "prepare_images", "platform_up", "artifacts_deploy"]


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (DeploymentPhase.FOUNDATION, ["foundation_up"]),
        (DeploymentPhase.PREFETCH, ["prepare_images"]),
        (DeploymentPhase.PLATFORM, ["platform_up"]),
        (DeploymentPhase.ARTIFACTS, ["artifacts_deploy"]),
    ],
)
def test_deploy_single_phase_runs_only_that_step(
    phase: DeploymentPhase, expected: list[str], tmp_path: Path
) -> None:
    provider = _FakeProvider(_runnable_project(tmp_path))
    engine = DeploymentEngine(provider)

    engine.deploy(phase)

    assert provider.calls == expected


def test_deploy_refuses_artifacts_for_a_project_without_a_runnable_descriptor(tmp_path: Path) -> None:
    provider = _FakeProvider(tmp_path)

    with pytest.raises(DeploymentPreconditionError, match="project is not runnable"):
        DeploymentEngine(provider).deploy(DeploymentPhase.ARTIFACTS)

    assert provider.calls == []


def test_deploy_all_refuses_before_the_first_phase_mutates_anything(tmp_path: Path) -> None:
    provider = _FakeProvider(tmp_path)

    with pytest.raises(DeploymentPreconditionError, match="project is not runnable"):
        DeploymentEngine(provider).deploy()

    assert provider.calls == []


@pytest.mark.parametrize(
    "phase", [DeploymentPhase.FOUNDATION, DeploymentPhase.PREFETCH, DeploymentPhase.PLATFORM]
)
def test_static_phases_stay_descriptor_free_under_adr_0008(phase: DeploymentPhase, tmp_path: Path) -> None:
    provider = _FakeProvider(tmp_path)

    DeploymentEngine(provider).deploy(phase)

    assert provider.calls != []


def test_destroy_all_tears_down_platform_before_foundation(tmp_path: Path) -> None:
    provider = _FakeProvider(_runnable_project(tmp_path))
    engine = DeploymentEngine(provider)

    engine.destroy(force=True)

    assert provider.calls == ["platform_down", "foundation_down(force=True)"]


def test_destroy_platform_phase_leaves_foundation_running(tmp_path: Path) -> None:
    provider = _FakeProvider(_runnable_project(tmp_path))
    engine = DeploymentEngine(provider)

    engine.destroy(DeploymentPhase.PLATFORM)

    assert provider.calls == ["platform_down"]


def test_destroy_foundation_phase_only(tmp_path: Path) -> None:
    provider = _FakeProvider(_runnable_project(tmp_path))
    engine = DeploymentEngine(provider)

    engine.destroy(DeploymentPhase.FOUNDATION)

    assert provider.calls == ["foundation_down(force=False)"]


def test_status_and_forward_delegate_to_provider(tmp_path: Path) -> None:
    provider = _FakeProvider(_runnable_project(tmp_path))
    engine = DeploymentEngine(provider)

    assert engine.status() == "report"
    engine.forward()

    assert provider.calls == ["status", "forward"]


def test_build_provider_rejects_unsupported_providers(tmp_path) -> None:  # noqa: ANN001
    # Every real `Provider` enum member is supported now (local, aws, azure); this exercises
    # the guard clause itself, which protects against a future enum member missing a branch.
    context = replace(DeploymentContext.local(repo_root=tmp_path), provider="gcp")

    with pytest.raises(UnsupportedProviderError):
        build_provider(context)


def test_build_provider_returns_local_provider(tmp_path) -> None:  # noqa: ANN001
    context = DeploymentContext.local(repo_root=tmp_path)

    provider = build_provider(context, environ={})

    assert provider.context is context


def test_build_provider_returns_cloud_provider_for_aws_and_azure(tmp_path) -> None:  # noqa: ANN001
    for provider_enum in (Provider.AWS, Provider.AZURE):
        context = DeploymentContext.for_provider(provider_enum, repo_root=tmp_path)

        provider = build_provider(context, environ={})

        assert provider.context is context
        assert provider.backend.scope == provider_enum.value


def test_toolkit_default_includes_aws_and_azure_adapters() -> None:
    # aws/az are not managed tools (#127), so host mode is enough here and
    # keeps this test independent of the managed-toolchain resolution path,
    # which is covered by tests/test_toolchain_resolver.py.
    toolkit = Toolkit.default(environ={"OLF_TOOLCHAIN_MODE": "host"})

    assert isinstance(toolkit.aws, AwsCli)
    assert isinstance(toolkit.azure, AzureCli)
