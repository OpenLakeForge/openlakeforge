from __future__ import annotations

from pathlib import Path

import pytest

from olf.common.contracts_check._alert_policy import _check_alert_policy_conformance

ROOT = Path(__file__).parents[4]
SCHEMAS = ROOT / "docs/schema"
EXAMPLE = """\
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy
routes:
  on-call: on-call
  sales: sales-webhook
defaults:
  notify: [on-call]
rules:
  - match: {domain: "*", product: "*"}
    stages: [dev]
    when:
      run_failed: {}
  - match: {domain: sales, product: "*"}
    notify: [sales]
    when:
      quality: {on: error}
  - match: {domain: sales, product: order_revenue}
    when:
      freshness: {warn_after: 26h}
"""


def _project(tmp_path: Path, alerts: str | None) -> Path:
    code = tmp_path / "lakehouse_code"
    code.mkdir()
    (code / "governance.yml").write_text((ROOT / "lakehouse_code/governance.yml").read_text(encoding="utf-8"))
    if alerts is not None:
        (code / "alerts.yml").write_text(alerts, encoding="utf-8")
    return tmp_path


def test_contracts_check_is_silent_without_alerts_yml(tmp_path: Path) -> None:
    result = _check_alert_policy_conformance(_project(tmp_path, None), schema_root=SCHEMAS)
    assert result.ok and result.detail == "no alerts.yml"


def test_contracts_check_accepts_the_sample(tmp_path: Path) -> None:
    sample = (ROOT / "lakehouse_code/alerts.yml").read_text(encoding="utf-8")
    assert _check_alert_policy_conformance(_project(tmp_path, sample), schema_root=SCHEMAS).ok


def test_contracts_check_does_not_check_receiver_names(tmp_path: Path) -> None:
    text = EXAMPLE.replace("sales-webhook", "no-such-receiver")
    assert _check_alert_policy_conformance(_project(tmp_path, text), schema_root=SCHEMAS).ok


@pytest.mark.parametrize(
    "text,fragment",
    [
        (
            EXAMPLE.replace("domain: sales, product: order_revenue", "domain: finance, product: order_revenue"),
            "not a domain",
        ),
        (EXAMPLE.replace("notify: [sales]", "notify: [ghost]"), "not a route"),
        (EXAMPLE.replace("stages: [dev]", "stages: [staging]"), "schema violation"),
        (EXAMPLE + "  - match: {domain: '*', product: '*'}\n    when: {run_failed: {}}\n", "same specificity"),
        ("routes: [", "alerts.yml:"),
    ],
)
def test_contracts_check_rejects(tmp_path: Path, text: str, fragment: str) -> None:
    result = _check_alert_policy_conformance(_project(tmp_path, text), schema_root=SCHEMAS)
    assert not result.ok and fragment in result.detail
