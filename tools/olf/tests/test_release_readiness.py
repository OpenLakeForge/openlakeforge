from pathlib import Path

import pytest
import yaml

from olf.release import _readiness
from olf.release._manifest import load_catalog

ROOT = Path(__file__).parents[3]


def test_check_images_digest_pinned_flags_missing_digest() -> None:
    catalog = {"components": {"images": {"bad": "python:3.12-slim"}}}
    result = _readiness._check_images_digest_pinned(catalog)
    assert not result.ok
    assert "bad" in result.detail


_VALID_TOOLCHAIN_ENTRY = {
    "version": "1.2.3",
    "platforms": {
        "darwin-amd64": "sha256:" + "a" * 64,
        "darwin-arm64": "sha256:" + "b" * 64,
        "linux-amd64": "sha256:" + "c" * 64,
        "linux-arm64": "sha256:" + "d" * 64,
    },
}


def _valid_toolchain_catalog() -> dict:
    return {
        "components": {
            "toolchain": {
                tool: dict(_VALID_TOOLCHAIN_ENTRY) for tool in ("terraform", "helm", "kubectl", "kind")
            }
        }
    }


def test_check_toolchain_pinned_passes_on_real_repo_catalog() -> None:
    catalog = load_catalog(ROOT / "release/component-catalog.yaml")
    result = _readiness._check_toolchain_pinned(catalog)
    assert result.ok, result.detail


def test_check_toolchain_pinned_flags_missing_components_block() -> None:
    result = _readiness._check_toolchain_pinned({"components": {}})
    assert not result.ok
    assert "missing" in result.detail


def test_check_toolchain_pinned_flags_missing_tool() -> None:
    catalog = _valid_toolchain_catalog()
    del catalog["components"]["toolchain"]["kind"]
    result = _readiness._check_toolchain_pinned(catalog)
    assert not result.ok
    assert "kind" in result.detail


@pytest.mark.parametrize("malformed", [None, "1.0.0", 42, ["not", "a", "mapping"]])
def test_check_toolchain_pinned_flags_a_non_mapping_entry(malformed: object) -> None:
    """A present-but-malformed entry (e.g. `terraform: null`) must be
    flagged, not silently skipped - `build_spec()` would otherwise crash on
    `entry.get()` at runtime for a catalog this check already approved."""
    catalog = _valid_toolchain_catalog()
    catalog["components"]["toolchain"]["terraform"] = malformed
    result = _readiness._check_toolchain_pinned(catalog)
    assert not result.ok
    assert "terraform" in result.detail


def test_check_toolchain_pinned_rejects_latest_version() -> None:
    catalog = _valid_toolchain_catalog()
    catalog["components"]["toolchain"]["terraform"]["version"] = "latest"
    result = _readiness._check_toolchain_pinned(catalog)
    assert not result.ok
    assert "terraform" in result.detail


def test_check_toolchain_pinned_flags_missing_platform() -> None:
    catalog = _valid_toolchain_catalog()
    del catalog["components"]["toolchain"]["helm"]["platforms"]["linux-arm64"]
    result = _readiness._check_toolchain_pinned(catalog)
    assert not result.ok
    assert "helm" in result.detail
    assert "linux-arm64" in result.detail


def test_check_toolchain_pinned_flags_malformed_digest() -> None:
    catalog = _valid_toolchain_catalog()
    catalog["components"]["toolchain"]["kubectl"]["platforms"]["linux-amd64"] = "not-a-digest"
    result = _readiness._check_toolchain_pinned(catalog)
    assert not result.ok
    assert "kubectl" in result.detail
    assert "linux-amd64" in result.detail


def test_check_images_match_deployment_sources_passes_on_real_repo_catalog() -> None:
    catalog = load_catalog(ROOT / "release/component-catalog.yaml")
    result = _readiness._check_images_match_deployment_sources(ROOT, catalog)
    assert result.ok, result.detail


