from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from openlakeforge_core import load_governance_descriptor
from typer.testing import CliRunner

from olf.cli import app
from olf.common.governance_migration import MigrationError, migrate_governance_text

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
