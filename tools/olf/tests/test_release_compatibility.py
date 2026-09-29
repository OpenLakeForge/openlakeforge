from pathlib import Path

import yaml

from olf.release import _compatibility
from olf.release._manifest import load_catalog

ROOT = Path(__file__).parents[3]


def _write_catalog(tmp_path: Path, **overrides) -> Path:
    catalog = {
        "apiVersion": "openlakeforge.io/v1alpha1",
        "kind": "ComponentCatalog",
        "metadata": {"name": "openlakeforge"},
        "distribution": {"version": "0.1.0-alpha.1", "release_tag_policy": "immutable-semver"},
        "components": {
            "terraform": {"required_version": ">= 1.6.0", "providers": {"hashicorp/aws": "5.100.0"}},
            "python": {
                "project_code_lock": "images/project-code/requirements.lock",
                "tooling_lock": "tools/olf/uv.lock",
            },
            "images": {
                "project_code_base": "python:3.12-slim@sha256:" + "a" * 64,
            },
            "actions": {"actions/checkout": "a" * 40},
        },
    }
    catalog.update(overrides)
    path = tmp_path / "component-catalog.yaml"
    path.write_text(yaml.safe_dump(catalog))
    return path


def test_render_compatibility_matrix_includes_catalog_values(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    rendered = _compatibility.render_compatibility_matrix(catalog, tmp_path)
    assert "hashicorp/aws" in rendered
    assert "5.100.0" in rendered
    assert "python_project_code_base" not in rendered  # sanity: no stray key names leak in


def test_render_compatibility_matrix_helm_charts_read_from_terraform_modules_not_catalog(
    tmp_path: Path,
) -> None:
    """The Helm charts table has no catalog counterpart to keep in sync -- it
    is read directly from each Terraform module's own `chart_version`
    variable default.
    """
    catalog = load_catalog(_write_catalog(tmp_path))
    module_dir = tmp_path / "infra/terraform/modules/query/trino"
    module_dir.mkdir(parents=True)
    (module_dir / "variables.tf").write_text(
        'variable "namespace" {\n  type = string\n}\n\n'
        'variable "chart_version" {\n  type    = string\n  default = "1.42.2"\n}\n'
    )

    rendered = _compatibility.render_compatibility_matrix(catalog, tmp_path)

    assert "## Helm charts" in rendered
    assert "| trino | 1.42.2 |" in rendered


def test_render_compatibility_matrix_omits_per_root_table_with_no_lockfiles(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    rendered = _compatibility.render_compatibility_matrix(catalog, tmp_path)
    assert "Terraform providers by root" not in rendered


def test_render_compatibility_matrix_per_root_table_is_read_from_lockfiles_not_catalog(tmp_path: Path) -> None:
    """The per-root table has no catalog counterpart to keep in sync -- it is
    read directly from each `.terraform.lock.hcl` under infra/terraform.
    """
    catalog = load_catalog(_write_catalog(tmp_path))
    lock_dir = tmp_path / "infra/terraform/environments/demo"
    lock_dir.mkdir(parents=True)
    (lock_dir / ".terraform.lock.hcl").write_text(
        'provider "registry.terraform.io/hashicorp/example" {\n  version = "1.2.3"\n}\n'
    )

    rendered = _compatibility.render_compatibility_matrix(catalog, tmp_path)

    assert "Terraform providers by root" in rendered
    assert "infra/terraform/environments/demo/.terraform.lock.hcl" in rendered
    assert "1.2.3" in rendered


def test_render_compatibility_matrix_exposes_per_root_provider_drift() -> None:
    """The real repo's infra/terraform/environments/local root locks
    hashicorp/helm at 3.1.1, distinct from the tracked 3.2.0 -- both must be
    visible, in the right table.
    """
    catalog = load_catalog(ROOT / "release/component-catalog.yaml")
    rendered = _compatibility.render_compatibility_matrix(catalog, ROOT)

    assert "Terraform providers by root" in rendered
    local_root = "infra/terraform/environments/local/.terraform.lock.hcl"
    assert local_root in rendered

    # Extract the hashicorp/helm row from the per-root table (not the tracked-
    # version table above it, which has an identically-prefixed row) and
    # confirm the local column reads 3.1.1, not the tracked 3.2.0 -- parsed
    # structurally, not just substring search, since "3.2.0" and "3.1.1" both
    # appear elsewhere in the document.
    per_root_section = rendered.split("### Terraform providers by root", 1)[1]
    header = next(line for line in per_root_section.splitlines() if line.startswith("| Provider | infra/"))
    columns = [c.strip() for c in header.strip("|").split("|")]
    local_index = columns.index(local_root)
    helm_row = next(line for line in per_root_section.splitlines() if line.startswith("| hashicorp/helm |"))
    cells = [c.strip() for c in helm_row.strip("|").split("|")]
    assert cells[local_index] == "3.1.1"


def test_terraform_lock_provider_versions_parses_real_local_lockfile() -> None:
    versions = _compatibility._terraform_lock_provider_versions(
        ROOT / "infra/terraform/environments/local/.terraform.lock.hcl"
    )
    assert versions["hashicorp/helm"] == "3.1.1"
    assert versions["hashicorp/kubernetes"] == "2.38.0"


def test_helm_chart_versions_from_terraform_modules_matches_real_repo() -> None:
    versions = _compatibility._helm_chart_versions_from_terraform_modules(ROOT)
    assert versions["trino"] == "1.42.2"
    assert versions["seaweedfs"] == "4.23.0"
    # The paired dependencies chart, from deps_chart_version in the same module.
    assert versions["openmetadata"] == "1.13.6"
    assert versions["openmetadata-dependencies"] == "1.13.6"


def test_helm_chart_versions_from_terraform_modules_ignores_variables_without_defaults(
    tmp_path: Path,
) -> None:
    module_dir = tmp_path / "infra/terraform/modules/query/trino"
    module_dir.mkdir(parents=True)
    (module_dir / "variables.tf").write_text('variable "chart_version" {\n  type = string\n}\n')
    assert _compatibility._helm_chart_versions_from_terraform_modules(tmp_path) == {}


def test_check_terraform_required_versions_match_real_catalog() -> None:
    catalog = load_catalog(ROOT / "release/component-catalog.yaml")
    result = _compatibility._check_terraform_required_versions_match_catalog(ROOT, catalog)
    assert result.ok, result.detail


def test_check_terraform_required_versions_flags_catalog_drift(tmp_path: Path) -> None:
    root_dir = tmp_path / "infra/terraform/environments/demo"
    root_dir.mkdir(parents=True)
    (root_dir / "main.tf").write_text('terraform {\n  required_version = ">= 1.9.0"\n}\n')

    catalog = {"components": {"terraform": {"required_version": ">= 1.6.0"}}}
    result = _compatibility._check_terraform_required_versions_match_catalog(tmp_path, catalog)

    assert not result.ok
    assert "environments/demo/main.tf" in result.detail
    assert "terraform='>= 1.9.0'" in result.detail


def test_check_terraform_required_versions_requires_each_root_to_declare_a_constraint(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / "infra/terraform/environments/demo"
    root_dir.mkdir(parents=True)
    (root_dir / "main.tf").write_text("terraform {\n  required_providers {}\n}\n")

    catalog = {"components": {"terraform": {"required_version": ">= 1.6.0"}}}
    result = _compatibility._check_terraform_required_versions_match_catalog(tmp_path, catalog)

    assert not result.ok
    assert "has no required_version" in result.detail


def test_check_terraform_required_versions_requires_a_terraform_block_in_every_root(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / "infra/terraform/environments/demo"
    root_dir.mkdir(parents=True)
    (root_dir / "main.tf").write_text('locals { name = "demo" }\n')

    catalog = {"components": {"terraform": {"required_version": ">= 1.6.0"}}}
    result = _compatibility._check_terraform_required_versions_match_catalog(tmp_path, catalog)

    assert not result.ok
    assert "environments/demo: no terraform block" in result.detail


def test_provider_using_terraform_roots_require_tracked_lockfiles(tmp_path: Path) -> None:
    root_dir = tmp_path / "infra/terraform/environments/demo"
    root_dir.mkdir(parents=True)
    (root_dir / "main.tf").write_text(
        'terraform {\n  required_providers {\n    example = { source = "hashicorp/example" }\n  }\n}\n'
    )

    result = _compatibility._check_provider_using_terraform_roots_have_lockfiles(tmp_path)

    assert not result.ok
    assert "infra/terraform/environments/demo/.terraform.lock.hcl" in result.detail


def test_provider_using_terraform_roots_with_lockfiles_passes() -> None:
    result = _compatibility._check_provider_using_terraform_roots_have_lockfiles(ROOT)
    assert result.ok, result.detail


def test_terraform_lock_provider_versions_ignores_nondefault_registry_provider(tmp_path: Path) -> None:
    lock_path = tmp_path / ".terraform.lock.hcl"
    lock_path.write_text('provider "registry.example.com/acme/example" {\n  version = "1.2.3"\n}\n')
    versions = _compatibility._terraform_lock_provider_versions(lock_path)
    assert versions == {"registry.example.com/acme/example": "1.2.3"}


def test_compatibility_matrix_doc_is_up_to_date_on_real_repo() -> None:
    catalog = load_catalog(ROOT / "release/component-catalog.yaml")
    result = _compatibility._check_compatibility_matrix_up_to_date(ROOT, catalog)
    assert result.ok, result.detail


def test_compatibility_matrix_doc_flags_drift(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    docs_dir = tmp_path / "docs" / "release"
    docs_dir.mkdir(parents=True)
    (docs_dir / "compatibility-matrix.md").write_text("stale content from before the last catalog bump\n")

    result = _compatibility._check_compatibility_matrix_up_to_date(tmp_path, catalog)

    assert not result.ok
    assert "does not match a fresh render" in result.detail


def test_compatibility_matrix_doc_missing_is_flagged(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    result = _compatibility._check_compatibility_matrix_up_to_date(tmp_path, catalog)
    assert not result.ok
    assert "does not exist" in result.detail
