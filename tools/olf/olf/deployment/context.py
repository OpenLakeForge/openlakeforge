"""Typed deployment context: paths, provider identity, and scoped environment.

This is deliberately separate from `olf.config` (the lightweight runtime
contract/environment helper); `DeploymentContext` is the new-code home for
provider/profile-scoped path and environment construction. It never executes
subprocesses and never mutates `os.environ` — building it is pure data
assembly, and command execution stays owned by `olf.tooling`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from olf.deployment.errors import DeploymentPreconditionError
from olf.project import ProjectSpec

if TYPE_CHECKING:
    from olf.profile import DeploymentTopology, StageName

SHARED_NAMESPACE = "olf-system"
DEFAULT_LOCAL_CLUSTER_NAME = "openlakeforge-local"


def stage_namespace(stage: StageName | str) -> str:
    """The deterministic Kubernetes namespace owning one stage's services.

    Namespace naming is physical, so it lives here rather than on
    `DeploymentTopology` -- ADR 0011 keeps the resolved topology free of
    namespaces, Helm releases, and endpoints.
    """
    from olf.profile import StageName

    return f"olf-{StageName(stage).value}"


def topology_variables(context: DeploymentContext) -> dict[str, str]:
    """The resolved topology, as every stage-aware root's typed Terraform inputs.

    Every provider's root derives its stage namespaces (or, for the
    single-namespace cloud POC roots, per-stage releases and physical
    resource names), service multiplicity, and capability gates from
    `stages`; nothing downstream re-reads the Deployment Profile. Every
    stage the resolver knows about is passed with its own `enabled` flag
    rather than only the enabled subset, so what the root receives is the
    resolved topology itself and not a filtered view of it.
    """
    topology = context.topology
    stages = {
        stage.name.value: {
            "enabled": stage.enabled,
            "analytics": stage.capabilities.analytics,
            "governance": stage.capabilities.governance,
        }
        for stage in topology.stages
    }
    return {
        "profile_name": topology.profile_name,
        "shared_namespace": context.shared_namespace,
        "stages": json.dumps(stages, sort_keys=True, separators=(",", ":")),
        "manage_user_deployments": "true" if context.manage_user_deployments else "false",
    }


def _resolve_foundation_state_path(foundation_terraform_dir: Path) -> Path:
    """Honor `FOUNDATION_STATE_PATH`, matching every removed provider's
    stack scripts (`FOUNDATION_STATE_PATH="${FOUNDATION_STATE_PATH:-
    ${FOUNDATION_TERRAFORM_DIR}/terraform.tfstate}"`), which both gated
    their own state-missing precondition check on this path and supplied it
    as the `foundation_state_path` Terraform variable to the platform root -
    a non-default foundation state (e.g. a distribution's isolated backend)
    would otherwise be rejected by every platform/artifact/status/forward/
    teardown command.
    """
    override = os.environ.get("FOUNDATION_STATE_PATH")
    if override:
        return Path(override).resolve()
    return foundation_terraform_dir / "terraform.tfstate"


def _resolved_namespaces(*, stage: StageName) -> tuple[str, str]:
    """The (stage, shared) namespaces for one run.

    Every root (local, aws, azure - #114) is stage-aware and derives both
    from the resolved topology; neither is overridable (`_shared.py` rejects
    `--namespace` for exactly this reason).
    """
    return stage_namespace(stage), SHARED_NAMESPACE


class Provider(StrEnum):
    LOCAL = "local"
    AWS = "aws"
    AZURE = "azure"


class Profile(StrEnum):
    SLIM = "slim"
    FULL = "full"


@dataclass(frozen=True)
class DeploymentPaths:
    repo_root: Path
    distribution_root: Path
    project: ProjectSpec
    state_root: Path
    work_root: Path
    cache_root: Path
    kubeconfig_path: Path
    foundation_terraform_dir: Path
    platform_terraform_dir: Path
    foundation_state_path: Path
    platform_state_path: Path
    terraform_data_root: Path
    docker_config_dir: Path
    helm_cache_dir: Path
    helm_repository_config: Path
    helm_repository_cache: Path
    superset_report_work_dir: Path
    port_forward_log_prefix: Path
    installed: bool = False
    """True when these paths came from a packaged distribution (payload under
    `OLF_HOME`) rather than a source checkout.

    The authoritative installed-mode signal, and deliberately *not* derivable
    from `distribution_root != repo_root`: an installed run without
    `--project-root` deploys the bundled demo, so both roots are the same
    immutable payload. Consumers that must know whether state, plugin data,
    and chart caches live outside the (read-only) distribution - and whether
    catalog chart digests are enforced - read this instead of comparing roots.
    """


@dataclass(frozen=True)
class DeploymentFeatures:
    governance_enabled: bool
    analytics_enabled: bool

    @classmethod
    def for_stage(cls, topology: DeploymentTopology, stage: StageName) -> DeploymentFeatures:
        resolved = topology.stage(stage)
        if resolved is None or not resolved.enabled:
            return cls(governance_enabled=False, analytics_enabled=False)
        return cls(
            governance_enabled=resolved.capabilities.governance,
            analytics_enabled=resolved.capabilities.analytics,
        )

    @classmethod
    def across_stages(cls, topology: DeploymentTopology) -> DeploymentFeatures:
        """The union of every enabled stage's capabilities.

        What Terraform must provision, as opposed to what one selected stage
        consumes: a slim DEV plus a full PROD still needs the Superset chart
        pulled and the shared OpenMetadata deployed.
        """
        enabled = [stage for stage in topology.stages if stage.enabled]
        return cls(
            governance_enabled=any(stage.capabilities.governance for stage in enabled),
            analytics_enabled=any(stage.capabilities.analytics for stage in enabled),
        )


@dataclass(frozen=True)
class DeploymentContext:
    provider: Provider
    profile: Profile
    namespace: str
    kube_context: str
    paths: DeploymentPaths
    features: DeploymentFeatures
    topology: DeploymentTopology
    stage: StageName
    shared_namespace: str = SHARED_NAMESPACE
    allow_stage_removal: bool = False
    # Which lifecycle owns Dagster user code. `olf deploy` (deprecated, single
    # stage) leaves Terraform owning it; the #115 commands, which resolve a
    # Deployment Profile, hand it to `olf project deploy` instead.
    manage_user_deployments: bool = True

    @property
    def platform_features(self) -> DeploymentFeatures:
        """Capabilities across every enabled stage. See `DeploymentFeatures.across_stages`."""
        return DeploymentFeatures.across_stages(self.topology)

    @property
    def enabled_stages(self) -> tuple[StageName, ...]:
        return tuple(stage.name for stage in self.topology.stages if stage.enabled)

    @property
    def owned_namespaces(self) -> tuple[str, ...]:
        """Every namespace this deployment created: the shared platform one
        plus one per enabled stage. The single-namespace cloud roots collapse
        to one entry."""
        return tuple(
            dict.fromkeys((self.shared_namespace, *(self.namespace_for(stage) for stage in self.enabled_stages)))
        )

    def namespace_for(self, stage: StageName | str) -> str:
        """The namespace owning one stage's services on this provider.

        Only a stage-aware root gives each stage its own; the cloud POC roots
        serve every stage from the one namespace they were given."""
        from olf.profile import StageName

        if self.shared_namespace != SHARED_NAMESPACE:
            return self.shared_namespace
        return stage_namespace(StageName(stage))

    @classmethod
    def for_provider(cls, provider: Provider | str, *, repo_root: Path, **kwargs: object) -> DeploymentContext:
        resolved = Provider(provider)
        # The three factories take different keyword sets, so the forwarded
        # `**kwargs` can only be checked at call time.
        factories: dict[Provider, Callable[..., DeploymentContext]] = {
            Provider.LOCAL: cls.local,
            Provider.AWS: cls.aws,
            Provider.AZURE: cls.azure,
        }
        return factories[resolved](repo_root=repo_root, **kwargs)

    @classmethod
    def local(
        cls,
        *,
        repo_root: Path,
        profile: Profile = Profile.FULL,
        distribution_root: Path | None = None,
        state_root: Path | None = None,
        work_root: Path | None = None,
        cache_root: Path | None = None,
        namespace: str = "",
        cluster_name: str = DEFAULT_LOCAL_CLUSTER_NAME,
        kubeconfig_path: Path | None = None,
        topology: DeploymentTopology | None = None,
        stage: StageName | str | None = None,
        allow_stage_removal: bool = False,
        manage_user_deployments: bool = True,
    ) -> DeploymentContext:
        """Build the local `DeploymentContext`.

        `kubeconfig_path`, when given, overrides the default
        `<repo_root>/.tmp/kubeconfigs/local.yaml` - CI runs multiple local
        kind deployments in parallel (e.g. a slim smoke job alongside a full
        deployment) and isolates each with its own kubeconfig file via
        `LOCAL_KUBECONFIG_PATH`; the shell scripts this replaces honored the
        same override.
        """
        context = cls._build(
            provider=Provider.LOCAL,
            scope="local",
            repo_root=repo_root,
            distribution_root=distribution_root,
            state_root=state_root,
            work_root=work_root,
            cache_root=cache_root,
            profile=profile,
            namespace=namespace,
            topology=topology,
            stage=stage,
            allow_stage_removal=allow_stage_removal,
            manage_user_deployments=manage_user_deployments,
            stage_aware_namespaces=True,
            kube_context=f"kind-{cluster_name}",
            foundation_terraform_dir=Path("infra/terraform/foundations/local-kind"),
            platform_terraform_dir=Path("infra/terraform/environments/local"),
        )
        if kubeconfig_path is not None:
            context = replace(context, paths=replace(context.paths, kubeconfig_path=Path(kubeconfig_path).resolve()))
        return context

    @classmethod
    def aws(
        cls,
        *,
        repo_root: Path,
        profile: Profile = Profile.FULL,
        distribution_root: Path | None = None,
        state_root: Path | None = None,
        work_root: Path | None = None,
        cache_root: Path | None = None,
        namespace: str = "",
        kube_context: str = "",
        kubeconfig_path: Path | None = None,
        topology: DeploymentTopology | None = None,
        stage: StageName | str | None = None,
        allow_stage_removal: bool = False,
        manage_user_deployments: bool = True,
    ) -> DeploymentContext:
        """Build the AWS `DeploymentContext`.

        `kubeconfig_path`, when given, overrides the default
        `<repo_root>/.tmp/kubeconfigs/aws.yaml` - the concurrent-deployment
        workflow in `docs/setup/cloud-poc-setup.md` isolates a parallel
        `olf deploy --provider aws --kubeconfig-path "$AWS_KUBECONFIG_PATH"`
        run with its own kubeconfig file, matching `local()`'s override.
        """
        context = cls._build(
            provider=Provider.AWS,
            scope="aws",
            repo_root=repo_root,
            distribution_root=distribution_root,
            state_root=state_root,
            work_root=work_root,
            cache_root=cache_root,
            profile=profile,
            namespace=namespace,
            topology=topology,
            stage=stage,
            allow_stage_removal=allow_stage_removal,
            manage_user_deployments=manage_user_deployments,
            stage_aware_namespaces=True,
            kube_context=kube_context,
            foundation_terraform_dir=Path("infra/terraform/foundations/aws-eks"),
            platform_terraform_dir=Path("infra/terraform/environments/aws-poc"),
        )
        if kubeconfig_path is not None:
            context = replace(context, paths=replace(context.paths, kubeconfig_path=Path(kubeconfig_path).resolve()))
        return context

    @classmethod
    def azure(
        cls,
        *,
        repo_root: Path,
        profile: Profile = Profile.FULL,
        distribution_root: Path | None = None,
        state_root: Path | None = None,
        work_root: Path | None = None,
        cache_root: Path | None = None,
        namespace: str = "",
        kube_context: str = "",
        kubeconfig_path: Path | None = None,
        topology: DeploymentTopology | None = None,
        stage: StageName | str | None = None,
        allow_stage_removal: bool = False,
        manage_user_deployments: bool = True,
    ) -> DeploymentContext:
        """Build the Azure `DeploymentContext`.

        `kubeconfig_path`, when given, overrides the default
        `<repo_root>/.tmp/kubeconfigs/azure.yaml` - see `aws()`.
        """
        context = cls._build(
            provider=Provider.AZURE,
            scope="azure",
            repo_root=repo_root,
            distribution_root=distribution_root,
            state_root=state_root,
            work_root=work_root,
            cache_root=cache_root,
            profile=profile,
            namespace=namespace,
            topology=topology,
            stage=stage,
            allow_stage_removal=allow_stage_removal,
            manage_user_deployments=manage_user_deployments,
            stage_aware_namespaces=True,
            kube_context=kube_context,
            foundation_terraform_dir=Path("infra/terraform/foundations/azure-aks"),
            platform_terraform_dir=Path("infra/terraform/environments/azure-poc"),
        )
        if kubeconfig_path is not None:
            context = replace(context, paths=replace(context.paths, kubeconfig_path=Path(kubeconfig_path).resolve()))
        return context

    @classmethod
    def _build(
        cls,
        *,
        provider: Provider,
        scope: str,
        repo_root: Path,
        distribution_root: Path | None,
        state_root: Path | None,
        work_root: Path | None,
        cache_root: Path | None,
        profile: Profile,
        namespace: str,
        topology: DeploymentTopology | None,
        stage: StageName | str | None,
        allow_stage_removal: bool,
        manage_user_deployments: bool = True,
        stage_aware_namespaces: bool,
        kube_context: str,
        foundation_terraform_dir: Path,
        platform_terraform_dir: Path,
    ) -> DeploymentContext:
        from olf.profile import Preset, StageName, legacy_single_stage_topology

        resolved_topology = (
            topology
            if topology is not None
            else legacy_single_stage_topology(provider=provider, preset=Preset(profile.value))
        )
        resolved_stage = StageName(stage) if stage is not None else StageName.DEV
        selected = resolved_topology.stage(resolved_stage)
        if selected is None or not selected.enabled:
            raise DeploymentPreconditionError(
                f"stage {resolved_stage.value!r} is not enabled in the resolved topology "
                f"(enabled: {[s.name.value for s in resolved_topology.stages if s.enabled]})"
            )
        resolved_repo_root = Path(repo_root).resolve()
        resolved_distribution_root = (
            Path(distribution_root).resolve() if distribution_root is not None else resolved_repo_root
        )
        resolved_work_root = (
            Path(work_root).resolve() / scope if work_root is not None else resolved_repo_root / ".tmp"
        )
        resolved_state_root = (Path(state_root).resolve() / scope) if state_root is not None else resolved_repo_root
        resolved_cache_root = Path(cache_root).resolve() if cache_root is not None else resolved_work_root
        helm_root = resolved_work_root / "helm" / scope
        foundation_dir = resolved_distribution_root / foundation_terraform_dir
        platform_dir = resolved_distribution_root / platform_terraform_dir
        foundation_state = (
            _resolve_foundation_state_path(foundation_dir)
            if state_root is None
            else Path(os.environ.get("FOUNDATION_STATE_PATH", resolved_state_root / "foundation.tfstate")).resolve()
        )

        paths = DeploymentPaths(
            repo_root=resolved_repo_root,
            distribution_root=resolved_distribution_root,
            project=ProjectSpec(root=resolved_repo_root, distribution_root=resolved_distribution_root),
            state_root=resolved_state_root,
            work_root=resolved_work_root,
            cache_root=resolved_cache_root,
            kubeconfig_path=(
                resolved_state_root / "kubeconfig.yaml"
                if state_root is not None
                else resolved_work_root / "kubeconfigs" / f"{scope}.yaml"
            ),
            foundation_terraform_dir=foundation_dir,
            platform_terraform_dir=platform_dir,
            foundation_state_path=foundation_state,
            platform_state_path=(
                resolved_state_root / "platform.tfstate"
                if state_root is not None
                else platform_dir / "terraform.tfstate"
            ),
            terraform_data_root=resolved_work_root / "terraform-data",
            docker_config_dir=resolved_work_root / "docker" / scope,
            helm_cache_dir=(resolved_cache_root / "helm") if cache_root is not None else helm_root / "charts",
            helm_repository_config=helm_root / "repositories.yaml",
            helm_repository_cache=helm_root / "repository-cache",
            superset_report_work_dir=resolved_work_root / "superset-reports" / scope,
            port_forward_log_prefix=Path(f"/tmp/openlakeforge-{scope}"),
            installed=state_root is not None,
        )
        resolved_namespace, resolved_shared_namespace = _resolved_namespaces(stage=resolved_stage)
        return cls(
            provider=provider,
            profile=profile,
            namespace=resolved_namespace,
            kube_context=kube_context,
            paths=paths,
            features=DeploymentFeatures.for_stage(resolved_topology, resolved_stage),
            topology=resolved_topology,
            stage=resolved_stage,
            allow_stage_removal=allow_stage_removal,
            manage_user_deployments=manage_user_deployments,
            shared_namespace=resolved_shared_namespace,
        )

    def command_env(
        self,
        *,
        base: Mapping[str, str] | None = None,
        docker_host: str | None = None,
    ) -> dict[str, str]:
        """Build a command-scoped environment overlay.

        Does not mutate `os.environ` or any mapping passed in as `base`;
        returns a fresh dict a caller can pass to `ProcessRunner`/tool
        adapters as `env`.
        """
        env: dict[str, str] = dict(base) if base is not None else {}
        env["KUBECONFIG"] = str(self.paths.kubeconfig_path)
        env["KUBE_CONTEXT"] = self.kube_context
        env["OPENLAKEFORGE_STAGE"] = self.stage.value
        env["OPENLAKEFORGE_SHARED_NAMESPACE"] = self.shared_namespace
        env["DOCKER_CONFIG"] = str(self.paths.docker_config_dir)
        env.setdefault("DOCKER_BUILDKIT", "1")
        env.setdefault("BUILDKIT_PROGRESS", "plain")
        env["HELM_REPOSITORY_CONFIG"] = str(self.paths.helm_repository_config)
        env["HELM_REPOSITORY_CACHE"] = str(self.paths.helm_repository_cache)
        env["SUPERSET_REPORT_WORK_DIR"] = str(self.paths.superset_report_work_dir)
        env["OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX"] = str(self.paths.port_forward_log_prefix)
        # Fresh contract readers create their own managed-tool resolver. Keep
        # them anchored to the same verified distribution as the Toolkit.
        env["OLF_DISTRIBUTION_ROOT"] = str(self.paths.distribution_root)
        env["OPENLAKEFORGE_PROJECT_ROOT"] = str(self.paths.project.root)
        # The legacy name remains for contributor/release compatibility.
        env["OPENLAKEFORGE_REPO_ROOT"] = str(self.paths.project.root)
        if self.provider is Provider.AWS and self.topology.region:
            # The Deployment Profile is the authority for a v0.3 cloud
            # deployment.  Avoid silently sending Terraform, ECR, or Glue to
            # an ambient CLI default in a different region.
            env["AWS_REGION"] = self.topology.region
            env["AWS_DEFAULT_REGION"] = self.topology.region
        # Gate on `installed`, never on `distribution_root != repo_root`: the
        # documented quick start (`uv tool install` then `olf deploy` with no
        # --project-root) deploys the bundled demo, so both roots are the same
        # read-only payload. Comparing them left Terraform without TF_DATA_DIR
        # and `-state=`, so `terraform apply` tried to write `terraform.tfstate`
        # into the 0555 payload and failed with "permission denied".
        if self.paths.installed:
            env["OPENLAKEFORGE_TERRAFORM_DATA_ROOT"] = str(self.paths.terraform_data_root)
            env["OPENLAKEFORGE_TERRAFORM_STATE_ROOT"] = str(self.paths.state_root)
            env["OPENLAKEFORGE_TERRAFORM_READONLY_LOCKFILE"] = "true"
        if docker_host:
            env["DOCKER_HOST"] = docker_host
        return env

    def prepare_directories(self, *, docker_cli_plugins_source: Path | None = None) -> None:
        """Create OpenLakeForge-owned working paths.

        Also links `~/.docker/cli-plugins` (or `$DOCKER_CONFIG/cli-plugins`)
        into the scoped Docker config directory, if present, so `docker build`
        keeps using BuildKit without an existing destination being overwritten.
        """
        for directory in (
            self.paths.kubeconfig_path.parent,
            self.paths.docker_config_dir,
            self.paths.helm_repository_config.parent,
            self.paths.helm_repository_cache,
            self.paths.helm_cache_dir,
            self.paths.superset_report_work_dir,
            self.paths.terraform_data_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._link_docker_cli_plugins(source=docker_cli_plugins_source)

    def _link_docker_cli_plugins(self, *, source: Path | None) -> None:
        if source is None:
            docker_config_source = os.environ.get("DOCKER_CONFIG")
            source = Path(docker_config_source) if docker_config_source else Path.home() / ".docker"
        cli_plugins_source = source / "cli-plugins"
        destination = self.paths.docker_config_dir / "cli-plugins"

        if not cli_plugins_source.is_dir() or destination.exists():
            return
        destination.symlink_to(cli_plugins_source)
