from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from _cloud_support import FakeCloudBackend
from _tooling_support import RecordingRunner

from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.cloud.images import (
    build_and_push_project_code_image,
    build_and_push_superset_image,
    resolve_effective_images,
)
from olf.deployment.context import DeploymentContext
from olf.deployment.engine import Toolkit
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver

_FACTS = FoundationFacts(
    cluster_name="eks-openlakeforge-poc",
    kube_context="eks-openlakeforge-poc",
    project_code_repository="123456789012.dkr.ecr.eu-west-1.amazonaws.com/openlakeforge/project-code",
    superset_repository="123456789012.dkr.ecr.eu-west-1.amazonaws.com/openlakeforge/superset",
    aws_region="eu-west-1",
)


def _config(tmp_path: Path, environ: dict | None = None) -> CloudDeploymentConfig:
    context = DeploymentContext.aws(repo_root=tmp_path)
    return CloudDeploymentConfig.from_environment(environ or {}, context=context)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.tooling.aws import AwsCli
    from olf.tooling.azure import AzureCli
    from olf.tooling.docker import Docker
    from olf.tooling.helm import Helm
    from olf.tooling.kind import Kind
    from olf.tooling.kubectl import Kubectl
    from olf.tooling.terraform import Terraform

    resolver = PathExecutableResolver(
        overrides={t: Path(t) for t in ("terraform", "docker", "kind", "kubectl", "helm", "aws", "az")}
    )
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
        aws=AwsCli(runner, resolver),
        azure=AzureCli(runner, resolver),
    )


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


@dataclass
class _RecordingBackend(FakeCloudBackend):
    login_repositories: list[str] = field(default_factory=list)

    def registry_login(self, tools, facts, *, repository, env):  # noqa: ANN001, ANN202, ARG002
        self.calls.append("registry_login")
        self.login_repositories.append(repository)


def test_resolve_effective_images_fills_empty_repositories_from_facts(tmp_path: Path) -> None:
    config = _config(tmp_path)

    images = resolve_effective_images(config.images, _FACTS)

    assert images.project_code_repository == _FACTS.project_code_repository
    assert images.superset_repository == _FACTS.superset_repository


def test_resolve_effective_images_keeps_explicit_override(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {
            "PROJECT_CODE_IMAGE_REPOSITORY": "999999999999.dkr.ecr.us-east-1.amazonaws.com/shared/project-code",
            "SUPERSET_IMAGE_REPOSITORY": "999999999999.dkr.ecr.us-east-1.amazonaws.com/shared/superset",
        },
    )

    images = resolve_effective_images(config.images, _FACTS)

    assert images.project_code_repository == "999999999999.dkr.ecr.us-east-1.amazonaws.com/shared/project-code"
    assert images.superset_repository == "999999999999.dkr.ecr.us-east-1.amazonaws.com/shared/superset"


def test_build_and_push_project_code_image_logs_in_to_the_overridden_repository(tmp_path: Path) -> None:
    overridden = "999999999999.dkr.ecr.us-east-1.amazonaws.com/shared/project-code"
    config = _config(tmp_path, {"PROJECT_CODE_IMAGE_REPOSITORY": overridden})
    tools = _toolkit(RecordingRunner(_ok()))
    backend = _RecordingBackend(scope="aws")

    build_and_push_project_code_image(config, tools, backend, _FACTS, env={})

    assert backend.login_repositories == [overridden]


def test_build_and_push_project_code_image_logs_in_to_the_foundation_default_when_unset(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = _toolkit(RecordingRunner(_ok()))
    backend = _RecordingBackend(scope="aws")

    build_and_push_project_code_image(config, tools, backend, _FACTS, env={})

    assert backend.login_repositories == [_FACTS.project_code_repository]


def test_build_and_push_superset_image_logs_in_to_the_overridden_repository(tmp_path: Path) -> None:
    overridden = "999999999999.dkr.ecr.us-east-1.amazonaws.com/shared/superset"
    config = _config(tmp_path, {"SUPERSET_IMAGE_REPOSITORY": overridden})
    tools = _toolkit(RecordingRunner(_ok()))
    backend = _RecordingBackend(scope="aws")

    build_and_push_superset_image(config, tools, backend, _FACTS, env={})

    assert backend.login_repositories == [overridden]
