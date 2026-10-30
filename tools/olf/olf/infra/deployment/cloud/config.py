"""Typed cloud-provider (AWS/Azure) deployment configuration.

Mirrors `olf.common.deployment.local.config`: plain frozen dataclasses with
`from_environment` classmethods built on the same `${VAR:-default}`
fallback semantics (`olf.common.deployment.env_settings`). Only the genuinely
provider-neutral settings live here - project-code image
selection, Helm chart caching, the platform Terraform var-file/retry
policy. Foundation-specific settings (AWS
region/node sizing/instance types; Azure node count/ACR name prefix) stay
in `cloud/aws.py`/`cloud/azure.py`, matching the shell scripts' own
provider split.

`Profile` (not the shell's ad hoc `ENABLE_GOVERNANCE`/`ENABLE_ANALYTICS`
variables) drives Full/Slim behavior here too, consistent with the
precedent ADR 0008 set for the local provider.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from olf.common.deployment.charts import ChartSetting, resolve_chart_settings
from olf.common.deployment.context import DeploymentContext, DeploymentFeatures
from olf.common.deployment.env_settings import env as _env
from olf.common.deployment.env_settings import retry_policy as _retry_policy
from olf.common.deployment.env_settings import truthy as _truthy
from olf.common.deployment.project_code import published_example_image, split_published_image
from olf.common.tooling.retry import RetryPolicy

_DEFAULT_PYTHON_BASE_IMAGE_DIGEST = "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
_DEFAULT_PYTHON_BASE_IMAGE = {
    "aws": f"public.ecr.aws/docker/library/python:3.12-slim@{_DEFAULT_PYTHON_BASE_IMAGE_DIGEST}",
    "azure": f"python:3.12-slim@{_DEFAULT_PYTHON_BASE_IMAGE_DIGEST}",
}


def _git_sha_tag(repo_root: Path) -> str | None:
    """Port of `scripts/lib/common.sh::git_or_time_tag`'s git branch, made
    optional so callers can try other stable sources before resorting to a
    timestamp."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _distribution_identity_tag(distribution_root: Path) -> str | None:
    """A stable tag derived from an installed distribution's own
    content-addressed path (`DistributionManager.payload_root` -
    `OLF_HOME/distributions/<version>/<sha256>/payload`), or `None` outside
    that layout (source checkouts, or a distribution_root that happens not
    to look like it).
    """
    sha256_dir = distribution_root.parent.name
    if len(sha256_dir) == 64 and all(char in "0123456789abcdef" for char in sha256_dir):
        return sha256_dir[:12]
    return None


def default_image_tag(repo_root: Path, *, scope: str, distribution_root: Path | None = None) -> str:
    """Resolve the fallback image tag used when `{SCOPE}_IMAGE_TAG` is unset.

    Prefers `repo_root`'s git SHA. Falls back to a tag derived from
    `distribution_root`'s content-addressed payload path when given and
    installed - stable across the separate `olf deploy` invocations one
    `--phase platform` then `--phase artifacts` run makes, unlike the
    wall-clock timestamp last resort: a fresh timestamp on every invocation
    left the platform phase configuring Dagster with one tag while the
    artifacts phase built and pushed a different one, so the code server
    could never pull the image actually pushed. Only a genuinely non-git,
    non-installed project root (a source-mode non-git checkout) reaches the
    timestamp.
    """
    git_tag = _git_sha_tag(repo_root)
    if git_tag is not None:
        return f"{scope}-{git_tag}"
    if distribution_root is not None:
        stable_tag = _distribution_identity_tag(distribution_root)
        if stable_tag is not None:
            return f"{scope}-{stable_tag}"
    return f"{scope}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"


def _image_env(environ: Mapping[str, str], *, scope: str, name: str, default: str) -> str:
    """Resolve generic image settings before their legacy provider-prefixed aliases."""
    return _env(environ, name, _env(environ, f"{scope.upper()}_{name}", default))


