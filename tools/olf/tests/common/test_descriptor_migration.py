from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from openlakeforge_core import load_governance_descriptor
from typer.testing import CliRunner

from olf.cli import app
from olf.common.descriptor_migration import (
    MigrationError,
    migrate_governance_text,
    migrate_profile_text,
    migrate_source_text,
    plan_project_migration,
)
from olf.common.profile import AccessSpec, load_deployment_profile, resolve_topology, validate_deployment_profile

runner = CliRunner()

FLAT = """\
apiVersion: openlakeforge.io/v1beta1
kind: Governance
# Domains first.
domains:
  - name: sales
    displayName: Sales
    status: planned
  # Supply chain is planned.
  - name: supply_chain
    displayName: Supply Chain
  - name: hr
    displayName: HR
products:
  # The revenue mart.
  - id: order_revenue
    domain: sales
    displayName: Order Revenue
    status: planned
  - domain: sales
    id: customer_health
    displayName: Customer Health
  - id: inventory
    displayName: Inventory
    domain: supply_chain  # moved with its product
"""


def test_products_move_under_their_domain_and_comments_survive(tmp_path: Path) -> None:
    migrated = migrate_governance_text(FLAT)

    path = tmp_path / "governance.yml"
    path.write_text(migrated, encoding="utf-8")
    document = load_governance_descriptor(path)
    assert {d["name"]: [p["id"] for p in d.get("products", [])] for d in document["domains"]} == {
        "sales": ["order_revenue", "customer_health"],
        "supply_chain": ["inventory"],
        "hr": [],
    }
    assert all("domain" not in p for d in document["domains"] for p in d.get("products", []))
    for comment in ("# Domains first.", "# Supply chain is planned.", "# The revenue mart."):
        assert comment in migrated
    assert "domain:" not in migrated


def test_migration_is_deterministic_and_not_repeatable() -> None:
    migrated = migrate_governance_text(FLAT)
    assert migrate_governance_text(FLAT) == migrated
    with pytest.raises(MigrationError, match="already 'openlakeforge.io/v1beta2'"):
        migrate_governance_text(migrated)


def test_a_flat_document_without_products_only_changes_its_version() -> None:
    text = (
        "apiVersion: openlakeforge.io/v1beta1\nkind: Governance\n"
        "domains:\n  - {name: a, displayName: A}\nproducts: []\n"
    )
    migrated = migrate_governance_text(text)
    assert yaml.safe_load(migrated) == {
        "apiVersion": "openlakeforge.io/v1beta2",
        "kind": "Governance",
        "domains": [{"name": "a", "displayName": "A"}],
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (FLAT.replace("domain: supply_chain", "domain: ghost"), "names unknown domain 'ghost'"),
        (FLAT + "  - id: inventory\n    domain: hr\n    displayName: Again\n", "duplicate product id 'inventory'"),
        (FLAT.replace("  # Supply", "    products: []\n  # Supply"), "layouts are mixed"),
        (FLAT.replace("v1beta1", "v1alpha3"), "only v1beta1 documents"),
    ],
)
def test_unmigratable_documents_are_refused(text: str, message: str) -> None:
    with pytest.raises(MigrationError, match=message):
        migrate_governance_text(text)


def _project(root: Path, text: str) -> Path:
    (root / "lakehouse_code").mkdir(parents=True)
    path = root / "lakehouse_code" / "governance.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_command_dry_run_prints_the_diff_and_writes_nothing(tmp_path: Path) -> None:
    path = _project(tmp_path, FLAT)
    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "+    products:" in result.output
    assert path.read_text(encoding="utf-8") == FLAT


def test_command_rewrites_the_file_in_place(tmp_path: Path) -> None:
    path = _project(tmp_path, FLAT)
    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert path.read_text(encoding="utf-8") == migrate_governance_text(FLAT)
    assert not list(path.parent.glob("*.migrating"))
    again = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert "already current" in again.output


def test_a_failing_migration_leaves_the_original_untouched(tmp_path: Path) -> None:
    broken = FLAT.replace("domain: supply_chain", "domain: ghost")
    path = _project(tmp_path, broken)
    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert result.exit_code != 0
    assert "unknown domain 'ghost'" in result.output
    assert path.read_text(encoding="utf-8") == broken


SOURCE = """\
# CRM extract.
apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: orders
    load: {mode: incremental, cursor: updated_at, primary_key: [id]}
runtime:
  secrets: [api_token]
"""


