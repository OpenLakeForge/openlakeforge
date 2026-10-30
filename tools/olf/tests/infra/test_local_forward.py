from __future__ import annotations

from pathlib import Path

from _tooling_support import RecordingRunner

from olf.common.deployment.context import DeploymentContext, Profile
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.local import forward


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path, profile=profile)
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit(dagster_pod_line: str) -> Toolkit:
    from olf.common.tooling.docker import Docker
    from olf.common.tooling.helm import Helm
    from olf.common.tooling.kind import Kind
    from olf.common.tooling.kubectl import Kubectl
    from olf.common.tooling.terraform import Terraform

    result = CommandResult(argv=(), returncode=0, stdout=dagster_pod_line, stderr="", duration_seconds=0.0)
    runner = RecordingRunner(result)
    resolver = PathExecutableResolver(
        overrides={t: Path(t) for t in ("terraform", "docker", "kind", "kubectl", "helm")}
    )
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
    )


def test_full_profile_includes_superset_and_openmetadata(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.FULL)
    tools = _toolkit("seaweedfs-master-0\ndagster-dagster-webserver-abc123\n")

    spec = forward.local_forward_spec(config, tools, env={})

    labels = [t.label for t in spec.targets]
    assert labels == [
        "seaweedfs-s3",
        "polaris",
        "trino",
        "dagster",
        "superset",
        "openmetadata",
        "seaweedfs-filer",
        "seaweedfs-master",
    ]
    dagster_target = next(t for t in spec.targets if t.label == "dagster")
    assert dagster_target.resource == "pod/dagster-dagster-webserver-abc123"
    assert dagster_target.ports == "3000:80"


def test_slim_profile_excludes_superset_and_openmetadata(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    tools = _toolkit("dagster-dagster-webserver-abc123\n")

    spec = forward.local_forward_spec(config, tools, env={})

    labels = [t.label for t in spec.targets]
    assert "superset" not in labels
    assert "openmetadata" not in labels
    assert not any("Superset UI" in line for line in spec.banner)
    assert not any("OpenMetadata UI" in line for line in spec.banner)


def test_missing_dagster_pod_is_skipped_not_fatal(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    tools = _toolkit("")

    spec = forward.local_forward_spec(config, tools, env={})

    assert "dagster" not in [t.label for t in spec.targets]
    assert not any("Dagster UI" in line for line in spec.banner)


def test_resolve_dagster_webserver_pod_picks_first_matching_line() -> None:
    tools = _toolkit("seaweedfs-master-0\ndagster-dagster-webserver-abc\ndagster-dagster-webserver-def\n")

    pod = forward.resolve_dagster_webserver_pod(
        tools,
        namespace="lakehouse",
        context="kind-openlakeforge-local",
        kubeconfig=Path("/repo/.tmp/kubeconfigs/local.yaml"),
        env={},
    )

    assert pod == "dagster-dagster-webserver-abc"


def test_shared_and_stage_targets_forward_from_their_own_namespaces(tmp_path: Path) -> None:
    """Shared services and stage services are in different namespaces, so one
    spec-wide namespace can no longer address every target."""
    config = _config(tmp_path)
    tools = _toolkit("dagster-dagster-webserver-abc\n")

    spec = forward.local_forward_spec(config, tools, env={})

    namespaces = {target.label: target.namespace or spec.namespace for target in spec.targets}
    assert namespaces["trino"] == "olf-system"
    assert namespaces["polaris"] == "olf-system"
    assert namespaces["seaweedfs-s3"] == "olf-system"
    assert namespaces["openmetadata"] == "olf-system"
    assert namespaces["superset"] == "olf-dev"
    assert namespaces["dagster"] == "olf-dev"