@dataclass(frozen=True)
class CloudImageSettings:
    project_code_repository: str
    project_code_tag: str
    project_code_pull_policy: str
    project_code_revision: str
    project_code_python_base_image: str
    image_platform: str
    pull_retry: RetryPolicy
    build_retry: RetryPolicy
    push_retry: RetryPolicy
    # True when project_code_* is the release-published example image: nothing to build or push.
    project_code_published: bool = False

    @property
    def project_code_image(self) -> str:
        return f"{self.project_code_repository}:{self.project_code_tag}"

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str], *, scope: str, image_tag: str, published: str | None = None
    ) -> CloudImageSettings:
        """Build settings for `scope` ("aws" or "azure").

        `project_code_repository` is empty when not explicitly
        overridden via `PROJECT_CODE_IMAGE_REPOSITORY` - `CloudProvider` fills in the
        foundation-derived registry (ECR/ACR) once it resolves, mirroring
        the shell scripts' `${VAR:-$(terraform output ...)}` fallback.

        `published`: a `repository:version@sha256:...` example image to deploy as it is instead of building and
        pushing one to the provider registry. The cluster pulls it from the public registry, so no pull is repeated
        once a node has it.
        """
        repository, tag, pull_policy = "", image_tag, "Always"
        if published:
            repository, tag = split_published_image(published)
            pull_policy = "IfNotPresent"
        return cls(
            project_code_repository=_image_env(
                environ, scope=scope, name="PROJECT_CODE_IMAGE_REPOSITORY", default=repository
            ),
            project_code_tag=_image_env(environ, scope=scope, name="PROJECT_CODE_IMAGE_TAG", default=tag),
            project_code_pull_policy=_image_env(
                environ, scope=scope, name="PROJECT_CODE_IMAGE_PULL_POLICY", default=pull_policy
            ),
            project_code_revision=_image_env(
                environ, scope=scope, name="PROJECT_CODE_IMAGE_REVISION", default="manual"
            ),
            project_code_python_base_image=_image_env(
                environ,
                scope=scope,
                name="PROJECT_CODE_PYTHON_BASE_IMAGE",
                default=_DEFAULT_PYTHON_BASE_IMAGE[scope],
            ),
            image_platform=_env(environ, f"{scope.upper()}_IMAGE_PLATFORM", "linux/amd64"),
            pull_retry=_retry_policy(
                environ, specific_attempts="DOCKER_PULL_ATTEMPTS", specific_delay="DOCKER_PULL_RETRY_DELAY_SECONDS"
            ),
            build_retry=_retry_policy(
                environ, specific_attempts="DOCKER_BUILD_ATTEMPTS", specific_delay="DOCKER_BUILD_RETRY_DELAY_SECONDS"
            ),
            push_retry=_retry_policy(
                environ, specific_attempts="DOCKER_PUSH_ATTEMPTS", specific_delay="DOCKER_PUSH_RETRY_DELAY_SECONDS"
            ),
            project_code_published=bool(published),
        )


_CLOUD_ALWAYS_CHARTS = ("trino", "dagster")
_CLOUD_IN_CLUSTER_STORAGE_CHARTS = ("seaweedfs", "polaris")
_CLOUD_GOVERNANCE_CHARTS = ("openmetadata", "openmetadata-dependencies")
_CLOUD_ANALYTICS_CHARTS = ("superset",)


def _cloud_chart_names(scope: str, features: DeploymentFeatures) -> tuple[str, ...]:
    """Every chart a given cloud scope deploys.

    AWS replaces SeaweedFS/Polaris with S3/Glue, so it never installs those
    two charts at all - unlike Azure, which (like local) runs both
    in-cluster. Governance/analytics follow the same optional-layer gating
    as the local provider.
    """
    names = list(_CLOUD_ALWAYS_CHARTS)
    if scope != "aws":
        names.extend(_CLOUD_IN_CLUSTER_STORAGE_CHARTS)
    if features.governance_enabled:
        names.extend(_CLOUD_GOVERNANCE_CHARTS)
    if features.analytics_enabled:
        names.extend(_CLOUD_ANALYTICS_CHARTS)
    return tuple(names)


@dataclass(frozen=True)
class CloudChartSettings:
    """A resolved `ChartSetting` per chart a given cloud scope deploys.

    Generalizes what used to be ten Trino/Dagster-only fields on this class -
    see `olf.common.deployment.charts.resolve_chart_settings`, which both this and
    `local.config.ChartSettings` now share.
    """

    settings: Mapping[str, ChartSetting]

    def __getitem__(self, name: str) -> ChartSetting:
        return self.settings[name]

    def get(self, name: str) -> ChartSetting | None:
        return self.settings.get(name)

    def values(self) -> Iterable[ChartSetting]:
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
        scope: str,
        features: DeploymentFeatures,
    ) -> CloudChartSettings:
        """An installed distribution pins each chart's digest from the
        component catalog, so a downloaded archive is verified before use
        exactly as the local provider's charts already are
        (`prepare_cached_chart`/`prepare_cached_dagster_chart_no_schema`).
        Source-mode runs (`installed=False`) keep resolving purely from each
        chart's `<SLUG>_CHART_VERSION` override, with `sha256=None`.
        """
        return cls(
            settings=resolve_chart_settings(
                _cloud_chart_names(scope, features),
                environ,
                helm_cache_dir=helm_cache_dir,
                cache_root=cache_root,
                catalog_path=catalog_path,
                installed=installed,
            )
        )