def test_check_images_match_deployment_sources_flags_a_stale_catalog_digest(tmp_path: Path) -> None:
    """A Helm values file's digest changing without the matching catalog
    update must be caught -- neither _check_images_digest_pinned (validates
    only the catalog's own shape) nor check-components.sh's Helm-values loop
    (validates only that some digest is present) compares the two.
    """
    values_dir = tmp_path / "infra/helm/values/local"
    values_dir.mkdir(parents=True)
    (values_dir / "trino.yaml").write_text(
        'image:\n  repository: trinodb/trino\n  tag: "480@sha256:' + "b" * 64 + '"\n'
    )

    catalog = {"components": {"images": {"trino": "trinodb/trino:480@sha256:" + "a" * 64}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "trino" in result.detail
    assert "b" * 64 in result.detail


def test_check_images_match_deployment_sources_ignores_build_only_images() -> None:
    """project_code_base/superset_base are Dockerfile-only build inputs with
    no Helm values counterpart -- absence from infra/helm/values must not be
    flagged as drift.
    """
    catalog = load_catalog(ROOT / "release/component-catalog.yaml")
    result = _readiness._check_images_match_deployment_sources(ROOT, catalog)
    assert result.ok, result.detail


def test_check_images_match_deployment_sources_catches_a_tag_bump_the_catalog_missed(
    tmp_path: Path,
) -> None:
    """The scenario from the review: bumping a deployed image to a new tag
    AND digest (e.g. Trino 480 -> 481) without updating the catalog removes
    every occurrence of the catalog's old tag from the deployment source.
    Matching against a registered file/field (not a substring search for the
    old tag) still catches this -- it reads whatever the field currently
    contains, not what the catalog remembers.
    """
    values_dir = tmp_path / "infra/helm/values/local"
    values_dir.mkdir(parents=True)
    (values_dir / "trino.yaml").write_text(
        'image:\n  repository: trinodb/trino\n  tag: "481@sha256:' + "b" * 64 + '"\n'
    )

    catalog = {"components": {"images": {"trino": "trinodb/trino:480@sha256:" + "a" * 64}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "480" in result.detail
    assert "481" in result.detail


def test_check_images_match_deployment_sources_catches_a_repository_override(
    tmp_path: Path,
) -> None:
    """A mirror override with an unchanged tag and digest is still drift."""
    values_dir = tmp_path / "infra/helm/values/local"
    values_dir.mkdir(parents=True)
    digest = "a" * 64
    (values_dir / "polaris.yaml").write_text(
        'image:\n  repository: mirror.example.com/apache/polaris\n  tag: "1.4.0@sha256:'
        + digest
        + '"\n'
    )

    catalog = {
        "components": {"images": {"polaris": "apache/polaris:1.4.0@sha256:" + digest}}
    }
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "apache/polaris" in result.detail
    assert "mirror.example.com/apache/polaris" in result.detail


def test_check_images_match_deployment_sources_requires_a_complete_split_reference(
    tmp_path: Path,
) -> None:
    """Split Helm values must pin the repository as well as tag and digest."""
    values_dir = tmp_path / "infra/helm/values/local"
    values_dir.mkdir(parents=True)
    digest = "a" * 64
    (values_dir / "trino.yaml").write_text('image:\n  tag: "480@sha256:' + digest + '"\n')

    catalog = {"components": {"images": {"trino": "trinodb/trino:480@sha256:" + digest}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "no complete image reference" in result.detail


def test_check_images_match_deployment_sources_flags_an_unregistered_image(tmp_path: Path) -> None:
    """A catalog image with no entry in _IMAGE_DEPLOYMENT_SOURCES or
    _BUILD_ONLY_IMAGES must fail loudly, not silently skip -- silently
    skipping is exactly how the tag-bump case above went unnoticed before.
    """
    catalog = {"components": {"images": {"brand_new_image": "example/new:1.0@sha256:" + "a" * 64}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "brand_new_image" in result.detail
    assert "no registered deployment source" in result.detail


def test_check_images_match_deployment_sources_flags_every_postgres_terraform_reference(
    tmp_path: Path,
) -> None:
    postgres_dir = tmp_path / "infra/terraform/modules/storage/postgresql"
    postgres_dir.mkdir(parents=True)
    rds_dir = tmp_path / "infra/terraform/modules/storage/rds-postgresql"
    rds_dir.mkdir(parents=True)
    postgres_ref = "postgres:16-alpine@sha256:" + "a" * 64
    stale_ref = "postgres:16-alpine@sha256:" + "b" * 64
    (postgres_dir / "workload.tf").write_text(f'image = "{postgres_ref}"\n')
    (postgres_dir / "bootstrap.tf").write_text(f'image = "{stale_ref}"\n')
    (rds_dir / "main.tf").write_text(f'image = "{postgres_ref}"\n')

    catalog = {"components": {"images": {"postgres": postgres_ref}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "postgres" in result.detail
    assert stale_ref in result.detail


def test_check_images_match_deployment_sources_flags_bootstrap_image_drift(tmp_path: Path) -> None:
    polaris_dir = tmp_path / "infra/terraform/modules/catalog/polaris"
    polaris_dir.mkdir(parents=True)
    openmetadata_dir = tmp_path / "infra/terraform/modules/governance/openmetadata"
    openmetadata_dir.mkdir(parents=True)
    bootstrap_ref = "alpine/k8s:1.30.0@sha256:" + "a" * 64
    stale_ref = "alpine/k8s:1.30.0@sha256:" + "b" * 64
    (polaris_dir / "variables.tf").write_text(
        f'variable "bootstrap_job_image" {{\n  default = "{bootstrap_ref}"\n}}\n'
    )
    (openmetadata_dir / "variables.tf").write_text(
        f'variable "bootstrap_job_image" {{\n  default = "{stale_ref}"\n}}\n'
    )

    catalog = {"components": {"images": {"k8s_bootstrap": bootstrap_ref}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "k8s_bootstrap" in result.detail
    assert stale_ref in result.detail


def test_check_images_match_deployment_sources_flags_polaris_admin_tool_drift(tmp_path: Path) -> None:
    polaris_dir = tmp_path / "infra/terraform/modules/catalog/polaris"
    polaris_dir.mkdir(parents=True)
    catalog_ref = "apache/polaris-admin-tool:1.4.0@sha256:" + "a" * 64
    stale_ref = "apache/polaris-admin-tool:1.4.0@sha256:" + "b" * 64
    (polaris_dir / "variables.tf").write_text(
        f'variable "metastore_bootstrap_job_image" {{\n  default = "{stale_ref}"\n}}\n'
    )

    catalog = {"components": {"images": {"polaris_admin_tool": catalog_ref}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "polaris_admin_tool" in result.detail
    assert stale_ref in result.detail


def test_check_images_match_deployment_sources_flags_chart_managed_image_drift(
    tmp_path: Path,
) -> None:
    helm_values = tmp_path / "infra/helm/values/local"
    helm_values.mkdir(parents=True)
    opensearch_ref = "opensearchproject/opensearch:3.3.2@sha256:" + "a" * 64
    stale_redis_ref = "docker.io/bitnamilegacy/redis:7.0.10@sha256:" + "b" * 64
    (helm_values / "openmetadata-deps.yaml").write_text(
        "opensearch:\n  image:\n    repository: opensearchproject/opensearch\n"
        f'    tag: "3.3.2@sha256:{"a" * 64}"\n'
    )
    (helm_values / "superset.yaml").write_text(
        "redis:\n  image:\n    registry: docker.io\n    repository: bitnamilegacy/redis\n"
        f'    tag: "7.0.10@sha256:{"b" * 64}"\n'
        "initImage:\n  repository: apache/superset\n"
        f'  tag: "dockerize@sha256:{"c" * 64}"\n'
    )

    catalog = {
        "components": {
            "images": {
                "opensearch": opensearch_ref,
                "superset_redis": "docker.io/bitnamilegacy/redis:7.0.10@sha256:" + "d" * 64,
                "superset_init": "apache/superset:dockerize@sha256:" + "c" * 64,
            }
        }
    }
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "superset_redis" in result.detail
    assert stale_redis_ref in result.detail


def test_check_images_match_deployment_sources_flags_aws_postgres_reference_drift(
    tmp_path: Path,
) -> None:
    postgres_dir = tmp_path / "infra/terraform/modules/storage/postgresql"
    postgres_dir.mkdir(parents=True)
    rds_dir = tmp_path / "infra/terraform/modules/storage/rds-postgresql"
    rds_dir.mkdir(parents=True)
    postgres_ref = "postgres:16-alpine@sha256:" + "a" * 64
    stale_ref = "postgres:16-alpine@sha256:" + "b" * 64
    (postgres_dir / "main.tf").write_text(f'image = "{postgres_ref}"\n')
    (rds_dir / "main.tf").write_text(f'image = "{stale_ref}"\n')

    catalog = {"components": {"images": {"postgres": postgres_ref}}}
    result = _readiness._check_images_match_deployment_sources(tmp_path, catalog)

    assert not result.ok
    assert "rds-postgresql/main.tf" in result.detail
    assert stale_ref in result.detail


def test_check_images_match_deployment_sources_requires_every_registered_image_in_catalog(
    tmp_path: Path,
) -> None:
    result = _readiness._check_images_match_deployment_sources(tmp_path, {"components": {"images": {}}})

    assert not result.ok
    assert "trino: registered deployment image is missing from the component catalog" in result.detail


def _write_chart_module(tmp_path: Path, module_path: str, *, version: str, deps_version: str | None = None) -> None:
    module_dir = tmp_path / "infra/terraform/modules" / module_path
    module_dir.mkdir(parents=True)
    body = f'variable "chart_version" {{\n  default = "{version}"\n}}\n'
    if deps_version is not None:
        body += f'variable "deps_chart_version" {{\n  default = "{deps_version}"\n}}\n'
    (module_dir / "variables.tf").write_text(body)


def test_chart_versions_match_deployment_wrappers_flags_version_drift(tmp_path: Path) -> None:
    _write_chart_module(tmp_path, "query/trino", version="9.9.9")

    result = _readiness._check_chart_versions_match_deployment_wrappers(tmp_path)

    assert not result.ok
    assert "trino: CHART_DEFAULTS version=" in result.detail
    assert "9.9.9" in result.detail


def test_chart_versions_match_deployment_wrappers_flags_terraform_only_chart(tmp_path: Path) -> None:
    _write_chart_module(tmp_path, "query/trino", version="1.42.2")
    _write_chart_module(tmp_path, "storage/not-a-catalog-chart", version="1.0.0")

    result = _readiness._check_chart_versions_match_deployment_wrappers(tmp_path)

    assert not result.ok
    assert "not-a-catalog-chart" in result.detail
    assert "no CHART_DEFAULTS entry" in result.detail


def test_chart_versions_match_deployment_wrappers_passes_on_real_repo() -> None:
    result = _readiness._check_chart_versions_match_deployment_wrappers(ROOT)
    assert result.ok, result.detail


def test_check_dockerfiles_pinned_passes_on_real_repo() -> None:
    result = _readiness._check_dockerfiles_pinned(ROOT)
    assert result.ok, result.detail


def test_check_dockerfiles_pinned_flags_unpinned_from(tmp_path: Path) -> None:
    images_dir = tmp_path / "images" / "demo"
    images_dir.mkdir(parents=True)
    (images_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
    result = _readiness._check_dockerfiles_pinned(tmp_path)
    assert not result.ok
    assert "Dockerfile" in result.detail


def test_check_actions_sha_pinned_flags_unpinned_action(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "demo.yml").write_text("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
    result = _readiness._check_actions_sha_pinned(tmp_path, {"components": {"actions": {}}})
    assert not result.ok
    assert "actions/checkout@v4" in result.detail


def test_check_actions_sha_pinned_flags_uncataloged_pinned_action(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    sha = "a" * 40
    (workflows / "demo.yml").write_text(f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{sha}\n")
    result = _readiness._check_actions_sha_pinned(tmp_path, {"components": {"actions": {}}})
    assert not result.ok
    assert "checkout" in result.detail


def test_check_actions_flags_a_mismatch_masked_by_a_later_cataloged_occurrence(tmp_path: Path) -> None:
    """A stale ref in one workflow must not be hidden by a cataloged ref in another.

    Collapsing occurrences into a dict keyed by action name let the later file win:
    'checks.yml' sorts before 'release.yml', so release.yml's cataloged SHA
    overwrote checks.yml's stale one and the gate passed.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    stale, cataloged = "b" * 40, "c" * 40
    (workflows / "checks.yml").write_text(
        f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{stale}\n"
    )
    (workflows / "release.yml").write_text(
        f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{cataloged}\n"
    )

    result = _readiness._check_actions_sha_pinned(
        tmp_path, {"components": {"actions": {"actions/checkout": cataloged}}}
    )

    assert not result.ok
    assert "checks.yml" in result.detail
    assert stale in result.detail


def test_check_actions_passes_when_every_occurrence_matches_the_catalog(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    sha = "d" * 40
    for name in ("checks.yml", "release.yml"):
        (workflows / name).write_text(f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{sha}\n")

    result = _readiness._check_actions_sha_pinned(
        tmp_path, {"components": {"actions": {"actions/checkout": sha}}}
    )

    assert result.ok
    assert "2 action reference(s)" in result.detail


def test_check_actions_flags_unused_catalog_entry(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    sha = "d" * 40
    (workflows / "release.yml").write_text(
        f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{sha}\n"
    )

    result = _readiness._check_actions_sha_pinned(
        tmp_path,
        {
            "components": {
                "actions": {
                    "actions/checkout": sha,
                    "actions/cache": "e" * 40,
                }
            }
        },
    )

    assert not result.ok
    assert "unused entries" in result.detail
    assert "actions/cache" in result.detail


def test_run_release_check_passes_on_real_repo_catalog() -> None:
    report = _readiness.run_release_check(ROOT, tag="v0.4.0-alpha.1")
    assert report.ok, report.render()


def test_run_release_check_fails_on_tag_mismatch() -> None:
    report = _readiness.run_release_check(ROOT, tag="v9.9.9-alpha.9")
    assert not report.ok
    assert any("tag" in result.name for result in report.results if not result.ok)


def test_run_release_check_without_tag_only_validates_catalog_shape() -> None:
    report = _readiness.run_release_check(ROOT)
    version_result = next(r for r in report.results if "valid alpha semver" in r.name)
    assert version_result.ok


def test_release_workflow_disables_redundant_matrix_sbom_uploads() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    sbom_step = next(
        step
        for step in workflow["jobs"]["build"]["steps"]
        if str(step.get("uses", "")).startswith("anchore/sbom-action@")
    )

    assert sbom_step["with"]["upload-artifact"] is False


def test_release_cli_exposes_bundle_building_without_a_shell_wrapper() -> None:
    from typer.testing import CliRunner

    from olf.cli import app

    result = CliRunner().invoke(app, ["release", "--help"])

    assert result.exit_code == 0
    assert "build-bundle" in result.output
