"""The Trino Web UI sign-in properties (ADR 0016) as Terraform renders them, evaluated without any provider."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

OIDC_TF = Path(__file__).resolve().parents[3] / "infra/terraform/modules/query/trino/oidc.tf"

pytestmark = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform is not installed")

HARNESS = """
variable "web_ui_oidc" {
  type = any
}
output "properties" { value = local.oidc_properties }
output "env" { value = local.oidc_env }
"""


def render(tmp_path: Path, web_ui_oidc: dict[str, object] | None) -> dict[str, object]:
    (tmp_path / "oidc.tf").write_text(OIDC_TF.read_text())
    (tmp_path / "harness.tf").write_text(HARNESS)
    (tmp_path / "vars.tfvars.json").write_text(json.dumps({"web_ui_oidc": web_ui_oidc}))
    # A module with no provider needs no download, so this runs offline.
    for args in (["init", "-backend=false", "-input=false"], ["apply", "-auto-approve", "-var-file=vars.tfvars.json"]):
        subprocess.run(["terraform", *args, "-no-color"], cwd=tmp_path, check=True, capture_output=True)
    out = subprocess.run(["terraform", "output", "-json"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return {key: value["value"] for key, value in json.loads(out.stdout).items()}


def test_dex_issuer_renders_oauth2_ui_with_a_secret_reference_only(tmp_path: Path) -> None:
    rendered = render(
        tmp_path,
        {
            "issuer_url": "https://auth.example.test",
            "client_id": "trino",
            "secret_ref": {"name": "dex-client-trino", "key": "client-secret"},
        },
    )

    properties = dict(line.split("=", 1) for line in rendered["properties"])  # type: ignore[attr-defined]
    prefix = "http-server.authentication.oauth2"
    assert properties["web-ui.authentication.type"] == "oauth2"
    assert properties[f"{prefix}.issuer"] == "https://auth.example.test"
    # Discovery would block Trino's startup on a route Traefik serves later.
    assert properties[f"{prefix}.oidc.discovery"] == "false"
    assert properties[f"{prefix}.auth-url"] == "https://auth.example.test/auth"
    assert properties[f"{prefix}.token-url"] == "https://auth.example.test/token"
    assert properties[f"{prefix}.jwks-url"] == "https://auth.example.test/keys"
    assert properties[f"{prefix}.client-id"] == "trino"
    assert properties[f"{prefix}.principal-field"] == "email"
    assert "email" in properties[f"{prefix}.scopes"].split(",")
    assert properties["oauth2-jwk.http-client.trust-store-path"] == "/etc/trino/tls/ca.crt"
    # The secret reaches Trino through the environment, never as a literal value.
    assert properties[f"{prefix}.client-secret"] == "${ENV:TRINO_OIDC_CLIENT_SECRET}"
    assert rendered["env"] == [
        {
            "name": "TRINO_OIDC_CLIENT_SECRET",
            "valueFrom": {"secretKeyRef": {"name": "dex-client-trino", "key": "client-secret"}},
        }
    ]


def test_without_an_issuer_nothing_is_rendered(tmp_path: Path) -> None:
    assert render(tmp_path, None) == {"properties": [], "env": []}
