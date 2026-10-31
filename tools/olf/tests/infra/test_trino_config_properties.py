"""Trino's config.properties lines as Terraform renders them, evaluated without any provider.

Chart 1.42.2 reads free-form config.properties lines only from the top-level
additionalConfigProperties; a coordinator-level list is ignored. query.max-stage-count
once rode in such a list, so Trino kept its default of 150 and refused a Gold
query over 75 tables at 152 stages (#706).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[4] / "infra/terraform/modules/query/trino"

pytestmark = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform is not installed")

# Stands in for what main.tf, ui.tf, oidc.tf and people.tf define, so config.tf is evaluated alone.
HARNESS = """
variable "query_sizing" {
  type    = object({ max_stage_count = number })
  default = null
}
variable "auth_enabled" { type = bool }
variable "ui_properties" { type = list(string) }
variable "oidc_properties" { type = list(string) }
locals {
  auth_enabled          = var.auth_enabled
  ui_properties         = var.ui_properties
  oidc_enabled          = length(var.oidc_properties) > 0
  oidc_properties       = var.oidc_properties
  people_jwt_properties = []
}
output "properties" { value = local.config_properties }
"""

AUTH = [
    "http-server.process-forwarded=true",
    "http-server.http.enabled=false",
    "discovery.uri=https://localhost:8443",
    "internal-communication.https.required=true",
    "internal-communication.https.truststore.path=/etc/trino/tls/ca.crt",
    "internal-communication.shared-secret=${ENV:TRINO_INTERNAL_SECRET}",
]
STAGES = "query.max-stage-count=300"


def render(
    tmp_path: Path,
    *,
    sizing: bool = True,
    auth: bool = False,
    ui: list[str] | None = None,
    oidc: list[str] | None = None,
) -> list[str]:
    (tmp_path / "config.tf").write_text((MODULE / "config.tf").read_text())
    (tmp_path / "harness.tf").write_text(HARNESS)
    variables = {
        "query_sizing": {"max_stage_count": 300} if sizing else None,
        "auth_enabled": auth,
        "ui_properties": ui or [],
        "oidc_properties": oidc or [],
    }
    (tmp_path / "vars.tfvars.json").write_text(json.dumps(variables))
    # A module with no provider needs no download, so this runs offline.
    for args in (["init", "-backend=false", "-input=false"], ["apply", "-auto-approve", "-var-file=vars.tfvars.json"]):
        subprocess.run(["terraform", *args, "-no-color"], cwd=tmp_path, check=True, capture_output=True)
    out = subprocess.run(["terraform", "output", "-json"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return list(json.loads(out.stdout)["properties"]["value"])


def test_the_stage_count_reaches_the_top_level_list_without_authentication(tmp_path: Path) -> None:
    assert render(tmp_path) == ["http-server.process-forwarded=true", STAGES]


def test_the_stage_count_follows_the_password_level_lines(tmp_path: Path) -> None:
    assert render(tmp_path, auth=True) == [*AUTH, STAGES]


def test_the_stage_count_follows_a_fixed_web_ui_user(tmp_path: Path) -> None:
    ui = ["web-ui.authentication.type=fixed", "web-ui.user=olf-web"]

    assert render(tmp_path, ui=ui) == ["http-server.process-forwarded=true", *ui, STAGES]


def test_the_stage_count_follows_the_sign_in_properties(tmp_path: Path) -> None:
    oidc = ["web-ui.authentication.type=oauth2"]

    assert render(tmp_path, auth=True, oidc=oidc) == [*AUTH, *oidc, STAGES]


def test_without_a_query_size_only_the_base_line_is_rendered(tmp_path: Path) -> None:
    assert render(tmp_path, sizing=False) == ["http-server.process-forwarded=true"]


def test_main_renders_the_list_last_and_no_role_level_list() -> None:
    main = (MODULE / "main.tf").read_text()

    assert "additionalConfigProperties = local.config_properties" in main
    assert "additionalConfigProperties = role" not in main
