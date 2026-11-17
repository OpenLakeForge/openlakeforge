from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from olf.cli import app

runner = CliRunner()
ROOT = Path(__file__).parents[4]
ALERTS = """\
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy
routes:
  on-call: on-call
  sales: sales-webhook
defaults:
  stages: [prod]
  notify: [on-call]
rules:
  - match: {domain: "*", product: "*"}
    when:
      run_failed: {}
      freshness: {}
      quality: {level: error}
  - match: {domain: sales, product: "*"}
    notify: [sales]
    when:
      quality: {level: warn}
  - match: {domain: sales, product: order_revenue}
    notify: [sales, on-call]
    when:
      freshness: {warn_after: 26h, critical_after: 30h}
      run_failed: false
"""


def _project(tmp_path: Path, alerts: str | None = ALERTS) -> Path:
    code = tmp_path / "lakehouse_code"
    code.mkdir()
    (code / "governance.yml").write_text((ROOT / "lakehouse_code/governance.yml").read_text(encoding="utf-8"))
    if alerts is not None:
        (code / "alerts.yml").write_text(alerts, encoding="utf-8")
    return tmp_path


def _explain(project: Path, stage: str, product: str) -> tuple[int, str]:
    result = runner.invoke(app, ["alert", "explain", "--stage", stage, "--product", product, "--project", str(project)])
    return result.exit_code, result.output


def test_explain_names_the_winning_rule_options_and_routes_per_condition(tmp_path: Path) -> None:
    code, out = _explain(_project(tmp_path), "prod", "order_revenue")
    assert code == 0
    assert "run_failed: off by rules[2] (sales/order_revenue)" in out
    assert "freshness: warn_after=26h, critical_after=30h; rules[2]" in out
    assert "notifies sales -> sales-webhook, on-call -> on-call" in out
    assert "quality: level=warn; rules[1] (sales/*); notifies sales -> sales-webhook" in out
    assert "schedule_mismatch: not declared" in out


def test_explain_uses_the_less_specific_rule_for_another_product(tmp_path: Path) -> None:
    code, out = _explain(_project(tmp_path), "prod", "inventory_reliability")
    assert code == 0
    assert "quality: level=error; rules[0] (*/*); notifies on-call -> on-call" in out
    assert "run_failed: severity=critical; rules[0]" in out


def test_explain_for_a_stage_no_rule_covers_says_nothing_alerts(tmp_path: Path) -> None:
    code, out = _explain(_project(tmp_path), "dev", "order_revenue")
    assert code == 0
    assert out.count("not declared, does not alert") == 5


def test_explain_without_alerts_yml_is_not_an_error(tmp_path: Path) -> None:
    code, out = _explain(_project(tmp_path, None), "prod", "order_revenue")
    assert code == 0 and "No alerts.yml" in out


@pytest.mark.parametrize(
    ("product", "stage", "alerts", "code", "fragment"),
    [
        ("ghost", "prod", ALERTS, 1, "'ghost' is not a product"),
        ("order_revenue", "staging", ALERTS, 2, "unknown stage"),
        ("order_revenue", "prod", ALERTS.replace("[sales, on-call]", "[nope]"), 1, "not a route"),
        ("order_revenue", "prod", "routes: [", 1, "alerts.yml"),
    ],
)
def test_explain_failures(tmp_path: Path, product: str, stage: str, alerts: str, code: int, fragment: str) -> None:
    exit_code, out = _explain(_project(tmp_path, alerts), stage, product)
    assert exit_code == code and fragment in out


def test_explain_reads_the_sample_project() -> None:
    code, out = _explain(ROOT, "prod", "order_revenue")
    assert code == 0 and "missed_run" in out and "on-call -> on-call" in out
