"""A policy that turns every product condition on, for tests about incidents rather than the policy."""

from __future__ import annotations

from datetime import datetime

from openlakeforge_core import AlertState, Evaluation, ResolvedPolicy, parse_alert_policy, resolve_policy
from openlakeforge_core import evaluate as evaluate_with_policy

CONDITIONS = ("run_failed", "missed_run", "freshness", "quality", "schedule_mismatch")


GOVERNANCE = {"domains": [{"name": "sales", "products": [{"id": "orders"}, {"id": "refunds"}]}]}


def policy_for(*rules: dict, stages: tuple[str, ...] = ("dev", "prod")) -> dict[str, ResolvedPolicy]:
    """Each rule is a `rules[]` entry of alerts.yml; routes `ops` and `sales`, and the default notify, are filled in."""
    document = {
        "apiVersion": "openlakeforge.io/v1beta1",
        "kind": "AlertPolicy",
        "routes": {"ops": "ops", "sales": "sales"},
        "defaults": {"notify": ["ops"]},
        "rules": list(rules),
    }
    parsed = parse_alert_policy(document)
    return {stage: resolve_policy(parsed, GOVERNANCE, stage) for stage in stages}


ALL_ON = policy_for({"match": {"domain": "*", "product": "*"}, "when": {name: {} for name in CONDITIONS}})


def evaluate(previous: AlertState, signals, now: datetime, **options) -> Evaluation:
    return evaluate_with_policy(previous, signals, now, **{"policy": ALL_ON, **options})