def test_source_migration_changes_only_the_version_line(tmp_path: Path) -> None:
    migrated = migrate_source_text(SOURCE)
    assert migrated == SOURCE.replace("v1alpha3", "v1beta1")
    with pytest.raises(MigrationError, match="already 'openlakeforge.io/v1beta1'"):
        migrate_source_text(migrated)


def test_a_source_that_is_invalid_is_refused_with_the_validation_message() -> None:
    with pytest.raises(MigrationError, match="not a valid openlakeforge.io/v1beta1 Source"):
        migrate_source_text(SOURCE.replace("name: crm", "name: CRM"))


def test_a_v1alpha3_source_names_the_command_that_fixes_it() -> None:
    from openlakeforge_core import LakehouseDescriptorError
    from openlakeforge_core.descriptors import validate_source_descriptor

    with pytest.raises(LakehouseDescriptorError, match="olf project migrate"):
        validate_source_descriptor(yaml.safe_load(SOURCE))


def _both(root: Path) -> tuple[Path, Path]:
    governance = _project(root, FLAT)
    source = root / "lakehouse_code" / "bronze" / "crm" / "source.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    return governance, source


def test_command_migrates_governance_and_every_source(tmp_path: Path) -> None:
    governance, source = _both(tmp_path)
    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "lakehouse_code/bronze/crm/source.yaml" in result.output
    assert source.read_text(encoding="utf-8") == SOURCE.replace("v1alpha3", "v1beta1")
    assert "v1beta2" in governance.read_text(encoding="utf-8")


def test_one_unmigratable_descriptor_leaves_all_files_untouched(tmp_path: Path) -> None:
    governance, source = _both(tmp_path)
    broken = SOURCE.replace("name: crm", "name: CRM")
    source.write_text(broken, encoding="utf-8")
    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert result.exit_code != 0
    assert "bronze/crm/source.yaml" in result.output
    assert governance.read_text(encoding="utf-8") == FLAT
    assert source.read_text(encoding="utf-8") == broken

def test_a_comment_after_the_last_product_is_kept() -> None:
    text = FLAT + "# keep me after the last product\n"
    assert "# keep me after the last product" in migrate_governance_text(text)


def test_a_quoted_api_version_is_migrated() -> None:
    quoted = FLAT.replace("apiVersion: openlakeforge.io/v1beta1", 'apiVersion: "openlakeforge.io/v1beta1"')
    migrated = migrate_governance_text(quoted)
    assert 'apiVersion: "openlakeforge.io/v1beta2"' in migrated
    assert "v1beta1" not in migrated


def test_a_current_file_that_mentions_the_old_version_is_not_legacy(tmp_path: Path) -> None:
    current = (
        "# Migrated from openlakeforge.io/v1beta1.\n"
        "apiVersion: openlakeforge.io/v1beta2\nkind: Governance\ndomains:\n  - {name: a, displayName: A}\n"
    )
    path = _project(tmp_path, current)
    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "already current" in result.output
    assert path.read_text(encoding="utf-8") == current