@dataclass(frozen=True)
class CloudTerraformSettings:
    """The platform Terraform apply's var-file and retry policy.

    Foundation-level tfvars handling (AWS: optional; Azure: required) is a
    genuine provider difference and stays in `cloud/aws.py`/`cloud/azure.py`
    - `var_file` only covers the platform apply, which AWS var-files and
    Azure does not (Azure's `stack/platform-up.sh` never references a
    tfvars file at all; ADR 0008 makes this a binding requirement, not an
    incidental default).

    `foundation_var_file` is the separate channel an explicit `--var-file`
    CLI override travels through for Azure foundation operations: it must
    never also populate `var_file`, or a combined `olf deploy --provider
    azure --var-file <foundation.tfvars>` run would forward that file into
    the platform apply too and fail Terraform's `-var-file` validation
    there (Azure's platform root declares none of the foundation-only
    variables a foundation tfvars file sets). AWS has no equivalent field -
    it deliberately reuses `var_file` for both phases (see
    `cloud/aws.py::foundation_tfvars_file`'s docstring).
    """

    var_file: Path | None
    apply_retry: RetryPolicy
    foundation_var_file: Path | None = None

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str], *, repo_root: Path, platform_terraform_dir: Path, scope: str
    ) -> CloudTerraformSettings:
        resolved: Path | None = None
        if scope == "aws":
            # AWS's stack/platform-up.sh reuses the same AWS_TFVARS_FILE (mandatory
            # account tags) as the foundation apply - unlike Azure, whose platform
            # Terraform root declares no foundation-only variables at all
            # (resource_group_name, location, node_vm_size, ...) and would fail
            # `-var-file` validation if AZURE_TFVARS_FILE were passed to it.
            raw = environ.get("AWS_TFVARS_FILE") or ""
            if raw:
                candidate = Path(raw)
                resolved = candidate if candidate.is_absolute() else repo_root / candidate
            else:
                default_tfvars = platform_terraform_dir / "sandbox.tfvars"
                resolved = default_tfvars if default_tfvars.is_file() else None
        return cls(
            var_file=resolved,
            apply_retry=RetryPolicy(
                max_attempts=int(_env(environ, f"{scope.upper()}_UP_RETRY_ATTEMPTS", "4")),
                delay_seconds=float(_env(environ, f"{scope.upper()}_UP_RETRY_DELAY_SECONDS", "20.0")),
            ),
        )


@dataclass(frozen=True)
class CloudDeploymentConfig:
    context: DeploymentContext
    images: CloudImageSettings
    charts: CloudChartSettings
    terraform: CloudTerraformSettings
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

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        context: DeploymentContext,
        var_file: Path | None = None,
    ) -> CloudDeploymentConfig:
        scope = context.provider.value
        project_root = context.paths.repo_root
        distribution_root = context.paths.distribution_root
        raw_tag = environ.get(f"{scope.upper()}_IMAGE_TAG")
        tag = raw_tag if raw_tag else default_image_tag(project_root, scope=scope, distribution_root=distribution_root)
        terraform = CloudTerraformSettings.from_environment(
            environ,
            repo_root=project_root,
            platform_terraform_dir=context.paths.platform_terraform_dir,
            scope=scope,
        )
        if var_file is not None:
            # Terraform runs with `-chdir=<foundation-or-platform-root>`, so
            # a relative `--var-file` would resolve beneath whichever
            # Terraform root happens to run first (AWS reuses this override
            # across two different roots), not beneath either root proper.
            # Normalize against project_root, not distribution_root: the
            # tfvars file is the user's own account/tag configuration, which
            # for an installed deployment with --project-root lives in their
            # writable project, never inside the read-only distribution
            # payload. Matches `AWS_TFVARS_FILE`'s resolution just above.
            if not var_file.is_absolute():
                var_file = project_root / var_file
            # AWS reuses the same explicit override for both foundation and
            # platform (var_file); Azure's platform apply must NEVER see a
            # tfvars file (ADR 0008), so the override travels through
            # foundation_var_file only, leaving var_file untouched (None).
            if scope == "aws":
                terraform = CloudTerraformSettings(
                    var_file=var_file, apply_retry=terraform.apply_retry, foundation_var_file=var_file
                )
            else:
                terraform = CloudTerraformSettings(
                    var_file=terraform.var_file, apply_retry=terraform.apply_retry, foundation_var_file=var_file
                )
        return cls(
            context=context,
            images=CloudImageSettings.from_environment(
                environ,
                scope=scope,
                image_tag=tag,
                published=published_example_image(context, environ)
                if not environ.get(f"{scope.upper()}_PROJECT_CODE_IMAGE_TAG")
                # The release image is amd64 only; any other requested platform is built.
                and _env(environ, f"{scope.upper()}_IMAGE_PLATFORM", "linux/amd64") == "linux/amd64"
                else None,
            ),
            charts=CloudChartSettings.from_environment(
                environ,
                helm_cache_dir=context.paths.helm_cache_dir,
                cache_root=context.paths.cache_root,
                catalog_path=context.paths.distribution_root / "release/component-catalog.yaml",
                installed=context.paths.installed,
                scope=scope,
                features=context.platform_features,
            ),
            terraform=terraform,
            force_foundation_down=_truthy(_env(environ, f"{scope.upper()}_FOUNDATION_FORCE_DOWN", "false")),
        )
