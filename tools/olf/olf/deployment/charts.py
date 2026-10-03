"""Provider-neutral Helm chart cache management.

Port of `scripts/lib/helm.sh::prepare_cached_chart` and
`prepare_cached_dagster_chart_no_schema`. Kept out of `olf.deployment.local`
so the AWS/Azure providers (#125) reuse both verbatim - the Dagster chart
ships a `values.schema.json` that rejects the OpenLakeForge values overlay,
so the cached package is re-packed without it, exactly as the shell helper
did.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tarfile
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from olf import log
from olf.deployment.context import DeploymentPaths
from olf.deployment.env_settings import env as _env_lookup
from olf.deployment.retry import RetryPolicy
from olf.tooling.helm import Helm


@dataclass(frozen=True)
class ChartRequest:
    display_name: str
    repo_name: str
    repo_url: str
    chart_ref: str
    version: str
    package_path: Path
    sha256: str | None = None


@dataclass(frozen=True)
class CatalogChart:
    """An immutable third-party chart declaration from the component catalog."""

    name: str
    repository: str
    reference: str
    version: str
    sha256: str

    @classmethod
    def load(cls, catalog_path: Path, name: str) -> CatalogChart:
        try:
            catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
            value = catalog["components"]["helm"]["charts"][name]
        except (KeyError, OSError, TypeError, yaml.YAMLError) as exc:
            raise ValueError(f"chart {name!r} is not declared in {catalog_path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"chart {name!r} in {catalog_path} must be a mapping")
        fields: dict[str, str] = {}
        for field in ("repository", "reference", "version", "sha256"):
            item = value.get(field)
            if not isinstance(item, str) or not item:
                raise ValueError(f"chart {name!r} in {catalog_path} has incomplete immutable metadata")
            fields[field] = item
        digest = fields["sha256"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"chart {name!r} in {catalog_path} has an invalid SHA-256")
        return cls(name=name, **fields)

    def request(self, *, cache_root: Path, variant: str | None = None) -> ChartRequest:
        """`variant` disambiguates a chart's cached package name when the
        cache holds more than one derivative of the same upstream archive -
        Dagster's schema-stripped repack lives beside (never overwrites) a
        cache keyed only by the pristine upstream digest.
        """
        filename = f"{self.sha256}-{variant}.tgz" if variant else f"{self.sha256}.tgz"
        return ChartRequest(
            display_name=self.name,
            repo_name=self.reference.split("/", 1)[0],
            repo_url=self.repository,
            chart_ref=self.reference,
            version=self.version,
            package_path=cache_root / "helm" / filename,
            sha256=self.sha256,
        )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def _chart_cache_lock(request: ChartRequest) -> Iterator[None]:
    """Serialize one chart's shared-cache lifecycle across providers.

    Installed local, AWS, and Azure deployments deliberately share
    ``OLF_HOME/cache/helm``. Helm writes its downloaded archive with a
    chart/version filename before OpenLakeForge verifies and activates the
    content-addressed package, so locking only the final rename would still
    leave concurrent pulls and repacks racing. The per-package lock covers
    cache validation, download, verification, and publication while allowing
    unrelated charts to prepare concurrently.
    """
    import fcntl

    request.package_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = request.package_path.with_name(f".{request.package_path.name}.lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare_cached_chart(
    request: ChartRequest,
    *,
    helm: Helm,
    paths: DeploymentPaths,
    env: Mapping[str, str],
    retry_policy: RetryPolicy,
) -> Path:
    with _chart_cache_lock(request):
        return _prepare_cached_chart(request, helm=helm, paths=paths, env=env, retry_policy=retry_policy)


def _prepare_cached_chart(
    request: ChartRequest,
    *,
    helm: Helm,
    paths: DeploymentPaths,
    env: Mapping[str, str],
    retry_policy: RetryPolicy,
) -> Path:
    repository_config = paths.helm_repository_config
    repository_cache = paths.helm_repository_cache

    cache_has_expected_digest = request.package_path.is_file() and (
        request.sha256 is None or _digest(request.package_path) == request.sha256
    )
    cache_has_valid_chart = cache_has_expected_digest and helm.show_chart(
        request.package_path,
        repository_config=repository_config,
        repository_cache=repository_cache,
        env=env,
    ).ok
    if cache_has_valid_chart:
        log.step(f"Using cached {request.display_name} Helm chart: {request.package_path}")
        return request.package_path

    request.package_path.unlink(missing_ok=True)

    log.step(f"Downloading {request.display_name} Helm chart {request.version} into local cache...")
    helm.repo_add(
        request.repo_name,
        request.repo_url,
        force_update=True,
        repository_config=repository_config,
        repository_cache=repository_cache,
        env=env,
        retry_policy=retry_policy,
    )
    helm.repo_update(
        repository_config=repository_config,
        repository_cache=repository_cache,
        env=env,
        retry_policy=retry_policy,
    )
    helm.pull(
        request.chart_ref,
        version=request.version,
        destination=paths.helm_cache_dir,
        repository_config=repository_config,
        repository_cache=repository_cache,
        env=env,
        retry_policy=retry_policy,
    )
    if request.sha256 is None:
        return request.package_path

    chart_name = request.chart_ref.rsplit("/", 1)[-1]
    downloaded = paths.helm_cache_dir / f"{chart_name}-{request.version}.tgz"
    if not downloaded.is_file() or _digest(downloaded) != request.sha256:
        downloaded.unlink(missing_ok=True)
        raise ValueError(f"{request.display_name} chart digest does not match the component catalog")
    request.package_path.parent.mkdir(parents=True, exist_ok=True)
    staged = request.package_path.with_name(f".{request.sha256}.{os.getpid()}.tgz")
    downloaded.replace(staged)
    os.replace(staged, request.package_path)
    return request.package_path


def _derivative_digest_sidecar(package_path: Path) -> Path:
    return package_path.with_name(package_path.name + ".sha256")


def _cached_derivative_is_trustworthy(package_path: Path) -> bool:
    """A repacked derivative's own digest can never match the catalog pin
    (its content changed by stripping the schema), so cache-hit integrity
    is checked against a digest sidecar this function itself wrote when it
    created the file - not the catalog pin. Without this, a corrupted or
    replaced-but-still-Helm-parseable cached file in the writable shared
    cache would be reused indefinitely, since `helm show chart` only checks
    structural validity, not content.
    """
    sidecar = _derivative_digest_sidecar(package_path)
    try:
        expected = sidecar.read_text().strip()
    except OSError:
        return False
    return bool(expected) and _digest(package_path) == expected


def prepare_cached_dagster_chart_no_schema(
    request: ChartRequest,
    *,
    helm: Helm,
    paths: DeploymentPaths,
    env: Mapping[str, str],
    retry_policy: RetryPolicy,
) -> Path:
    with _chart_cache_lock(request):
        return _prepare_cached_dagster_chart_no_schema(
            request, helm=helm, paths=paths, env=env, retry_policy=retry_policy
        )


def _prepare_cached_dagster_chart_no_schema(
    request: ChartRequest,
    *,
    helm: Helm,
    paths: DeploymentPaths,
    env: Mapping[str, str],
    retry_policy: RetryPolicy,
) -> Path:
    """Cache the Dagster Helm chart with `values.schema.json` stripped.

    Port of `scripts/lib/helm.sh::prepare_cached_dagster_chart_no_schema`: the
    upstream Dagster chart ships a schema that rejects the OpenLakeForge
    values overlay, so the cached package is untarred, has every
    `values.schema.json` deleted, and is re-packaged under `request.
    package_path` (distinct from the plain `helm pull` output name, since
    that name collides with `prepare_cached_chart`'s Dagster-less callers).

    When `request.sha256` is set (an installed distribution's catalog-pinned
    chart), the downloaded archive is verified against it *before* being
    unpacked and re-packaged - the repacked artifact's own digest can never
    match the catalog pin (its content changed), so this is the only point
    where the upstream chart's integrity can be checked at all. The
    transformed package itself is then re-verified on every future cache hit
    against a digest sidecar recorded when it was created (see
    `_cached_derivative_is_trustworthy`).
    """
    repository_config = paths.helm_repository_config
    repository_cache = paths.helm_repository_cache
    digest_sidecar = _derivative_digest_sidecar(request.package_path)

    cache_is_trustworthy = (
        request.package_path.is_file()
        and (request.sha256 is None or _cached_derivative_is_trustworthy(request.package_path))
        and helm.show_chart(
            request.package_path, repository_config=repository_config, repository_cache=repository_cache, env=env
        ).ok
    )
    if cache_is_trustworthy:
        log.step(f"Using cached Dagster Helm chart: {request.package_path}")
        return request.package_path

    request.package_path.unlink(missing_ok=True)
    digest_sidecar.unlink(missing_ok=True)

    chart_name = request.chart_ref.rsplit("/", 1)[-1]
    paths.work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dagster-chart.", dir=paths.work_root) as work_dir_str:
        work_dir = Path(work_dir_str)

        log.step(f"Downloading Dagster Helm chart {request.version} into local cache...")
        helm.repo_add(
            request.repo_name,
            request.repo_url,
            force_update=True,
            repository_config=repository_config,
            repository_cache=repository_cache,
            env=env,
            retry_policy=retry_policy,
        )
        helm.repo_update(
            repository_config=repository_config,
            repository_cache=repository_cache,
            env=env,
            retry_policy=retry_policy,
        )
        helm.pull(
            request.chart_ref,
            version=request.version,
            destination=work_dir,
            repository_config=repository_config,
            repository_cache=repository_cache,
            env=env,
            retry_policy=retry_policy,
        )

        downloaded = work_dir / f"{chart_name}-{request.version}.tgz"
        if request.sha256 is not None and (not downloaded.is_file() or _digest(downloaded) != request.sha256):
            raise ValueError(f"{request.display_name} chart digest does not match the component catalog")

        chart_dir = work_dir / chart_name
        with tarfile.open(downloaded, "r:gz") as bundle:
            bundle.extractall(work_dir, filter="data")
        for schema_file in chart_dir.rglob("values.schema.json"):
            schema_file.unlink()

        paths.helm_cache_dir.mkdir(parents=True, exist_ok=True)
        helm.package(chart_dir, destination=paths.helm_cache_dir, env=env)
        packaged_path = paths.helm_cache_dir / f"{chart_name}-{request.version}.tgz"
        request.package_path.parent.mkdir(parents=True, exist_ok=True)
        packaged_path.rename(request.package_path)
        if request.sha256 is not None:
            digest_sidecar.write_text(_digest(request.package_path) + "\n")

    return request.package_path
@dataclass(frozen=True)
class ChartDefault:
    """A catalog chart's source-mode fallback (no catalog entry, or a
    non-installed run) plus how it must be prepared.

    `chart_ref` is the repo-relative reference Helm resolves once the repo
    named `name.split("/", 1)[0]`-equivalent is added (`<repo>/<chart>`,
    matching `CatalogChart.reference`); `repository`/`version` are the
    literal defaults every provider config used to hardcode per chart before
    this table existed.
    """

    chart_ref: str
    repository: str
    version: str
    repack: bool = False
    variant: str | None = None


CHART_DEFAULTS: Mapping[str, ChartDefault] = {
    "trino": ChartDefault(chart_ref="trino/trino", repository="https://trinodb.github.io/charts", version="1.42.2"),
    "dagster": ChartDefault(
        chart_ref="dagster/dagster",
        repository="https://dagster-io.github.io/helm",
        version="1.13.7",
        repack=True,
        variant="no-schema",
    ),
    "seaweedfs": ChartDefault(
        chart_ref="seaweedfs/seaweedfs",
        repository="https://seaweedfs.github.io/seaweedfs/helm",
        version="4.23.0",
    ),
    "polaris": ChartDefault(
        chart_ref="polaris/polaris",
        repository="https://downloads.apache.org/polaris/helm-chart",
        version="1.4.1",
    ),
    "openmetadata": ChartDefault(
        chart_ref="open-metadata/openmetadata",
        repository="https://helm.open-metadata.org",
        version="1.13.6",
    ),
    "openmetadata-dependencies": ChartDefault(
        chart_ref="open-metadata/openmetadata-dependencies",
        repository="https://helm.open-metadata.org",
        version="1.13.6",
    ),
    "superset": ChartDefault(
        chart_ref="superset/superset",
        repository="https://apache.github.io/superset/",
        version="0.15.5",
    ),
}


def _env_slug(name: str) -> str:
    return name.upper().replace("-", "_")


TERRAFORM_VARIABLE_KEY: Mapping[str, str] = {
    "trino": "trino_chart_package_path",
    "dagster": "dagster_chart_package_path",
    "seaweedfs": "seaweedfs_chart_package_path",
    "polaris": "polaris_chart_package_path",
    "openmetadata": "openmetadata_chart_package_path",
    "openmetadata-dependencies": "openmetadata_deps_chart_package_path",
    "superset": "superset_chart_package_path",
}
"""Maps a catalog chart name to the Terraform variable each environment root
declares for it. `openmetadata-dependencies` breaks the `<name>_chart_package_path`
pattern because its Terraform module variable is `deps_chart_package_path`
(paired with the module's existing `deps_chart_version`, see
`modules/governance/openmetadata/variables.tf`), not a name derived from the
catalog key.
"""


@dataclass(frozen=True)
class ChartSetting:
    """A resolved, provider-agnostic chart ready for `prepare_chart`."""

    name: str
    repository_url: str
    chart_ref: str
    version: str
    package_path: Path
    sha256: str | None
    repack: bool
    variant: str | None

    def request(self) -> ChartRequest:
        return ChartRequest(
            display_name=self.name,
            repo_name=self.chart_ref.split("/", 1)[0],
            repo_url=self.repository_url,
            chart_ref=self.chart_ref,
            version=self.version,
            package_path=self.package_path,
            sha256=self.sha256,
        )


def resolve_chart_setting(
    name: str,
    environ: Mapping[str, str],
    *,
    helm_cache_dir: Path,
    cache_root: Path,
    catalog_path: Path,
    installed: bool,
) -> ChartSetting:
    """Resolve one catalog chart's settings, mirroring the `${VAR:-default}`
    fallback chain `local.config.ChartSettings`/`cloud.config.CloudChartSettings`
    each duplicated per chart before this helper existed.
    """
    default = CHART_DEFAULTS[name]
    slug = _env_slug(name)
    catalog_chart = CatalogChart.load(catalog_path, name) if catalog_path.is_file() else None
    version = _env_lookup(environ, f"{slug}_CHART_VERSION", catalog_chart.version if catalog_chart else default.version)
    fallback_filename = f"{name}-{version}-{default.variant}.tgz" if default.variant else f"{name}-{version}.tgz"
    default_package_path = (
        catalog_chart.request(cache_root=cache_root, variant=default.variant).package_path
        if installed and catalog_chart is not None
        else helm_cache_dir / fallback_filename
    )
    fallback_repository = catalog_chart.repository if catalog_chart is not None else default.repository
    return ChartSetting(
        name=name,
        repository_url=_env_lookup(environ, f"{slug}_CHART_REPOSITORY", fallback_repository),
        chart_ref=default.chart_ref,
        version=version,
        package_path=Path(_env_lookup(environ, f"{slug}_CHART_PACKAGE_PATH", str(default_package_path))),
        sha256=catalog_chart.sha256 if installed and catalog_chart is not None else None,
        repack=default.repack,
        variant=default.variant,
    )


def resolve_chart_settings(
    names: Iterable[str],
    environ: Mapping[str, str],
    *,
    helm_cache_dir: Path,
    cache_root: Path,
    catalog_path: Path,
    installed: bool,
) -> dict[str, ChartSetting]:
    return {
        name: resolve_chart_setting(
            name,
            environ,
            helm_cache_dir=helm_cache_dir,
            cache_root=cache_root,
            catalog_path=catalog_path,
            installed=installed,
        )
        for name in names
    }


def prepare_chart(
    setting: ChartSetting,
    *,
    helm: Helm,
    paths: DeploymentPaths,
    env: Mapping[str, str],
    retry_policy: RetryPolicy,
) -> Path:
    """Cache and verify one resolved chart, dispatching to the schema-stripping
    repack only for charts that declare it (currently Dagster alone)."""
    request = setting.request()
    if setting.repack:
        return prepare_cached_dagster_chart_no_schema(
            request, helm=helm, paths=paths, env=env, retry_policy=retry_policy
        )
    return prepare_cached_chart(request, helm=helm, paths=paths, env=env, retry_policy=retry_policy)
