from __future__ import annotations

import copy
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from openlakeforge_core import (
    LakehouseDescriptorError,
    load_alert_policy,
    load_alert_policy_document,
    load_governance_descriptor,
    parse_alert_policy,
    resolve_conditions,
    resolve_policy,
    validate_alert_policy,
)
from openlakeforge_core.alert_policy import Freshness, MissedRun, Quality, RunFailed, ScheduleMismatch, parse_duration

ROOT = Path(__file__).parents[4]
SCHEMAS = ROOT / "docs/schema"
SCHEMA = json.loads((SCHEMAS / "alert-policy.schema.json").read_text(encoding="utf-8"))
GOVERNANCE = {
    "domains": [
        {"name": "sales", "products": [{"id": "order_revenue"}, {"id": "customer_health"}]},
        {"name": "supply_chain", "products": [{"id": "inventory_reliability"}]},
    ]
}
# The ADR's example policy.
EXAMPLE = """\
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy
routes:
  on-call: on-call
  sales: sales-webhook
  data-eng: data-eng-chat
defaults:
  stages: [prod]
  notify: [on-call]
rules:
  - match: {domain: "*", product: "*"}
    when:
      run_failed: {}
      missed_run: {}
      freshness: {}
      schedule_mismatch: {}
      quality: {on: error}
  - match: {domain: "*", product: "*"}
    stages: [dev]
    notify: [data-eng]
    when:
      run_failed: {severity: warning}
  - match: {domain: sales, product: "*"}
    notify: [sales]
    when:
      quality: {on: warn}
  - match: {domain: sales, product: order_revenue}
    notify: [sales, on-call]
    when:
      freshness: {warn_after: 26h, critical_after: 30h}
      missed_run: {grace: 30m}
  - match: {domain: supply_chain, product: inventory_reliability}
    when:
      freshness: false
"""


def _doc(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = load_alert_policy_document(EXAMPLE)
    document.update(overrides)
    return document


def _rules(*rules: dict[str, Any], **top: Any) -> dict[str, Any]:
    base = {
        "apiVersion": "openlakeforge.io/v1beta1",
        "kind": "AlertPolicy",
        "routes": {"on-call": "on-call", "sales": "sales-webhook"},
        "defaults": {"notify": ["on-call"]},
        "rules": list(rules),
    }
    return {**base, **top}


def _rule(domain: str = "*", product: str = "*", **fields: Any) -> dict[str, Any]:
    fields.setdefault("when", {"run_failed": {}})
    return {"match": {"domain": domain, "product": product}, **fields}


def _schema_errors(document: object) -> list[str]:
    return [e.message for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(document)]


def _model_error(document: dict[str, Any], governance: dict[str, Any] | None = GOVERNANCE) -> str:
    with pytest.raises(LakehouseDescriptorError) as raised:
        validate_alert_policy(parse_alert_policy(document), governance)
    return str(raised.value)


def test_adr_example_and_sample_policy_pass_schema_and_model() -> None:
    assert _schema_errors(load_alert_policy_document(EXAMPLE)) == []
    validate_alert_policy(parse_alert_policy(load_alert_policy_document(EXAMPLE)), GOVERNANCE)
    sample = ROOT / "lakehouse_code/alerts.yml"
    governance = load_governance_descriptor(ROOT / "lakehouse_code/governance.yml")
    assert _schema_errors(load_alert_policy_document(sample.read_text(encoding="utf-8"))) == []
    load_alert_policy(sample, governance)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(apiVersion="openlakeforge.io/v1beta2"),
        lambda d: d.update(kind="Governance"),
        lambda d: d.update(routes={}),
        lambda d: d.update(routes={"On_Call": "x"}),
        lambda d: d.update(rules=[]),
        lambda d: d.pop("rules"),
        lambda d: d["defaults"].update(stages=["staging"]),
        lambda d: d["defaults"].update(stages=["prod", "prod"]),
        lambda d: d["defaults"].update(notify=[]),
        lambda d: d["rules"][0].update(extra=1),
        lambda d: d["rules"][0]["match"].pop("product"),
        lambda d: d["rules"][0]["match"].update(product="Order Revenue"),
        lambda d: d["rules"][0].update(when={}),
        lambda d: d["rules"][0]["when"].update(stage_unreadable={}),
        lambda d: d["rules"][0]["when"].update(policy_unreadable={}),
        lambda d: d["rules"][0]["when"].update(run_failed={"severity": "info"}),
        lambda d: d["rules"][0]["when"].update(run_failed=True),
        lambda d: d["rules"][0]["when"].update(quality={"on": "fail"}),
        lambda d: d["rules"][0]["when"].update(missed_run={"grace": "0m"}),
        lambda d: d["rules"][0]["when"].update(missed_run={"grace": "1w"}),
        lambda d: d["rules"][0]["when"].update(missed_run={"grace": 15}),
        lambda d: d["rules"][0]["when"].update(freshness={"warn_after": "2h", "grace": "5m"}),
        lambda d: d["rules"][0]["when"].update(freshness={"critical_after": "2h", "severity": "warning"}),
        lambda d: d["rules"][0]["when"].update(freshness={"bogus": "2h"}),
    ],
)
def test_schema_rejects(mutate: Any) -> None:
    document = copy.deepcopy(load_alert_policy_document(EXAMPLE))
    mutate(document)
    assert _schema_errors(document)