def test_a_current_source_that_mentions_the_old_version_is_left_alone(tmp_path: Path) -> None:
    governance, source = _both(tmp_path)
    governance.write_text(migrate_governance_text(FLAT), encoding="utf-8")
    source.write_text(
        SOURCE.replace("v1alpha3", "v1beta1").replace("# CRM extract.", "# was openlakeforge.io/v1alpha3"),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "already current" in result.output


def test_a_quoted_source_api_version_is_migrated() -> None:
    quoted = SOURCE.replace("apiVersion: openlakeforge.io/v1alpha3", "apiVersion: 'openlakeforge.io/v1alpha3'")
    migrated = migrate_source_text(quoted)
    assert "apiVersion: 'openlakeforge.io/v1beta1'" in migrated


LEGACY_PROFILE = Path(__file__).parents[1] / "fixtures" / "profiles" / "legacy_v1alpha1_issuer.yaml"


def _legacy_text() -> str:
    return LEGACY_PROFILE.read_text(encoding="utf-8")


def test_a_profile_migrates_to_v1beta1_renaming_only_access_issuer() -> None:
    text = _legacy_text()

    migrated = migrate_profile_text(text)

    assert migrated == (
        text.replace("openlakeforge.io/v1alpha1\nkind", "openlakeforge.io/v1beta1\nkind").replace(
            "    issuer: local-ca  # renamed tls", "    tls: local-ca  # renamed tls"
        )
    )
    profile = validate_deployment_profile(yaml.safe_load(migrated))
    assert resolve_topology(profile).access == AccessSpec("olf.localhost", "local-ca")
    assert profile.identity is not None and profile.identity.issuer == "github"
    with pytest.raises(MigrationError, match="already 'openlakeforge.io/v1beta1'"):
        migrate_profile_text(migrated)


@pytest.mark.parametrize(
    "access",
    [
        "  access: {issuer: local-ca}\n",
        "  access: { issuer: local-ca }\n",
        "  access:\n    issuer: local-ca\n",
    ],
)
def test_a_profile_access_block_is_migrated_in_flow_or_block_style(access: str) -> None:
    text = (
        "apiVersion: openlakeforge.io/v1alpha1\nkind: DeploymentProfile\nmetadata: {name: p}\n"
        "spec:\n  provider: {type: local}\n  preset: slim\n  stages: {dev: {enabled: true}}\n" + access
    )

    migrated = migrate_profile_text(text)

    assert yaml.safe_load(migrated)["spec"]["access"] == {"tls": "local-ca"}
    assert yaml.safe_load(migrated)["apiVersion"] == "openlakeforge.io/v1beta1"


def test_a_profile_without_access_only_changes_its_version() -> None:
    text = (LEGACY_PROFILE.parent / "valid_slim_local.yaml").read_text(encoding="utf-8")
    text = text.replace("v1beta1", "v1alpha1")

    assert migrate_profile_text(text) == text.replace("v1alpha1", "v1beta1")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_legacy_text().replace("    issuer: local-ca", "    tls: none\n    issuer: local-ca"), "sets both"),
        (_legacy_text().replace("kind: DeploymentProfile", "kind: Other"), "expected kind"),
        (_legacy_text().replace("issuer: local-ca", "issuer: letsencrypt"), "not a valid"),
    ],
)
def test_an_unmigratable_profile_is_refused(text: str, message: str) -> None:
    with pytest.raises(MigrationError, match=message):
        migrate_profile_text(text)


def _legacy_project(root: Path) -> Path:
    path = root / "openlakeforge.yaml"
    path.write_text(_legacy_text(), encoding="utf-8")
    return path


def test_the_command_migrates_the_profile_next_to_the_descriptors(tmp_path: Path) -> None:
    profile = _legacy_project(tmp_path)
    governance, source = _both(tmp_path)

    dry = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "+    tls: local-ca" in dry.output
    assert "v1alpha1" in profile.read_text(encoding="utf-8")

    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Migrated 3 descriptor(s)" in result.output
    load_deployment_profile(profile)
    assert "v1beta2" in governance.read_text(encoding="utf-8")
    assert "v1beta1" in source.read_text(encoding="utf-8")
    again = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])
    assert "already current" in again.output


def test_role_mappings_are_checked_against_the_distribution_not_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = Path(__file__).resolve().parents[4]
    project = tmp_path / "project"
    project.mkdir()
    assert not (project / "release").exists()
    profile = project / "openlakeforge.yaml"
    profile.write_text(
        _legacy_text().replace(
            "  identity:\n    issuer: github\n    github:\n      organization: my-org\n"
            "      teams:\n        platform-admin: [platform-team]\n"
            "      oauth_app_secret_ref:\n        name: github-oauth-app\n",
            "  identity:\n    issuer: external\n    issuer_url: https://login.example.com\n"
            "    role_claim: groups\n    role_mapping:\n      platform-admin: [admins]\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    (change,) = plan_project_migration(project, distribution_root=distribution)

    assert "    tls: local-ca" in change.migrated


def test_an_unmigratable_profile_leaves_the_descriptors_untouched(tmp_path: Path) -> None:
    profile = _legacy_project(tmp_path)
    broken = _legacy_text().replace("issuer: local-ca", "issuer: letsencrypt")
    profile.write_text(broken, encoding="utf-8")
    governance, _source = _both(tmp_path)

    result = runner.invoke(app, ["project", "migrate", "--repo-root", str(tmp_path)])

    assert result.exit_code != 0
    assert "openlakeforge.yaml" in result.output
    assert profile.read_text(encoding="utf-8") == broken
    assert governance.read_text(encoding="utf-8") == FLAT
