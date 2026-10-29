"""Typed local-provider configuration.

Plain frozen dataclasses with `from_environment` classmethods, matching the
existing `OpenMetadataConfig.from_environment` convention (no pydantic
anywhere in `olf`). Each settings group is a straight port of the
corresponding shell script's `${VAR:-default}` fallback chain.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from olf.deployment.charts import ChartSetting, resolve_chart_settings
from olf.deployment.context import DeploymentContext, DeploymentFeatures
from olf.deployment.env_settings import env as _env
from olf.deployment.env_settings import float_env as _float_env
from olf.deployment.env_settings import int_env as _int_env
from olf.deployment.env_settings import retry_policy as _retry_policy
from olf.deployment.env_settings import truthy as _truthy
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.retry import RetryPolicy
from olf.profile import PUBLIC_EXAMPLE_IMAGE_REPOSITORY, resolve_project


@dataclass(frozen=True)
class ClusterSettings:
    name: str
    config_path: Path
    wait_timeout: str
    reset_existing: bool

    @classmethod
    def from_environment(cls, environ: Mapping[str, str], *, repo_root: Path, cluster_name: str) -> ClusterSettings:
        config_path = Path(_env(environ, "CLUSTER_CONFIG", str(repo_root / "infra/kind/local/kind-cluster.yaml")))
        return cls(
            name=cluster_name,
            config_path=config_path,
            wait_timeout=_env(environ, "KIND_WAIT_TIMEOUT", "120s"),
            reset_existing=_truthy(_env(environ, "LOCAL_FOUNDATION_RESET", "false")),
        )


@dataclass(frozen=True)
class ImageSettings:
    project_code_repository: str
    project_code_tag: str
    project_code_pull_policy: str
    project_code_revision: str
    project_code_python_base_image: str
    # True when project_code_* is the release-published example image rather than a local build.
    project_code_published: bool
    superset_repository: str
    superset_tag: str
    superset_build: bool
    superset_base_image: str
    pull_retry: RetryPolicy
    build_retry: RetryPolicy
    push_retry: RetryPolicy

    @property
    def project_code_image(self) -> str:
        return f"{self.project_code_repository}:{self.project_code_tag}"

    @property
    def superset_image(self) -> str:
        return f"{self.superset_repository}:{self.superset_tag}"

    def builds_superset(self, server_arch: str) -> bool:
        """The release publishes Superset for amd64 only, so any other Docker engine builds it, as does a maintainer."""
        return self.superset_build or server_arch not in ("amd64", "x86_64")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str], *, published: str | None = None) -> ImageSettings:
        """`published`: a `repository:version@sha256:...` reference to deploy instead of building locally."""
        repository, tag, pull_policy = PUBLIC_EXAMPLE_IMAGE_REPOSITORY, "local", "Never"
        if published:
            if "@sha256:" not in published:
                raise DeploymentPreconditionError(f"the pinned example project image {published!r} has no digest.")
            name, _, digest = published.partition("@")
            repository, _, version = name.rpartition(":")
            # The image is loaded into kind by prefetch, so the pod never needs registry credentials.
            tag, pull_policy = f"{version}@{digest}", "IfNotPresent"
        return cls(
            project_code_repository=_env(environ, "PROJECT_CODE_IMAGE_REPOSITORY", repository),
            project_code_tag=_env(environ, "PROJECT_CODE_IMAGE_TAG", tag),
            project_code_pull_policy=_env(environ, "PROJECT_CODE_IMAGE_PULL_POLICY", pull_policy),
            project_code_revision=_env(environ, "PROJECT_CODE_IMAGE_REVISION", "manual"),
            project_code_python_base_image=_env(
                environ,
                "PROJECT_CODE_PYTHON_BASE_IMAGE",
                "python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de",
            ),
            project_code_published=bool(published),
            superset_repository=_env(environ, "SUPERSET_IMAGE_REPOSITORY", "ghcr.io/openlakeforge/superset"),
            superset_tag=_env(environ, "SUPERSET_IMAGE_TAG", "local"),
            superset_build=_truthy(_env(environ, "BUILD_SUPERSET_IMAGE", "false")),
            superset_base_image=_env(
                environ,
                "SUPERSET_BASE_IMAGE",
                "apache/superset:6.1.0@sha256:fb3464528ec7076f91195f0ff7835755aa023e281f1bb78a84782ce7a36b3705",
            ),
            pull_retry=_retry_policy(
                environ, specific_attempts="DOCKER_PULL_ATTEMPTS", specific_delay="DOCKER_PULL_RETRY_DELAY_SECONDS"
            ),
            build_retry=_retry_policy(
                environ, specific_attempts="DOCKER_BUILD_ATTEMPTS", specific_delay="DOCKER_BUILD_RETRY_DELAY_SECONDS"
            ),
            push_retry=_retry_policy(
                environ, specific_attempts="DOCKER_PUSH_ATTEMPTS", specific_delay="DOCKER_PUSH_RETRY_DELAY_SECONDS"
            ),
        )


_ALWAYS_CHARTS = ("trino", "dagster", "seaweedfs", "polaris", "traefik", "cert-manager")
_GOVERNANCE_CHARTS = ("openmetadata", "openmetadata-dependencies")
_ANALYTICS_CHARTS = ("superset",)
_ALL_CHARTS = _ALWAYS_CHARTS + _GOVERNANCE_CHARTS + _ANALYTICS_CHARTS


def _local_chart_names(features: DeploymentFeatures) -> tuple[str, ...]:
    """The charts this topology deploys, minus the ones a disabled optional
    layer will never install - a slim-profile run must not spend time
    downloading and verifying charts it cannot use.
    """
    names = list(_ALWAYS_CHARTS)
    if features.governance_enabled:
        names.extend(_GOVERNANCE_CHARTS)
    if features.analytics_enabled:
        names.extend(_ANALYTICS_CHARTS)
    return tuple(names)


@dataclass(frozen=True)
class ChartSettings:
    """A resolved `ChartSetting` per chart the local provider knows about.

    Generalizes what used to be five Trino-only fields on this class - see
    `olf.deployment.charts.resolve_chart_settings`, which both this and
    `cloud.config.CloudChartSettings` now share.

    `required` names the subset this topology actually deploys, and is what
    `values()` walks: the deploy path must not fetch a chart no enabled stage
    asked for. Settings are resolved for every chart regardless, because
    turning off the last analytics or governance stage does not remove that
    release from Terraform state - the apply that follows still has to destroy
    it, and a `helm_release` pointed at a repository name makes the provider
    fetch that repository's index to do so. Keeping the setting lets
    `platform.cached_chart_variables` hand the destroy the archive already in
    the cache, which is what keeps stage removal working offline.
    """

    settings: Mapping[str, ChartSetting]
    required: frozenset[str]

    def __getitem__(self, name: str) -> ChartSetting:
        return self.settings[name]

    def get(self, name: str) -> ChartSetting | None:
        return self.settings.get(name)

    def values(self) -> Iterable[ChartSetting]:
        """The charts this topology deploys."""
        return tuple(setting for name, setting in self.settings.items() if name in self.required)

    def all_values(self) -> Iterable[ChartSetting]:
        """Every known chart, including optional ones this topology disables."""
        return self.settings.values()

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        helm_cache_dir: Path,
        cache_root: Path,
        catalog_path: Path,
        installed: bool,
        features: DeploymentFeatures,
    ) -> ChartSettings:
        return cls(
            settings=resolve_chart_settings(
                _ALL_CHARTS,
                environ,
                helm_cache_dir=helm_cache_dir,
                cache_root=cache_root,
                catalog_path=catalog_path,
                installed=installed,
            ),
            required=frozenset(_local_chart_names(features)),
        )


@dataclass(frozen=True)
class TerraformSettings:
    var_file: Path | None
    apply_retry: RetryPolicy

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        project_root: Path,
        var_file: Path | None = None,
    ) -> TerraformSettings:
        """There is no platform-owned default var file: which capabilities a
        stage gets is part of the resolved topology `platform_apply_variables`
        already sends. A user-provided `--var-file`/`LOCAL_TFVARS_FILE` is the
        user's own tfvars, which - with `--project-root` - lives in their
        writable project, never inside the read-only distribution payload.
        """
        user_raw = str(var_file) if var_file is not None else environ.get("LOCAL_TFVARS_FILE", "")
        resolved: Path | None = None
        if user_raw:
            candidate = Path(user_raw)
            resolved = candidate if candidate.is_absolute() else project_root / candidate
        return cls(
            var_file=resolved,
            apply_retry=RetryPolicy(
                max_attempts=_int_env(environ, "LOCAL_UP_RETRY_ATTEMPTS", 4),
                delay_seconds=_float_env(environ, "LOCAL_UP_RETRY_DELAY_SECONDS", 20.0),
            ),
        )


@dataclass(frozen=True)
class PrefetchSettings:
    pull_retry: RetryPolicy

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> PrefetchSettings:
        return cls(
            pull_retry=_retry_policy(
                environ,
                specific_attempts="LOCAL_PREFETCH_PULL_ATTEMPTS",
                specific_delay="LOCAL_PREFETCH_PULL_RETRY_DELAY_SECONDS",
                generic_attempts="DOCKER_PULL_ATTEMPTS",
                generic_delay="DOCKER_PULL_RETRY_DELAY_SECONDS",
                default_attempts=5,
                default_delay=20.0,
            ),
        )


def _published_example_image(context: DeploymentContext, environ: Mapping[str, str]) -> str | None:
    """The release-pinned example project image, only when no customer project is configured.

    The catalog of a source checkout does not carry the pin: a release stamps its own digest in when it publishes the
    image (`release.yml`), so that image and the bundled `lakehouse_code/` come from the same commit. A checkout
    without one builds the example locally instead. An explicit `PROJECT_CODE_IMAGE_TAG` is a customer's own build.
    """
    topology = context.topology
    if resolve_project(topology.project, topology.provider, environ).source != "public-example":
        return None
    if environ.get("PROJECT_CODE_IMAGE_TAG"):
        return None
    catalog_path = context.paths.distribution_root / "release/component-catalog.yaml"
    if not catalog_path.is_file():
        return None
    components = (yaml.safe_load(catalog_path.read_text()) or {}).get("components") or {}
    return (components.get("images") or {}).get("project_code")


@dataclass(frozen=True)
class LocalDeploymentConfig:
    context: DeploymentContext
    cluster: ClusterSettings
    images: ImageSettings
    charts: ChartSettings
    terraform: TerraformSettings
    prefetch: PrefetchSettings
    force_foundation_down: bool

    @property
    def paths(self):  # noqa: ANN201 - DeploymentPaths, avoided import cycle noise
        return self.context.paths

    @property
    def namespace(self) -> str:
        return self.context.namespace

    @property
    def kube_context(self) -> str:
        return self.context.kube_context

    @property
    def features(self):  # noqa: ANN201 - DeploymentFeatures
        return self.context.features

    @property
    def platform_features(self):  # noqa: ANN201 - DeploymentFeatures
        return self.context.platform_features

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        context: DeploymentContext,
        var_file: Path | None = None,
    ) -> LocalDeploymentConfig:
        distribution_root = context.paths.distribution_root
        cluster_name = context.kube_context.removeprefix("kind-") or context.kube_context
        return cls(
            context=context,
            cluster=ClusterSettings.from_environment(environ, repo_root=distribution_root, cluster_name=cluster_name),
            images=ImageSettings.from_environment(environ, published=_published_example_image(context, environ)),
            charts=ChartSettings.from_environment(
                environ,
                helm_cache_dir=context.paths.helm_cache_dir,
                cache_root=context.paths.cache_root,
                catalog_path=context.paths.distribution_root / "release/component-catalog.yaml",
                installed=context.paths.installed,
                # Charts are a platform-wide input: one chart archive serves
                # every stage that enables its capability, so a slim DEV
                # alongside a full PROD must still fetch Superset.
                features=context.platform_features,
            ),
            terraform=TerraformSettings.from_environment(
                environ,
                project_root=context.paths.repo_root,
                var_file=var_file,
            ),
            prefetch=PrefetchSettings.from_environment(environ),
            force_foundation_down=_truthy(_env(environ, "LOCAL_FOUNDATION_FORCE_DOWN", "false")),
        )