@pytest.mark.parametrize(
    "when,message",
    [
        ({"freshness": {"warn_after": "30h", "critical_after": "30h"}}, "warn_after must be below critical_after"),
        ({"freshness": {"warn_after": "2d", "critical_after": "30h"}}, "warn_after must be below critical_after"),
        ({"freshness": {"warn_after": "2h", "grace": "5m"}}, "do not mix"),
        ({"missed_run": {"grace": "soon"}}, "positive integer"),
        ({"quality": {"on": "info"}}, "'warn' or 'error'"),
        ({"stage_unreadable": {}}, "platform conditions"),
        ({"run_failed": {"severity": "fatal"}}, "severity"),
        ({}, "non-empty"),
    ],
)
def test_model_rejects_structure(when: dict[str, Any], message: str) -> None:
    with pytest.raises(LakehouseDescriptorError, match=message):
        parse_alert_policy(_rules(_rule(when=when)))


def test_model_rejects_envelope() -> None:
    for bad, message in (
        ({"apiVersion": "openlakeforge.io/v1beta2"}, "unsupported apiVersion"),
        ({"kind": "Governance"}, "kind must be"),
        ({"routes": {}}, "non-empty"),
        ({"surprise": 1}, "must not contain"),
        ({"rules": []}, "non-empty"),
        ({"defaults": {"stages": ["staging"]}}, "not one of"),
    ):
        with pytest.raises(LakehouseDescriptorError, match=message):
            parse_alert_policy(_rules(_rule(), **bad))
    with pytest.raises(LakehouseDescriptorError, match="alerts.yml: .*unsupported"):
        parse_alert_policy({"apiVersion": "x", "kind": "AlertPolicy", "routes": {"a": "a"}, "rules": [_rule()]})


@pytest.mark.parametrize(
    ("text", "expected"), [("30m", timedelta(minutes=30)), ("26h", timedelta(hours=26)), ("2d", timedelta(days=2))]
)
def test_duration_units(text: str, expected: timedelta) -> None:
    assert parse_duration(text, where="x") == expected


@pytest.mark.parametrize("text", ["", "0m", "-5m", "1.5h", "90", "h", "5s", "05m", 30, None])
def test_duration_rejects(text: object) -> None:
    with pytest.raises(LakehouseDescriptorError):
        parse_duration(text, where="x")


def test_condition_defaults() -> None:
    policy = parse_alert_policy(
        _rules(_rule(when={c: {} for c in ("run_failed", "missed_run", "freshness", "quality", "schedule_mismatch")}))
    )
    when = policy.rules[0].when
    assert when["run_failed"] == RunFailed("critical")
    assert when["missed_run"] == MissedRun(timedelta(minutes=15), "critical")
    assert when["freshness"] == Freshness(None, None, timedelta(minutes=15), "critical")
    assert when["quality"] == Quality("warn")
    assert when["schedule_mismatch"] == ScheduleMismatch("warning")


