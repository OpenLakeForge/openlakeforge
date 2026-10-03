from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.cloud.aws import AwsBackend
from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.context import DeploymentContext, Profile, Provider
from olf.deployment.engine import Toolkit
from olf.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.profile import DeploymentProfile, Preset, ProviderSpec, StageName, StageSpec, resolve_topology
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm", "aws", "az")
_FACTS = FoundationFacts(
    cluster_name="eks-openlakeforge-poc",
    kube_context="eks-openlakeforge-poc",
    project_code_repository="123456789012.dkr.ecr.eu-west-1.amazonaws.com/openlakeforge/project-code",
    superset_repository="123456789012.dkr.ecr.eu-west-1.amazonaws.com/openlakeforge/superset",
    aws_region="eu-west-1",
)


class _Aws:
    def __init__(self, *, reachable: bool = True) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.reachable = reachable

    def eks_update_kubeconfig(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls.append(("kubeconfig", args, kwargs))

    def ecr_get_login_password(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls.append(("ecr", args, kwargs))
        return "ecr-password"

    def eks_describe_cluster(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls.append(("describe", args, kwargs))
        return CommandResult(argv=(), returncode=0 if self.reachable else 1, stdout="", stderr="", duration_seconds=0)


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> CloudDeploymentConfig:
    context = DeploymentContext.aws(repo_root=tmp_path, profile=profile)
    return CloudDeploymentConfig.from_environment({}, context=context)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.tooling.aws import AwsCli
    from olf.tooling.azure import AzureCli
    from olf.tooling.docker import Docker
    from olf.tooling.helm import Helm
    from olf.tooling.kind import Kind
    from olf.tooling.kubectl import Kubectl
    from olf.tooling.terraform import Terraform

    resolver = PathExecutableResolver(overrides={t: Path(t) for t in _TOOLS})
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


def _fail(stderr: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=1, stdout="", stderr=stderr, duration_seconds=0.0)


def test_scope_is_aws() -> None:
    assert AwsBackend().scope == "aws"


def test_foundation_state_resource_addr() -> None:
    assert AwsBackend().foundation_state_resource_addr() == "aws_eks_cluster.this"


def test_foundation_apply_and_destroy_variables_are_identical_and_wrap_bare_instance_type(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = AwsBackend()

    apply_vars = backend.foundation_apply_variables(config, {})
    destroy_vars = backend.foundation_destroy_variables(config, {})

    assert apply_vars == destroy_vars
    assert apply_vars["cluster_name"] == "limited-eks-openlakeforge-poc"
    assert apply_vars["aws_region"] == "eu-west-1"
    assert apply_vars["node_instance_types"] == '["m7i.large"]'


def test_default_cluster_name_matches_the_e2e_default(tmp_path: Path) -> None:
    """A direct `olf deploy --provider aws` (no AWS_CLUSTER_NAME set)
    creates this cluster; `olf e2e run --env aws` (also unset) must target
    the same one - see the cross-reference comment on
    AwsBackend/`_DEFAULT_CLUSTER_NAME` and `commands.e2e._default_kube_context`.
    """
    from olf.commands.e2e import _default_kube_context

    config = _config(tmp_path)
    backend = AwsBackend()

    deployed_cluster_name = backend.foundation_apply_variables(config, {})["cluster_name"]

    assert deployed_cluster_name == _default_kube_context("aws")


def test_foundation_variables_pass_through_a_bracketed_instance_type_list(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = AwsBackend()

    apply_vars = backend.foundation_apply_variables(config, {"AWS_NODE_INSTANCE_TYPES": '["m7i.large","m7i.xlarge"]'})

    assert apply_vars["node_instance_types"] == '["m7i.large","m7i.xlarge"]'


def test_foundation_variables_use_the_profile_region(tmp_path: Path) -> None:
    topology = resolve_topology(
        DeploymentProfile(
            name="acceptance",
            provider=ProviderSpec(type=Provider.AWS, region="eu-west-3"),
            preset=Preset.SLIM,
            stages=(StageSpec(name=StageName.DEV),),
        )
    )
    context = DeploymentContext.aws(repo_root=tmp_path, topology=topology)
    config = CloudDeploymentConfig.from_environment({}, context=context)

    variables = AwsBackend().foundation_apply_variables(config, {"AWS_REGION": "eu-west-1"})

    assert variables["aws_region"] == "eu-west-3"


def test_foundation_tfvars_file_is_none_when_absent(tmp_path: Path) -> None:
    backend = AwsBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/aws-eks"
    foundation_dir.mkdir(parents=True)

    assert backend.foundation_tfvars_file({}, repo_root=tmp_path, foundation_terraform_dir=foundation_dir) is None


def test_foundation_tfvars_file_defaults_to_sandbox_tfvars_when_present(tmp_path: Path) -> None:
    backend = AwsBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/aws-eks"
    foundation_dir.mkdir(parents=True)
    (foundation_dir / "sandbox.tfvars").write_text('region = "eu-west-1"\n')

    resolved = backend.foundation_tfvars_file({}, repo_root=tmp_path, foundation_terraform_dir=foundation_dir)

    assert resolved == foundation_dir / "sandbox.tfvars"


def test_resolve_foundation_facts_reads_all_four_outputs(tmp_path: Path) -> None:
    backend = AwsBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/aws-eks"

    class _OutputRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            name = argv[-1]
            outputs = {
                "cluster_name": "eks-openlakeforge-poc",
                "aws_region": "eu-west-1",
                "project_code_ecr_repository_url": "123.dkr.ecr.eu-west-1.amazonaws.com/project-code",
                "superset_ecr_repository_url": "123.dkr.ecr.eu-west-1.amazonaws.com/superset",
            }
            return _ok(outputs[name])

    tools = _toolkit(_OutputRunner())

    facts = backend.resolve_foundation_facts(tools, foundation_terraform_dir=foundation_dir, env={})

    assert facts.cluster_name == "eks-openlakeforge-poc"
    assert facts.kube_context == "eks-openlakeforge-poc"
    assert facts.aws_region == "eu-west-1"
    assert facts.project_code_repository == "123.dkr.ecr.eu-west-1.amazonaws.com/project-code"
    assert facts.superset_repository == "123.dkr.ecr.eu-west-1.amazonaws.com/superset"


def test_resolve_foundation_facts_tolerates_missing_registry_outputs(tmp_path: Path) -> None:
    """A foundation apply that left the cluster in state but failed before
    the ECR repository outputs were recorded (or an older/custom foundation
    that omits them) must not block teardown/status/forward/platform-down -
    none of those operations ever reads the registry facts; only
    resolve_effective_images does, as an optional fallback behind explicit
    repository overrides.
    """
    backend = AwsBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/aws-eks"

    class _PartialOutputRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            name = argv[-1]
            if name in ("project_code_ecr_repository_url", "superset_ecr_repository_url"):
                raise CommandExecutionError(argv, 1, stderr=f"no output named {name!r}")
            outputs = {"cluster_name": "eks-openlakeforge-poc", "aws_region": "eu-west-1"}
            return _ok(outputs[name])

    tools = _toolkit(_PartialOutputRunner())

    facts = backend.resolve_foundation_facts(tools, foundation_terraform_dir=foundation_dir, env={})

    assert facts.cluster_name == "eks-openlakeforge-poc"
    assert facts.aws_region == "eu-west-1"
    assert facts.project_code_repository == ""
    assert facts.superset_repository == ""


def test_update_kubeconfig_uses_eks_update_kubeconfig_with_alias(tmp_path: Path) -> None:
    backend = AwsBackend()
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)
    aws = _Aws()
    object.__setattr__(tools, "aws", aws)

    backend.update_kubeconfig(tools, _FACTS, kubeconfig_path=tmp_path / "kc.yaml", env={})

    assert aws.calls == [
        (
            "kubeconfig",
            ("eks-openlakeforge-poc",),
            {
                "region": "eu-west-1",
                "kubeconfig_path": tmp_path / "kc.yaml",
                "alias": "eks-openlakeforge-poc",
                "env": {},
            },
        )
    ]


def test_update_kubeconfig_refuses_facts_without_a_region(tmp_path: Path) -> None:
    tools = _toolkit(RecordingRunner(_ok()))
    object.__setattr__(tools, "aws", _Aws())

    with pytest.raises(DeploymentPreconditionError, match="aws_region"):
        AwsBackend().update_kubeconfig(
            tools, replace(_FACTS, aws_region=None), kubeconfig_path=tmp_path / "kc.yaml", env={}
        )


def test_registry_login_uses_ecr_password_via_docker_login(tmp_path: Path) -> None:
    backend = AwsBackend()
    runner = RecordingRunner(_ok("ecr-password\n"))
    tools = _toolkit(runner)
    object.__setattr__(tools, "aws", _Aws())

    backend.registry_login(tools, _FACTS, repository=_FACTS.project_code_repository, env={})

    login_call = next(c for c in runner.calls if c.argv[:2] == ["docker", "login"])
    assert login_call.argv[-1] == "123456789012.dkr.ecr.eu-west-1.amazonaws.com"
    assert login_call.kwargs["input_text"] == "ecr-password"


def test_registry_login_uses_the_effective_repository_not_the_foundation_default(tmp_path: Path) -> None:
    """A PROJECT_CODE_IMAGE_REPOSITORY/SUPERSET_IMAGE_REPOSITORY override pointed at a
    different ECR registry than the foundation's default must still get credentials
    for *that* registry - matching the removed shell scripts, which always derived
    `registry="${PROJECT_CODE_IMAGE_REPOSITORY%%/*}"` from the effective repository.
    """
    backend = AwsBackend()
    runner = RecordingRunner(_ok("ecr-password\n"))
    tools = _toolkit(runner)
    object.__setattr__(tools, "aws", _Aws())
    overridden_repository = "999999999999.dkr.ecr.us-east-1.amazonaws.com/shared/project-code"

    backend.registry_login(tools, _FACTS, repository=overridden_repository, env={})

    login_call = next(c for c in runner.calls if c.argv[:2] == ["docker", "login"])
    assert login_call.argv[-1] == "999999999999.dkr.ecr.us-east-1.amazonaws.com"


def test_cluster_reachable_uses_eks_describe_cluster(tmp_path: Path) -> None:
    backend = AwsBackend()
    runner = RecordingRunner(_fail())
    tools = _toolkit(runner)
    object.__setattr__(tools, "aws", _Aws(reachable=False))

    reachable = backend.cluster_reachable(tools, _FACTS, env={})

    assert reachable is False


def test_platform_apply_variables_include_aws_region_and_resolved_repository(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = AwsBackend()

    variables = backend.platform_apply_variables(config, _FACTS)

    assert variables["aws_region"] == "eu-west-1"
    assert variables["project_code_image_repository"] == _FACTS.project_code_repository
    assert variables["superset_image_repository"] == _FACTS.superset_repository
    assert set(variables.keys()) == {
        "profile_name",
        "shared_namespace",
        "stages",
        "manage_user_deployments",
        "aws_region",
        "kube_context",
        "kubeconfig_path",
        "helm_repository_cache_path",
        "helm_repository_config_path",
        "foundation_state_path",
        "project_code_image_repository",
        "project_code_image_tag",
        "project_code_image_pull_policy",
        "project_code_image_revision",
        "superset_image_repository",
        "superset_image_tag",
        "superset_image_pull_policy",
        "trino_chart_package_path",
        "dagster_chart_package_path",
        "openmetadata_chart_package_path",
        "openmetadata_deps_chart_package_path",
        "superset_chart_package_path",
    }


def test_platform_destroy_variables_are_the_five_var_subset(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = AwsBackend()

    variables = backend.platform_destroy_variables(config, _FACTS)

    assert set(variables.keys()) == {
        "profile_name",
        "shared_namespace",
        "stages",
        "manage_user_deployments",
        "aws_region",
        "kube_context",
        "kubeconfig_path",
        "helm_repository_cache_path",
        "helm_repository_config_path",
        "foundation_state_path",
    }


def test_cleanup_polaris_jobs_before_apply_is_false() -> None:
    assert AwsBackend().cleanup_polaris_jobs_before_apply() is False


def test_forward_base_targets_is_trino_only() -> None:
    targets = AwsBackend().forward_base_targets()

    assert [t.label for t in targets] == ["trino"]


def test_artifact_transport_is_direct() -> None:
    assert AwsBackend().artifact_transport() == "direct"


class _RaisingRunner(RecordingRunner):
    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        raise CommandExecutionError(argv, 1, stderr="not authorized")


def test_preflight_wraps_sts_failure() -> None:
    backend = AwsBackend()
    tools = _toolkit(_RaisingRunner())

    with pytest.raises(DeploymentPreconditionError, match="not authenticated"):
        backend.preflight(tools, env={})
