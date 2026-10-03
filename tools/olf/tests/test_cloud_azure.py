from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.cloud.azure import AzureBackend
from olf.deployment.cloud.backend import FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.context import DeploymentContext, Profile
from olf.deployment.engine import Toolkit
from olf.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm", "aws", "az")
_FACTS = FoundationFacts(
    cluster_name="aks-openlakeforge-poc",
    kube_context="aks-openlakeforge-poc",
    project_code_repository="openlakeforgepoc.azurecr.io/openlakeforge/project-code",
    superset_repository="openlakeforgepoc.azurecr.io/openlakeforge/superset",
    azure_resource_group="openlakeforge-poc-rg",
    azure_acr_name="openlakeforgepoc",
)


class _Azure:
    def __init__(self, *, reachable: bool = True, fail_account: bool = False) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.reachable = reachable
        self.fail_account = fail_account

    def account_show(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if self.fail_account:
            raise RuntimeError("not authenticated")
        return {"id": "sub-id"}

    def aks_get_credentials(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls.append(("kubeconfig", args, kwargs))

    def acr_login(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls.append(("acr", args, kwargs))
        return "acr-token"

    def aks_show(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls.append(("show", args, kwargs))
        return CommandResult(argv=(), returncode=0 if self.reachable else 1, stdout="", stderr="", duration_seconds=0)


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> CloudDeploymentConfig:
    context = DeploymentContext.azure(repo_root=tmp_path, profile=profile)
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


class _RaisingRunner(RecordingRunner):
    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        raise CommandExecutionError(argv, 1, stderr="not logged in")


def test_scope_is_azure() -> None:
    assert AzureBackend().scope == "azure"


def test_foundation_state_resource_addr() -> None:
    assert AzureBackend().foundation_state_resource_addr() == "azurerm_kubernetes_cluster.this"


def test_foundation_apply_and_destroy_variables_are_identical(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = AzureBackend()

    apply_vars = backend.foundation_apply_variables(config, {})
    destroy_vars = backend.foundation_destroy_variables(config, {})

    assert apply_vars == destroy_vars
    assert apply_vars == {
        "cluster_name": "aks-openlakeforge-poc",
        "node_count": "3",
        "acr_name_prefix": "openlakeforgepoc",
        "kubeconfig_path": str(config.paths.kubeconfig_path),
    }


def test_foundation_tfvars_file_raises_when_missing(tmp_path: Path) -> None:
    backend = AzureBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/azure-aks"
    foundation_dir.mkdir(parents=True)

    with pytest.raises(DeploymentPreconditionError, match="configuration not found"):
        backend.foundation_tfvars_file({}, repo_root=tmp_path, foundation_terraform_dir=foundation_dir)


def test_foundation_tfvars_file_resolves_default_path_when_present(tmp_path: Path) -> None:
    backend = AzureBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/azure-aks"
    foundation_dir.mkdir(parents=True)
    (foundation_dir / "sandbox.tfvars").write_text('resource_group = "rg"\n')

    resolved = backend.foundation_tfvars_file({}, repo_root=tmp_path, foundation_terraform_dir=foundation_dir)

    assert resolved == foundation_dir / "sandbox.tfvars"


def test_foundation_tfvars_file_honors_relative_override_against_repo_root(tmp_path: Path) -> None:
    backend = AzureBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/azure-aks"
    foundation_dir.mkdir(parents=True)
    override = tmp_path / "custom/azure.tfvars"
    override.parent.mkdir(parents=True)
    override.write_text('resource_group = "rg"\n')

    resolved = backend.foundation_tfvars_file(
        {"AZURE_TFVARS_FILE": "custom/azure.tfvars"}, repo_root=tmp_path, foundation_terraform_dir=foundation_dir
    )

    assert resolved == override


def test_resolve_foundation_facts_falls_back_to_acr_login_server_prefix_when_acr_name_output_missing(
    tmp_path: Path,
) -> None:
    backend = AzureBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/azure-aks"

    class _OutputRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            name = argv[-1]
            if name == "acr_name":
                raise CommandExecutionError(argv, 1, stderr="no output named acr_name")
            outputs = {
                "resource_group_name": "openlakeforge-poc-rg",
                "cluster_name": "aks-openlakeforge-poc",
                "acr_login_server": "openlakeforgepoc.azurecr.io",
            }
            return _ok(outputs[name])

    tools = _toolkit(_OutputRunner())

    facts = backend.resolve_foundation_facts(tools, foundation_terraform_dir=foundation_dir, env={})

    assert facts.cluster_name == "aks-openlakeforge-poc"
    assert facts.azure_resource_group == "openlakeforge-poc-rg"
    assert facts.azure_acr_name == "openlakeforgepoc"
    assert facts.project_code_repository == "openlakeforgepoc.azurecr.io/openlakeforge/project-code"
    assert facts.superset_repository == "openlakeforgepoc.azurecr.io/openlakeforge/superset"


def test_resolve_foundation_facts_tolerates_missing_acr_login_server_output(tmp_path: Path) -> None:
    """A foundation apply that failed before `acr_login_server` was
    recorded (or an older/custom foundation that omits it) must not block
    teardown/status/forward/platform-down - none of those operations ever
    reads the registry facts; only resolve_effective_images does, as an
    optional fallback behind explicit repository overrides.
    """
    backend = AzureBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/azure-aks"

    class _PartialOutputRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            name = argv[-1]
            if name in ("acr_login_server", "acr_name"):
                raise CommandExecutionError(argv, 1, stderr=f"no output named {name!r}")
            outputs = {"resource_group_name": "openlakeforge-poc-rg", "cluster_name": "aks-openlakeforge-poc"}
            return _ok(outputs[name])

    tools = _toolkit(_PartialOutputRunner())

    facts = backend.resolve_foundation_facts(tools, foundation_terraform_dir=foundation_dir, env={})

    assert facts.cluster_name == "aks-openlakeforge-poc"
    assert facts.azure_resource_group == "openlakeforge-poc-rg"
    assert facts.azure_acr_name == ""
    assert facts.project_code_repository == "/openlakeforge/project-code"
    assert facts.superset_repository == "/openlakeforge/superset"


def test_resolve_foundation_facts_uses_acr_name_output_when_present(tmp_path: Path) -> None:
    backend = AzureBackend()
    foundation_dir = tmp_path / "infra/terraform/foundations/azure-aks"

    class _OutputRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            name = argv[-1]
            outputs = {
                "resource_group_name": "openlakeforge-poc-rg",
                "cluster_name": "aks-openlakeforge-poc",
                "acr_login_server": "openlakeforgepoc.azurecr.io",
                "acr_name": "explicitacrname",
            }
            return _ok(outputs[name])

    tools = _toolkit(_OutputRunner())

    facts = backend.resolve_foundation_facts(tools, foundation_terraform_dir=foundation_dir, env={})

    assert facts.azure_acr_name == "explicitacrname"


def test_update_kubeconfig_uses_aks_get_credentials(tmp_path: Path) -> None:
    backend = AzureBackend()
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)
    azure = _Azure()
    object.__setattr__(tools, "azure", azure)

    backend.update_kubeconfig(tools, _FACTS, kubeconfig_path=tmp_path / "kc.yaml", env={})

    assert azure.calls[0][0] == "kubeconfig"
    assert azure.calls[0][1] == ("aks-openlakeforge-poc",)


def test_registry_login_uses_acr_login_with_acr_name(tmp_path: Path) -> None:  # noqa: ARG001
    backend = AzureBackend()
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)
    azure = _Azure()
    object.__setattr__(tools, "azure", azure)

    backend.registry_login(tools, _FACTS, repository=_FACTS.project_code_repository, env={})

    assert azure.calls[0][0] == "acr"
    assert azure.calls[0][1] == ("openlakeforgepoc",)
    assert next(c for c in runner.calls if c.argv[:2] == ["docker", "login"]).kwargs["input_text"] == "acr-token"


def test_registry_login_derives_acr_name_from_an_overridden_repository(tmp_path: Path) -> None:  # noqa: ARG001
    """AZURE_PROJECT_CODE_IMAGE_REPOSITORY/AZURE_SUPERSET_IMAGE_REPOSITORY
    can point at a different ACR than the foundation's default - the login
    must target *that* registry, matching the AWS backend's equivalent fix,
    not the foundation's `facts.azure_acr_name`.
    """
    backend = AzureBackend()
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)
    azure = _Azure()
    object.__setattr__(tools, "azure", azure)
    overridden_repository = "otheracr.azurecr.io/shared/project-code"

    backend.registry_login(tools, _FACTS, repository=overridden_repository, env={})

    assert azure.calls[0][1] == ("otheracr",)


def test_registry_login_derives_acr_name_even_when_foundation_facts_have_no_registry(tmp_path: Path) -> None:
    """When registry outputs are absent from the foundation state (see
    resolve_foundation_facts's lenient handling) but an explicit repository
    override is supplied, the login must not depend on `facts.azure_acr_name`
    being resolvable at all.
    """
    backend = AzureBackend()
    runner = RecordingRunner(_ok())
    tools = _toolkit(runner)
    azure = _Azure()
    object.__setattr__(tools, "azure", azure)
    facts_without_registry = FoundationFacts(
        cluster_name="aks-openlakeforge-poc",
        kube_context="aks-openlakeforge-poc",
        project_code_repository="",
        superset_repository="",
        azure_resource_group="openlakeforge-poc-rg",
        azure_acr_name="",
    )

    backend.registry_login(tools, facts_without_registry, repository="otheracr.azurecr.io/shared/project-code", env={})

    assert azure.calls[0][1] == ("otheracr",)


def test_cluster_reachable_refuses_facts_without_a_resource_group() -> None:
    tools = _toolkit(RecordingRunner(_ok()))
    object.__setattr__(tools, "azure", _Azure())

    with pytest.raises(DeploymentPreconditionError, match="resource group"):
        AzureBackend().cluster_reachable(tools, replace(_FACTS, azure_resource_group=None), env={})


def test_cluster_reachable_uses_aks_show(tmp_path: Path) -> None:  # noqa: ARG001
    backend = AzureBackend()
    runner = RecordingRunner(_fail())
    tools = _toolkit(runner)
    object.__setattr__(tools, "azure", _Azure(reachable=False))

    reachable = backend.cluster_reachable(tools, _FACTS, env={})

    assert reachable is False


def test_platform_apply_variables_have_no_aws_region(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = AzureBackend()

    variables = backend.platform_apply_variables(config, _FACTS)

    assert "aws_region" not in variables
    assert variables["project_code_image_repository"] == _FACTS.project_code_repository
    assert set(variables.keys()) == {
        "profile_name",
        "shared_namespace",
        "stages",
        "manage_user_deployments",
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
        "seaweedfs_chart_package_path",
        "polaris_chart_package_path",
        "openmetadata_chart_package_path",
        "openmetadata_deps_chart_package_path",
        "superset_chart_package_path",
    }


def test_platform_destroy_variables_are_the_four_var_subset(tmp_path: Path) -> None:
    config = _config(tmp_path)
    backend = AzureBackend()

    variables = backend.platform_destroy_variables(config, _FACTS)

    assert set(variables.keys()) == {
        "profile_name",
        "shared_namespace",
        "stages",
        "manage_user_deployments",
        "kube_context",
        "kubeconfig_path",
        "helm_repository_cache_path",
        "helm_repository_config_path",
        "foundation_state_path",
    }


def test_cleanup_polaris_jobs_before_apply_is_true() -> None:
    assert AzureBackend().cleanup_polaris_jobs_before_apply() is True


def test_forward_base_targets_include_seaweedfs_polaris_and_trino() -> None:
    targets = AzureBackend().forward_base_targets()

    assert [t.label for t in targets] == ["seaweedfs-s3", "polaris", "trino"]


def test_artifact_transport_is_port_forward() -> None:
    assert AzureBackend().artifact_transport() == "port-forward"


def test_preflight_wraps_account_show_failure() -> None:
    backend = AzureBackend()
    tools = _toolkit(_RaisingRunner())
    object.__setattr__(tools, "azure", _Azure(fail_account=True))

    with pytest.raises(DeploymentPreconditionError, match="olf auth login"):
        backend.preflight(tools, env={})