def test_freshness_thresholds_may_be_set_alone() -> None:
    policy = parse_alert_policy(_rules(_rule(when={"freshness": {"critical_after": "2d"}})))
    assert policy.rules[0].when["freshness"] == Freshness(None, timedelta(days=2))


@pytest.mark.parametrize(
    "document,message",
    [
        (_rules(_rule(notify=["nope"])), "'nope' is not a route"),
        (_rules(_rule(), defaults={"notify": ["nope"]}), "defaults.notify 'nope' is not a route"),
        (_rules(_rule(), defaults={}), "resolves to no route"),
        (_rules(_rule(domain="finance")), "'finance' is not a domain"),
        (_rules(_rule(product="ghost")), "'ghost' is not a product"),
        (_rules(_rule(domain="supply_chain", product="order_revenue")), "nested under domain 'sales'"),
        (_rules(_rule(), _rule()), "same specificity"),
        (_rules(_rule("sales"), _rule("sales", when={"run_failed": False})), "same specificity"),
        (_rules(_rule("*", "order_revenue"), _rule("sales", "order_revenue")), "same specificity"),
        (_rules(_rule(stages=["dev", "prod"]), _rule(stages=["prod"])), r"stage\(s\) \['prod'\]"),
        (_rules(_rule(stages=["dev"]), _rule(when={"run_failed": {}})), "same specificity"),
    ],
)
def test_validation_errors(document: dict[str, Any], message: str) -> None:
    assert re.search(message, _model_error(document))


def test_validation_without_governance_skips_id_checks() -> None:
    validate_alert_policy(parse_alert_policy(_rules(_rule("finance", "ghost"))), None)


@pytest.mark.parametrize(
    "rules",
    [
        # different conditions at one level
        (_rule(when={"run_failed": {}}), _rule(when={"quality": {}})),
        # disjoint stages
        (_rule(stages=["dev"]), _rule(stages=["prod"])),
        # different scopes at one level
        (_rule("sales"), _rule("supply_chain")),
        (_rule("*", "order_revenue"), _rule("*", "customer_health")),
        # different levels
        (_rule(), _rule("sales"), _rule("sales", "order_revenue")),
    ],
)
def test_no_false_overlap(rules: tuple[dict[str, Any], ...]) -> None:
    validate_alert_policy(parse_alert_policy(_rules(*rules)), GOVERNANCE)


@pytest.fixture
def policy() -> Any:
    return parse_alert_policy(_doc())


def _kinds(resolved: dict[str, Any]) -> dict[str, Any]:
    return {name: (c.options, c.routes) for name, c in resolved.items()}


def test_resolution_matches_the_adr_table_for_order_revenue_in_prod(policy: Any) -> None:
    resolved = resolve_conditions(policy, "prod", domain="sales", product="order_revenue")
    assert resolved["run_failed"].options == RunFailed("critical") and resolved["run_failed"].routes == ("on-call",)
    assert resolved["missed_run"].options == MissedRun(timedelta(minutes=30), "critical")
    assert resolved["missed_run"].routes == ("sales", "on-call")
    assert resolved["freshness"].options == Freshness(timedelta(hours=26), timedelta(hours=30))
    assert resolved["freshness"].routes == ("sales", "on-call")
    assert resolved["quality"].options == Quality("warn") and resolved["quality"].routes == ("sales",)
    assert resolved["schedule_mismatch"].options == ScheduleMismatch("warning")


def test_winner_notify_is_not_a_union(policy: Any) -> None:
    resolved = resolve_conditions(policy, "prod", domain="sales", product="customer_health")
    assert resolved["quality"].routes == ("sales",)
    assert resolved["run_failed"].routes == ("on-call",)


def test_product_rule_takes_only_its_own_conditions(policy: Any) -> None:
    resolved = resolve_conditions(policy, "prod", domain="sales", product="order_revenue")
    assert resolved["run_failed"].rule == 0
    assert resolved["freshness"].rule == 3


def test_false_wins_over_a_less_specific_rule(policy: Any) -> None:
    resolved = resolve_conditions(policy, "prod", domain="supply_chain", product="inventory_reliability")
    assert resolved["freshness"].enabled is False
    assert resolved["freshness"].options is None
    assert resolved["run_failed"].enabled


