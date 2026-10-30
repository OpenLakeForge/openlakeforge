from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_TF = Path(__file__).parents[3] / "infra/terraform/modules/query/trino/ui.tf"
pytestmark = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform is not installed")

HARNESS = """
variable "web_ui_fixed_user" {
  type    = string
  default = null
}
output "properties" { value = local.ui_properties }
"""


def render(tmp_path: Path, user: str | None) -> list[str]:
    (tmp_path / "ui.tf").write_text(UI_TF.read_text())
    (tmp_path / "harness.tf").write_text(HARNESS)
    (tmp_path / "vars.tfvars.json").write_text(json.dumps({"web_ui_fixed_user": user}))
    for args in (["init", "-backend=false", "-input=false"], ["apply", "-auto-approve", "-var-file=vars.tfvars.json"]):
        subprocess.run(["terraform", *args, "-no-color"], cwd=tmp_path, check=True, capture_output=True)
    out = subprocess.run(["terraform", "output", "-json"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return list(json.loads(out.stdout)["properties"]["value"])


def test_plain_http_route_renders_a_fixed_web_ui_user(tmp_path: Path) -> None:
    assert render(tmp_path, "olf-web") == ["web-ui.authentication.type=fixed", "web-ui.user=olf-web"]


def test_https_route_renders_nothing(tmp_path: Path) -> None:
    assert render(tmp_path, None) == []
