"""Dex overrides only error.html, and names the GitHub causes of a refused sign-in (#438)."""

from __future__ import annotations

from pathlib import Path

MAIN = Path(__file__).resolve().parents[3] / "infra/terraform/modules/identity/dex/main.tf"


def test_error_page_is_mounted_over_the_stock_template_with_both_causes() -> None:
    source = MAIN.read_text()
    assert 'mount_path = "/srv/dex/web/templates/error.html"' in source
    assert '"error.html"  = local.error_html' in source
    assert "not installed on ${var.github.organization}" in source
    assert "docs/guides/identity/connect-github.md#troubleshooting" in source