def test_stage_filter_and_default_stages(policy: Any) -> None:
    dev = resolve_conditions(policy, "dev", domain="sales", product="order_revenue")
    assert set(dev) == {"run_failed"}
    assert dev["run_failed"].options == RunFailed("warning") and dev["run_failed"].routes == ("data-eng",)
    assert resolve_conditions(policy, "uat", domain="sales", product="order_revenue") == {}


def test_rule_stages_override_defaults() -> None:
    policy = parse_alert_policy(_rules(_rule(stages=["uat"])))
    assert resolve_conditions(policy, "uat")
    assert not resolve_conditions(policy, "prod")


def test_every_stage_without_defaults() -> None:
    policy = parse_alert_policy(_rules(_rule()))
    assert all(resolve_conditions(policy, stage) for stage in ("dev", "uat", "prod"))


def test_same_level_rules_with_disjoint_stages_resolve_per_stage() -> None:
    policy = parse_alert_policy(
        _rules(
            _rule(stages=["dev"], notify=["sales"], when={"run_failed": {"severity": "warning"}}),
            _rule(stages=["prod"], when={"run_failed": {}}),
        )
    )
    validate_alert_policy(policy)
    assert resolve_conditions(policy, "dev")["run_failed"].routes == ("sales",)
    assert resolve_conditions(policy, "prod")["run_failed"].routes == ("on-call",)


def test_wildcard_domain_with_a_named_product_is_product_level() -> None:
    policy = parse_alert_policy(
        _rules(_rule(), _rule("*", "order_revenue", notify=["sales"], when={"run_failed": {"severity": "warning"}}))
    )
    resolved = resolve_conditions(policy, "prod", domain="sales", product="order_revenue")
    assert resolved["run_failed"].options == RunFailed("warning")
    assert resolve_conditions(policy, "prod", domain="sales", product="customer_health")["run_failed"].rule == 0


def test_jobs_of_no_product_match_only_the_global_rule(policy: Any) -> None:
    unmatched = resolve_conditions(policy, "prod")
    assert set(unmatched) == {"run_failed", "missed_run", "freshness", "schedule_mismatch", "quality"}
    assert unmatched["quality"].options == Quality("error")
    only_domain = parse_alert_policy(_rules(_rule("sales")))
    assert resolve_conditions(only_domain, "prod") == {}


def test_resolve_policy_maps_product_jobs(policy: Any) -> None:
    resolved = resolve_policy(policy, GOVERNANCE, "prod")
    assert set(resolved.jobs) == {
        "order_revenue_pipeline",
        "customer_health_pipeline",
        "inventory_reliability_pipeline",
    }
    assert resolved.for_job("order_revenue_pipeline")["freshness"].options == Freshness(
        timedelta(hours=26), timedelta(hours=30)
    )
    assert resolved.for_job("gold_maintenance")["quality"].options == Quality("error")
    assert _kinds(resolved.for_job("silver_maintenance")) == _kinds(resolved.unmatched)


def test_resolve_policy_silent_unless_declared() -> None:
    only_sales = parse_alert_policy(_rules(_rule("sales")))
    resolved = resolve_policy(only_sales, GOVERNANCE, "prod")
    assert resolved.for_job("order_revenue_pipeline")
    assert resolved.for_job("inventory_reliability_pipeline") == {}
    assert resolved.for_job("gold_maintenance") == {}


def test_load_alert_policy_reports_the_path(tmp_path: Path) -> None:
    path = tmp_path / "alerts.yml"
    path.write_text("- not an object\n", encoding="utf-8")
    with pytest.raises(LakehouseDescriptorError, match="must contain a YAML object"):
        load_alert_policy(path)
    path.write_text(yaml.safe_dump(_rules(_rule(notify=["nope"]))), encoding="utf-8")
    with pytest.raises(LakehouseDescriptorError, match=r"alerts\.yml: rules\[0\]"):
        load_alert_policy(path)


def test_on_stays_a_key_not_a_yaml_boolean() -> None:
    document = load_alert_policy_document("quality: {on: error}\nflag: true\nno: false\n")
    assert document == {"quality": {"on": "error"}, "flag": True, "no": False}
