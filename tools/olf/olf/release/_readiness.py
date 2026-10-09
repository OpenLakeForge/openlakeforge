"""Release-readiness gate ("olf release check").

Owns the pin/drift checks that catch a release from being cut with an
unpinned image, an un-cataloged workflow action, or Terraform/Helm
inputs that have drifted from `release/component-catalog.yaml`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from olf.release._compatibility import (
    _check_compatibility_matrix_up_to_date,
    _check_provider_using_terraform_roots_have_lockfiles,
    _check_terraform_required_versions_match_catalog,
    _helm_chart_versions_from_terraform_modules,
)
from olf.release._manifest import (
    ACTION_SHA_PATTERN,
    DEFAULT_CATALOG_PATH,
    IMAGE_DIGEST_SUFFIX_PATTERN,
    CheckResult,
    ReleaseCheckReport,
    catalog_version,
    load_catalog,
    tag_for_version,
)

_IMAGE_TAG_DIGEST_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")


def _check_version_matches_tag(catalog: dict[str, Any], tag: str | None) -> CheckResult:
    version = catalog_version(catalog)
    if tag is None:
        return CheckResult("catalog version is valid alpha semver", True, version)
    expected_tag = tag_for_version(version)
    if tag != expected_tag:
        return CheckResult(
            "tag matches catalog distribution.version",
            False,
            f"tag {tag!r} does not match expected {expected_tag!r} (catalog version {version!r})",
        )
    return CheckResult("tag matches catalog distribution.version", True, f"{tag} == {expected_tag}")


_TOOLCHAIN_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOOLCHAIN_PLATFORMS = ("darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64")
_CHART_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ALPHA_PATTERN = re.compile(r"^(\d+\.\d+\.\d+)-alpha\.(\d+)$")


def _pep440_release_version(version: str) -> str:
    match = _RELEASE_ALPHA_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported release version {version!r}")
    return f"{match.group(1)}a{match.group(2)}"


def _check_python_package_versions(repo_root: Path, catalog: dict[str, Any]) -> CheckResult:
    """The two published distributions and catalog release are one version contract."""
    expected = _pep440_release_version(catalog_version(catalog))
    paths = {
        "openlakeforge": repo_root / "tools/olf/pyproject.toml",
        "openlakeforge-domain-model": repo_root / "packages/domain-model/pyproject.toml",
    }
    problems: list[str] = []
    for name, path in paths.items():
        try:
            project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        except (KeyError, OSError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{path.relative_to(repo_root)} is unreadable: {exc}")
            continue
        if project.get("name") != name:
            problems.append(f"{path.relative_to(repo_root)} names {project.get('name')!r}, expected {name!r}")
        if project.get("version") != expected:
            problems.append(f"{path.relative_to(repo_root)} version {project.get('version')!r}, expected {expected!r}")
    if problems:
        return CheckResult("Python package versions match catalog", False, "; ".join(problems))
    return CheckResult("Python package versions match catalog", True, expected)


def _check_catalog_charts(repo_root: Path, catalog: dict[str, Any]) -> CheckResult:
    """Every deployed chart has an immutable catalog entry matching Terraform."""
    charts = ((catalog.get("components") or {}).get("helm") or {}).get("charts")
    module_versions = _helm_chart_versions_from_terraform_modules(repo_root)
    required = frozenset(module_versions)
    if not isinstance(charts, dict):
        return CheckResult("catalog Helm charts are immutable", False, "components.helm.charts is missing")
    problems: list[str] = []
    for name in sorted(required):
        entry = charts.get(name)
        if not isinstance(entry, dict):
            problems.append(f"{name} is missing")
            continue
        version = entry.get("version")
        if version != module_versions[name]:
            problems.append(f"{name}.version={version!r}, Terraform={module_versions[name]!r}")
        for field in ("repository", "reference"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                problems.append(f"{name}.{field} is missing")
        if not _CHART_SHA256_PATTERN.fullmatch(str(entry.get("sha256", ""))):
            problems.append(f"{name}.sha256 is not a SHA-256")
    unexpected = sorted(set(charts) - required)
    if unexpected:
        problems.append(f"unknown chart entries: {', '.join(unexpected)}")
    if problems:
        return CheckResult("catalog Helm charts are immutable", False, "; ".join(problems))
    return CheckResult("catalog Helm charts are immutable", True, f"{len(required)} chart(s) checked")


def _check_toolchain_pinned(catalog: dict[str, Any]) -> CheckResult:
    """Every managed tool (#127) has a concrete version - never `latest` -
    and a well-formed digest for every supported platform."""
    from olf.toolchain.spec import MANAGED_TOOLS

    toolchain = (catalog.get("components") or {}).get("toolchain")
    if not isinstance(toolchain, dict):
        return CheckResult("managed toolchain is pinned", False, "components.toolchain is missing")

    problems: list[str] = []
    missing_tools = [tool for tool in MANAGED_TOOLS if tool not in toolchain]
    if missing_tools:
        problems.append(f"missing entries: {', '.join(missing_tools)}")

    for tool in MANAGED_TOOLS:
        entry = toolchain.get(tool)
        if tool in missing_tools:
            continue
        if not isinstance(entry, dict):
            problems.append(f"{tool} entry must be a mapping, got {entry!r}")
            continue
        version = entry.get("version")
        if not isinstance(version, str) or not version or version == "latest":
            problems.append(f"{tool}.version must be a concrete version, got {version!r}")
        platforms = entry.get("platforms")
        if not isinstance(platforms, dict):
            problems.append(f"{tool}.platforms must be a mapping")
            continue
        missing_platforms = [p for p in _TOOLCHAIN_PLATFORMS if p not in platforms]
        if missing_platforms:
            problems.append(f"{tool}.platforms is missing {', '.join(missing_platforms)}")
        malformed = sorted(
            p for p, digest in platforms.items() if not _TOOLCHAIN_SHA256_PATTERN.match(str(digest))
        )
        if malformed:
            problems.append(f"{tool}.platforms has malformed digest(s) for {', '.join(malformed)}")

    if problems:
        return CheckResult("managed toolchain is pinned", False, "; ".join(problems))
    return CheckResult("managed toolchain is pinned", True, f"{len(MANAGED_TOOLS)} tool(s) checked")


def _check_images_digest_pinned(catalog: dict[str, Any]) -> CheckResult:
    images = (catalog.get("components") or {}).get("images") or {}
    if not images:
        return CheckResult("catalog images are digest-pinned", False, "no images declared in catalog")
    unpinned = [name for name, ref in images.items() if not IMAGE_DIGEST_SUFFIX_PATTERN.search(str(ref))]
    if unpinned:
        return CheckResult(
            "catalog images are digest-pinned", False, f"missing @sha256 digest: {', '.join(sorted(unpinned))}"
        )
    return CheckResult("catalog images are digest-pinned", True, f"{len(images)} image(s) checked")


@dataclass(frozen=True)
class _ImageDeploymentSource:
    """Registered files that resolve to one or more deployed image refs."""

    paths: tuple[str, ...]
    full_ref_pattern: re.Pattern[str] | None = None
    full_ref_key_path: tuple[str, ...] | None = None
    registry_key_path: tuple[str, ...] | None = None
    repository_key_path: tuple[str, ...] | None = None
    tag_key_path: tuple[str, ...] | None = None
    # One values file may pin the same image in several blocks (a chart's
    # webserver and daemon, say). Each is read independently so a drifted
    # second block cannot hide behind a matching first one.
    image_key_paths: tuple[tuple[str, ...], ...] | None = None


# Every non-build-only catalog image, mapped to exactly where its deployed
# reference lives and how to read it. Terraform embeds the complete
# "repo:tag@digest" as one string. Helm values either embed a full reference
# at one YAML key path or split it across explicit registry, repository, and
# tag keys; split values are reconstructed from deployment fields, never from
# the catalog, so a mirror/repository change is detected as drift too.
#
# A catalog image not covered by this map and not in _BUILD_ONLY_IMAGES fails
# the check outright: matching by loose text search (an earlier version of
# this check) could not tell "not deployed anywhere in this repo" apart from
# "deployed, but the tag changed since this was last searched for" -- a
# version bump made the drift this check exists to catch disappear silently.
_IMAGE_DEPLOYMENT_SOURCES: dict[str, _ImageDeploymentSource] = {
    "k8s_bootstrap": _ImageDeploymentSource(
        paths=(
            "infra/terraform/modules/catalog/polaris/variables.tf",
            "infra/terraform/modules/governance/openmetadata/variables.tf",
            "infra/terraform/modules/access/in-cluster-resolution/variables.tf",
            "infra/terraform/modules/identity/keycloak/variables.tf",
        ),
        full_ref_pattern=re.compile(
            r'variable\s+"bootstrap_job_image"\s*\{.*?default\s*=\s*"([^"\n]+@sha256:[0-9a-f]{64})"',
            re.DOTALL,
        ),
    ),
    "keycloak": _ImageDeploymentSource(
        paths=("infra/terraform/modules/identity/keycloak/variables.tf",),
        full_ref_pattern=re.compile(
            r'variable\s+"image"\s*\{.*?default\s*=\s*"([^"\n]+@sha256:[0-9a-f]{64})"', re.DOTALL
        ),
    ),
    "keycloak_config_cli": _ImageDeploymentSource(
        paths=("infra/terraform/modules/identity/keycloak/variables.tf",),
        full_ref_pattern=re.compile(
            r'variable\s+"config_cli_image"\s*\{.*?default\s*=\s*"([^"\n]+@sha256:[0-9a-f]{64})"', re.DOTALL
        ),
    ),
    "dagster_control_plane": _ImageDeploymentSource(
        paths=("infra/helm/values/local/dagster.yaml",),
        image_key_paths=(("dagsterWebserver", "image"), ("dagsterDaemon", "image")),
    ),
    "opensearch": _ImageDeploymentSource(
        paths=("infra/helm/values/local/openmetadata-deps.yaml",),
        repository_key_path=("opensearch", "image", "repository"),
        tag_key_path=("opensearch", "image", "tag"),
    ),
    "postgres": _ImageDeploymentSource(
        paths=(
            "infra/terraform/modules/storage/postgresql/workload.tf",
            "infra/terraform/modules/storage/postgresql/bootstrap.tf",
            "infra/terraform/modules/storage/rds-postgresql/main.tf",
        ),
        full_ref_pattern=re.compile(r'image\s*=\s*"([^"]+@sha256:[0-9a-f]{64})"'),
    ),
    "seaweedfs": _ImageDeploymentSource(
        paths=("infra/helm/values/local/seaweedfs.yaml",),
        repository_key_path=("image", "repository"),
        tag_key_path=("image", "tag"),
    ),
    "polaris": _ImageDeploymentSource(
        paths=("infra/helm/values/local/polaris.yaml",),
        repository_key_path=("image", "repository"),
        tag_key_path=("image", "tag"),
    ),
    "polaris_admin_tool": _ImageDeploymentSource(
        paths=("infra/terraform/modules/catalog/polaris/variables.tf",),
        full_ref_pattern=re.compile(
            r'variable\s+"metastore_bootstrap_job_image"\s*\{.*?default\s*=\s*"([^"\n]+@sha256:[0-9a-f]{64})"',
            re.DOTALL,
        ),
    ),
    "trino": _ImageDeploymentSource(
        paths=("infra/helm/values/local/trino.yaml",),
        repository_key_path=("image", "repository"),
        tag_key_path=("image", "tag"),
    ),
    "openmetadata_ingestion": _ImageDeploymentSource(
        paths=("infra/helm/values/local/openmetadata.yaml",),
        full_ref_key_path=(
            "openmetadata",
            "config",
            "pipelineServiceClientConfig",
            "k8s",
            "ingestionImage",
        ),
    ),
    "superset_init": _ImageDeploymentSource(
        paths=("infra/helm/values/local/superset.yaml",),
        repository_key_path=("initImage", "repository"),
        tag_key_path=("initImage", "tag"),
    ),
    "superset_redis": _ImageDeploymentSource(
        paths=("infra/helm/values/local/superset.yaml",),
        registry_key_path=("redis", "image", "registry"),
        repository_key_path=("redis", "image", "repository"),
        tag_key_path=("redis", "image", "tag"),
    ),
    "portal_static_server": _ImageDeploymentSource(
        paths=("infra/terraform/modules/access/portal/variables.tf",),
        full_ref_pattern=re.compile(
            r'variable\s+"image"\s*\{.*?default\s*=\s*"([^"\n]+@sha256:[0-9a-f]{64})"',
            re.DOTALL,
        ),
    ),
    "traefik": _ImageDeploymentSource(
        paths=("infra/helm/values/local/traefik.yaml",),
        registry_key_path=("image", "registry"),
        repository_key_path=("image", "repository"),
        tag_key_path=("image", "tag"),
    ),
    **{
        f"cert_manager_{component}": _ImageDeploymentSource(
            paths=("infra/helm/values/local/cert-manager.yaml",),
            repository_key_path=(*prefix, "image", "repository"),
            tag_key_path=(*prefix, "image", "tag"),
        )
        for component, prefix in (
            ("controller", ()),
            ("webhook", ("webhook",)),
            ("cainjector", ("cainjector",)),
            ("acmesolver", ("acmesolver",)),
            ("startupapicheck", ("startupapicheck",)),
        )
    },
}
# Not part of the deployed stack: build bases, and the throwaway mail sink
# `olf e2e` starts for its email-delivery check (e2e/_identity_mail.py).
_BUILD_ONLY_IMAGES = frozenset({"project_code_base", "superset_base", "e2e_mail_sink"})


def _value_at_key_path(data: Any, key_path: tuple[str, ...]) -> Any:
    value = data
    for key in key_path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _deployed_image_references(source_path: Path, source: _ImageDeploymentSource) -> list[str]:
    """Read every complete image reference from one registered source file."""
    source_text = source_path.read_text()
    if source.full_ref_pattern is not None:
        return [found.group(1) for found in source.full_ref_pattern.finditer(source_text)]

    try:
        values = yaml.safe_load(source_text)
    except yaml.YAMLError:
        return []

    if source.full_ref_key_path is not None:
        found = _value_at_key_path(values, source.full_ref_key_path)
        return [found] if isinstance(found, str) else []

    if source.image_key_paths is not None:
        references = []
        for prefix in source.image_key_paths:
            repository = _value_at_key_path(values, (*prefix, "repository"))
            tag = _value_at_key_path(values, (*prefix, "tag"))
            if isinstance(repository, str) and isinstance(tag, str):
                references.append(f"{repository}:{tag}")
        return references

    if source.repository_key_path is None or source.tag_key_path is None:
        return []
    repository = _value_at_key_path(values, source.repository_key_path)
    tag = _value_at_key_path(values, source.tag_key_path)
    if not isinstance(repository, str) or not isinstance(tag, str):
        return []
    registry_prefix = ""
    if source.registry_key_path is not None:
        registry = _value_at_key_path(values, source.registry_key_path)
        if not isinstance(registry, str) or not registry:
            return []
        registry_prefix = f"{registry.rstrip('/')}/"
    return [f"{registry_prefix}{repository}:{tag}"]


def _check_images_match_deployment_sources(repo_root: Path, catalog: dict[str, Any]) -> CheckResult:
    """Cross-check each catalog image against its registered deployment source.

    _check_images_digest_pinned only validates the catalog's own shape.
    Deployed images are Dockerfile build inputs baked in at build time
    (project_code_base, superset_base -- no independent source to compare
    against) or specific fields in Helm values / Terraform module source, so
    each is checked against exactly where it is actually deployed from.
    """
    images = (catalog.get("components") or {}).get("images") or {}

    problems: list[str] = []
    for name in sorted(set(_IMAGE_DEPLOYMENT_SOURCES) - set(images)):
        problems.append(f"{name}: registered deployment image is missing from the component catalog")

    for name, ref in sorted(images.items()):
        ref = str(ref)
        if not _IMAGE_TAG_DIGEST_PATTERN.search(ref):
            continue  # missing/malformed digest is already caught by _check_images_digest_pinned
        if name in _BUILD_ONLY_IMAGES:
            continue

        source = _IMAGE_DEPLOYMENT_SOURCES.get(name)
        if source is None:
            problems.append(
                f"{name}: no registered deployment source -- add it to _IMAGE_DEPLOYMENT_SOURCES or "
                "_BUILD_ONLY_IMAGES in tools/olf/olf/release/_readiness.py"
            )
            continue

        for relative_path in source.paths:
            source_path = repo_root / relative_path
            if not source_path.is_file():
                problems.append(f"{name}: deployment source {relative_path} does not exist")
                continue

            deployed_references = _deployed_image_references(source_path, source)
            if not deployed_references:
                problems.append(f"{name}: {relative_path} has no complete image reference")
                continue

            for deployed in deployed_references:
                if deployed != ref:
                    problems.append(f"{name}: catalog pins {ref!r} but {relative_path} pins {deployed!r}")

    if problems:
        return CheckResult("catalog images match their deployment sources", False, "; ".join(problems))
    return CheckResult("catalog images match their deployment sources", True)


def _check_actions_sha_pinned(repo_root: Path, catalog: dict[str, Any]) -> CheckResult:
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return CheckResult("workflow actions are SHA-pinned", False, "no .github/workflows directory")

    uses_pattern = re.compile(r"^\s*(?:-\s*)?uses:\s*([^@\s]+)@(\S+)")
    problems: list[str] = []
    # Every occurrence is kept (not collapsed into a dict keyed by action name)
    # so a mismatch in one workflow can't be hidden by a matching ref in another.
    occurrences: list[tuple[str, str, str]] = []
    for workflow_file in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
        for line in workflow_file.read_text().splitlines():
            match = uses_pattern.match(line)
            if not match:
                continue
            action, ref = match.group(1), match.group(2)
            if not ACTION_SHA_PATTERN.match(ref):
                problems.append(f"{workflow_file.name}: {action}@{ref}")
            else:
                occurrences.append((workflow_file.name, action, ref))

    catalog_actions = (catalog.get("components") or {}).get("actions") or {}
    if not isinstance(catalog_actions, dict):
        return CheckResult(
            "workflow actions are recorded in the component catalog",
            False,
            "components.actions must be a mapping",
        )

    missing_from_catalog = sorted(
        {
            f"{workflow_name}: {action}@{ref}"
            for workflow_name, action, ref in occurrences
            if catalog_actions.get(action) != ref
        }
    )
    unused_catalog_actions = sorted(set(catalog_actions) - {action for _, action, _ in occurrences})

    if problems:
        return CheckResult("workflow actions are SHA-pinned", False, "; ".join(problems))
    if missing_from_catalog:
        return CheckResult(
            "workflow actions are recorded in the component catalog",
            False,
            f"not recorded (or mismatched) in components.actions: {', '.join(missing_from_catalog)}",
        )
    if unused_catalog_actions:
        return CheckResult(
            "workflow actions are recorded in the component catalog",
            False,
            f"unused entries in components.actions: {', '.join(unused_catalog_actions)}",
        )
    return CheckResult(
        "workflow actions are SHA-pinned and cataloged",
        True,
        f"{len(occurrences)} action reference(s) checked",
    )


def _check_dockerfiles_pinned(repo_root: Path) -> CheckResult:
    problems: list[str] = []
    for dockerfile in sorted(repo_root.glob("images/*/Dockerfile")):
        for line in dockerfile.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("FROM ${"):
                continue
            if stripped.startswith("FROM ") and not IMAGE_DIGEST_SUFFIX_PATTERN.search(stripped):
                problems.append(f"{dockerfile.relative_to(repo_root)}: {stripped}")
            if re.match(r"^ARG\s+[A-Z_]*IMAGE=", stripped) and not IMAGE_DIGEST_SUFFIX_PATTERN.search(stripped):
                problems.append(f"{dockerfile.relative_to(repo_root)}: {stripped}")
    if problems:
        return CheckResult("Dockerfile base images are digest-pinned", False, "; ".join(problems))
    return CheckResult("Dockerfile base images are digest-pinned", True)


def _check_chart_versions_match_deployment_wrappers(repo_root: Path) -> CheckResult:
    """Ensure every catalog-routed chart's fallback default and Terraform
    module default stay aligned, and that the two chart sets match exactly.

    Every deployment provider (#124 local, #125 AWS/Azure) now resolves
    every deployed chart through `olf.deployment.charts.CHART_DEFAULTS`
    rather than a Trino/Dagster-only pair of hardcoded fields (#147) - a
    chart added to a Terraform module without a matching `CHART_DEFAULTS`
    entry would otherwise fetch straight from its upstream repository,
    unverified against the component catalog, and go unnoticed exactly as
    5 of the 7 catalog charts did before this check existed.
    """
    from olf.deployment.charts import CHART_DEFAULTS

    module_versions = _helm_chart_versions_from_terraform_modules(repo_root)
    module_charts = frozenset(module_versions)
    default_charts = frozenset(CHART_DEFAULTS)

    problems: list[str] = []
    only_in_terraform = sorted(module_charts - default_charts)
    if only_in_terraform:
        problems.append(f"Terraform module chart(s) with no CHART_DEFAULTS entry: {', '.join(only_in_terraform)}")
    only_in_defaults = sorted(default_charts - module_charts)
    if only_in_defaults:
        problems.append(f"CHART_DEFAULTS entry with no Terraform module: {', '.join(only_in_defaults)}")

    checked = 0
    for name in sorted(module_charts & default_charts):
        checked += 1
        default_version = CHART_DEFAULTS[name].version
        module_version = module_versions[name]
        if default_version != module_version:
            problems.append(
                f"{name}: CHART_DEFAULTS version={default_version!r} but Terraform chart_version={module_version!r}"
            )
    if problems:
        return CheckResult("Helm chart versions match deployment wrappers", False, "; ".join(problems))
    return CheckResult("Helm chart versions match deployment wrappers", True, f"{checked} chart(s) checked")


def run_release_check(
    repo_root: str | Path = ".",
    *,
    catalog_path: str | Path = DEFAULT_CATALOG_PATH,
    tag: str | None = None,
) -> ReleaseCheckReport:
    """Run the release-readiness / clean-install consistency gate.

    Validates: catalog version matches the tag (when a tag is given), every
    catalog image is digest-pinned and matches its Helm deployment source
    (where one exists), every workflow action is SHA-pinned and recorded in
    the catalog, every Terraform root's required_version matches the catalog,
    provider-using Terraform roots retain their lockfiles, effective wrapper
    chart versions match the rendered Terraform-module values, no Dockerfile
    FROM/ARG is unpinned outside of build-arg indirection, and the
    compatibility matrix doc matches a fresh render.
    Lockfile/pyproject.toml sync is validated separately by
    `scripts/test/check-lockfiles.sh` using `uv` directly.
    """
    root = Path(repo_root).resolve()
    catalog = load_catalog(root / catalog_path)

    report = ReleaseCheckReport()
    report.results.append(_check_version_matches_tag(catalog, tag))
    report.results.append(_check_python_package_versions(root, catalog))
    report.results.append(_check_images_digest_pinned(catalog))
    report.results.append(_check_toolchain_pinned(catalog))
    report.results.append(_check_images_match_deployment_sources(root, catalog))
    report.results.append(_check_actions_sha_pinned(root, catalog))
    report.results.append(_check_dockerfiles_pinned(root))
    report.results.append(_check_terraform_required_versions_match_catalog(root, catalog))
    report.results.append(_check_provider_using_terraform_roots_have_lockfiles(root))
    report.results.append(_check_chart_versions_match_deployment_wrappers(root))
    report.results.append(_check_catalog_charts(root, catalog))
    report.results.append(_check_compatibility_matrix_up_to_date(root, catalog))
    return report
