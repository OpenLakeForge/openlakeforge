"""The platform API may reach Trino for a browser session's queries (#799).

How the local root wires the token and the query endpoints is planned in
infra/terraform/environments/local/tests/wiring.tftest.hcl (run by `olf check infra`).
"""

from __future__ import annotations

from pathlib import Path

TERRAFORM = Path(__file__).resolve().parents[4] / "infra/terraform"


def test_the_platform_api_may_reach_the_trino_coordinator_on_its_tls_port() -> None:
    import yaml

    flows = yaml.safe_load((TERRAFORM.parents[1] / "release/network-flows.yaml").read_text())["flows"]
    rows = [f for f in flows if f["id"] == "F36"]
    assert [(r["from"]["pods"], r["to"]["pods"]["app.kubernetes.io/component"], r["ports"]) for r in rows] == [
        ({"app.kubernetes.io/name": "platform-api"}, "coordinator", [8443])
    ]
