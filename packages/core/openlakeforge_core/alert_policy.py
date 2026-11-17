"""The ``AlertPolicy`` descriptor at ``lakehouse_code/alerts.yml`` (ADR 0023, alert policy).

It says which conditions alert for which products and stages, with which
options, and through which named routes. Receivers and their credentials stay
in the Deployment Profile; a route is only a name for one of them.

``resolve_conditions`` and ``resolve_policy`` are pure, so the evaluator can
call them without reading a file or a cluster.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from .descriptors import LakehouseDescriptorError

ALERT_POLICY_API_VERSION = "openlakeforge.io/v1beta1"
ALERT_POLICY_KIND = "AlertPolicy"
STAGES = ("dev", "uat", "prod")
WILDCARD = "*"
# A product's Dagster job; jobs of no product (gold_maintenance, ...) are matched by the global rule only.
JOB_SUFFIX = "_pipeline"

_NAME = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
_SELECTOR = re.compile(r"^[a-z][a-z0-9_]*$")
_DURATION = re.compile(r"^([1-9][0-9]*)([mhd])$")
_UNIT = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}
_SEVERITIES = ("warning", "critical")
DEFAULT_GRACE = timedelta(minutes=15)


def parse_duration(value: object, *, where: str) -> timedelta:
    """``30m``, ``26h`` or ``2d``: a positive integer and one unit."""
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise LakehouseDescriptorError(
            f"{where} must be a positive integer with m, h or d (30m, 26h, 2d), got {value!r}"
        )
    return int(match[1]) * _UNIT[match[2]]


@dataclass(frozen=True)
class RunFailed:
    severity: str = "critical"


@dataclass(frozen=True)
class MissedRun:
    grace: timedelta = DEFAULT_GRACE
    severity: str = "critical"


@dataclass(frozen=True)
class Freshness:
    """Age thresholds when either is set; otherwise cadence-derived staleness with ``grace`` and ``severity``."""

    warn_after: timedelta | None = None
    critical_after: timedelta | None = None
    grace: timedelta = DEFAULT_GRACE
    severity: str = "critical"


@dataclass(frozen=True)
class Quality:
    level: str = "warn"


@dataclass(frozen=True)
class ScheduleMismatch:
    severity: str = "warning"


ConditionOptions = RunFailed | MissedRun | Freshness | Quality | ScheduleMismatch
CONDITIONS: tuple[str, ...] = ("run_failed", "missed_run", "freshness", "quality", "schedule_mismatch")


@dataclass(frozen=True)
class AlertRule:
    domain: str
    product: str
    when: Mapping[str, ConditionOptions | None]
    stages: tuple[str, ...] | None = None
    notify: tuple[str, ...] | None = None

    @property
    def specificity(self) -> int:
        """2 names a product, 1 a domain with every product, 0 is global."""
        if self.product != WILDCARD:
            return 2
        return 0 if self.domain == WILDCARD else 1


@dataclass(frozen=True)
class AlertPolicy:
    routes: Mapping[str, str]
    rules: tuple[AlertRule, ...]
    default_stages: tuple[str, ...] = STAGES
    default_notify: tuple[str, ...] = ()

    def stages_of(self, rule: AlertRule) -> tuple[str, ...]:
        return rule.stages if rule.stages is not None else self.default_stages

    def notify_of(self, rule: AlertRule) -> tuple[str, ...]:
        return rule.notify if rule.notify is not None else self.default_notify


@dataclass(frozen=True)
class ResolvedCondition:
    """The winning rule's answer for one condition; ``options`` is None when the rule turned it off."""

    options: ConditionOptions | None
    routes: tuple[str, ...]
    rule: int  # index into AlertPolicy.rules, for `olf alert explain`

    @property
    def enabled(self) -> bool:
        return self.options is not None


@dataclass(frozen=True)
class ResolvedPolicy:
    """Per Dagster job, the conditions some rule covers. A condition absent from a job's map does not alert."""

    stage: str
    jobs: Mapping[str, Mapping[str, ResolvedCondition]] = field(default_factory=dict)
    unmatched: Mapping[str, ResolvedCondition] = field(default_factory=dict)

    def for_job(self, job: str) -> Mapping[str, ResolvedCondition]:
        return self.jobs.get(job, self.unmatched)


def _object(
    value: object, allowed: set[str], *, where: str, required: frozenset[str] | set[str] = frozenset()
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LakehouseDescriptorError(f"{where} must be an object")
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise LakehouseDescriptorError(f"{where} must not contain {unexpected!r}")
    missing = sorted(required - set(value))
    if missing:
        raise LakehouseDescriptorError(f"{where} is missing {missing!r}")
    return value


def _bool_hint(value: object) -> str:
    return (
        "; YAML reads unquoted on, off, yes, no, y and n as booleans, so quote names like that"
        if isinstance(value, bool)
        else ""
    )


def _name(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise LakehouseDescriptorError(
            f"{where} must match '^[a-z]([a-z0-9-]{{0,61}}[a-z0-9])?$', got {value!r}{_bool_hint(value)}"
        )
    return value


def _name_list(value: object, *, where: str, allowed: Sequence[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LakehouseDescriptorError(f"{where} must be a non-empty list")
    if not all(isinstance(item, str) for item in value):
        hint = _bool_hint(next((i for i in value if isinstance(i, bool)), None))
        raise LakehouseDescriptorError(f"{where} must list strings, got {value!r}{hint}")
    items = tuple(value)
    if len(set(items)) != len(items):
        raise LakehouseDescriptorError(f"{where} must not repeat an entry")
    for item in items:
        if allowed is not None:
            if item not in allowed:
                raise LakehouseDescriptorError(f"{where}: {item!r} is not one of {list(allowed)!r}")
        else:
            _name(item, where=where)
    return items


def _severity(options: Mapping[str, Any], default: str, *, where: str) -> str:
    severity = options.get("severity", default)
    if severity not in _SEVERITIES:
        raise LakehouseDescriptorError(f"{where}.severity must be one of {list(_SEVERITIES)!r}")
    return str(severity)


def _duration(options: Mapping[str, Any], key: str, default: timedelta, *, where: str) -> timedelta:
    return parse_duration(options[key], where=f"{where}.{key}") if key in options else default


def _condition(name: str, value: object, *, where: str) -> ConditionOptions | None:
    if value is False:
        return None
    here = f"{where}.{name}"
    if name == "run_failed":
        options = _object(value, {"severity"}, where=here)
        return RunFailed(_severity(options, "critical", where=here))
    if name == "missed_run":
        options = _object(value, {"grace", "severity"}, where=here)
        return MissedRun(
            _duration(options, "grace", DEFAULT_GRACE, where=here), _severity(options, "critical", where=here)
        )
    if name == "freshness":
        options = _object(value, {"warn_after", "critical_after", "grace", "severity"}, where=here)
        thresholds = {"warn_after", "critical_after"} & set(options)
        if thresholds and {"grace", "severity"} & set(options):
            raise LakehouseDescriptorError(
                f"{here}: warn_after and critical_after do not mix with grace and severity; "
                "thresholds measure the age of the last success, grace and severity belong to cadence-derived staleness"
            )
        warn = _duration(options, "warn_after", timedelta(0), where=here) if "warn_after" in options else None
        critical = (
            _duration(options, "critical_after", timedelta(0), where=here) if "critical_after" in options else None
        )
        if warn is not None and critical is not None and warn >= critical:
            raise LakehouseDescriptorError(f"{here}: warn_after must be below critical_after")
        return Freshness(
            warn,
            critical,
            _duration(options, "grace", DEFAULT_GRACE, where=here),
            _severity(options, "critical", where=here),
        )
    if name == "quality":
        # YAML 1.1 loads the key `on` as True, which is how an old `quality: {on: error}` arrives.
        if isinstance(value, Mapping) and True in value:
            raise LakehouseDescriptorError(f"{here}: quality uses `level`, not `on` (quality: {{level: warn|error}})")
        options = _object(value, {"level"}, where=here)
        if options.get("level", "warn") not in ("warn", "error"):
            raise LakehouseDescriptorError(f"{here}.level must be 'warn' or 'error'")
        return Quality(options.get("level", "warn"))
    options = _object(value, {"severity"}, where=here)
    return ScheduleMismatch(_severity(options, "warning", where=here))


def _selector(value: object, *, where: str) -> str:
    if value != WILDCARD and not (isinstance(value, str) and _SELECTOR.fullmatch(value)):
        raise LakehouseDescriptorError(f"{where} must be an identifier or '*', got {value!r}{_bool_hint(value)}")
    return str(value)


def _rule(raw: object, *, where: str) -> AlertRule:
    entry = _object(raw, {"match", "stages", "notify", "when"}, where=where, required={"match", "when"})
    match = _object(entry["match"], {"domain", "product"}, where=f"{where}.match", required={"domain", "product"})
    when = entry["when"]
    if not isinstance(when, Mapping) or not when:
        raise LakehouseDescriptorError(f"{where}.when must be a non-empty object")
    unknown = sorted(set(when) - set(CONDITIONS))
    if unknown:
        raise LakehouseDescriptorError(
            f"{where}.when must not contain {unknown!r}; conditions are {list(CONDITIONS)!r} "
            "(platform conditions are routed by the Deployment Profile)"
        )
    return AlertRule(
        domain=_selector(match["domain"], where=f"{where}.match.domain"),
        product=_selector(match["product"], where=f"{where}.match.product"),
        when={name: _condition(name, options, where=f"{where}.when") for name, options in when.items()},
        stages=_name_list(entry["stages"], where=f"{where}.stages", allowed=STAGES) if "stages" in entry else None,
        notify=_name_list(entry["notify"], where=f"{where}.notify") if "notify" in entry else None,
    )


def _scopes_overlap(a: AlertRule, b: AlertRule) -> bool:
    if a.specificity != b.specificity:
        return False
    if a.specificity == 2:
        return a.product == b.product and (a.domain == b.domain or WILDCARD in (a.domain, b.domain))
    return a.domain == b.domain


def _semantic_errors(policy: AlertPolicy, governance: Mapping[str, Any] | None, source: str) -> list[str]:
    errors: list[str] = []
    for index, rule in enumerate(policy.rules):
        where = f"{source}: rules[{index}]"
        notify = policy.notify_of(rule)
        if not notify:
            errors.append(f"{where} resolves to no route; set notify on the rule or defaults.notify")
        errors += [
            f"{where}: notify {r!r} is not a route (routes: {sorted(policy.routes)})"
            for r in notify
            if r not in policy.routes
        ]
        if governance is not None:
            errors += _match_errors(rule, governance, where)
    errors += [
        f"{source}: defaults.notify {r!r} is not a route" for r in policy.default_notify if r not in policy.routes
    ]
    for i, first in enumerate(policy.rules):
        for j in range(i + 1, len(policy.rules)):
            second = policy.rules[j]
            if not _scopes_overlap(first, second):
                continue
            stages = sorted(set(policy.stages_of(first)) & set(policy.stages_of(second)), key=STAGES.index)
            shared = sorted(set(first.when) & set(second.when), key=CONDITIONS.index)
            if stages and shared:
                errors.append(
                    f"{source}: rules[{i}] and rules[{j}] have the same specificity and both set {shared} "
                    f"for stage(s) {stages}; make one more specific or split their stages"
                )
    return errors


def _match_errors(rule: AlertRule, governance: Mapping[str, Any], where: str) -> list[str]:
    domains = {d["name"]: {p["id"] for p in d.get("products", [])} for d in governance["domains"]}
    owner = {p: d for d, products in domains.items() for p in products}
    errors = []
    if rule.domain != WILDCARD and rule.domain not in domains:
        errors.append(f"{where}: match.domain {rule.domain!r} is not a domain of governance.yml")
    elif rule.product != WILDCARD and rule.product not in owner:
        errors.append(f"{where}: match.product {rule.product!r} is not a product of governance.yml")
    elif rule.product != WILDCARD and rule.domain != WILDCARD and owner[rule.product] != rule.domain:
        errors.append(
            f"{where}: product {rule.product!r} is nested under domain {owner[rule.product]!r}, not {rule.domain!r}"
        )
    return errors


def validate_alert_policy(
    policy: AlertPolicy, governance: Mapping[str, Any] | None = None, *, source: str = "alerts.yml"
) -> None:
    """Routes, ``match`` ids and same-level overlaps. ``governance`` is a validated ``governance.yml`` document."""
    errors = _semantic_errors(policy, governance, source)
    if errors:
        raise LakehouseDescriptorError("; ".join(errors))


def parse_alert_policy(document: Mapping[str, Any], *, source: str = "alerts.yml") -> AlertPolicy:
    """The structural half: envelope, fields and option values. Raises on the first violation."""
    try:
        root = _object(
            document,
            {"apiVersion", "kind", "routes", "defaults", "rules"},
            where="document",
            required={"apiVersion", "kind", "routes", "rules"},
        )
        if root["apiVersion"] != ALERT_POLICY_API_VERSION:
            raise LakehouseDescriptorError(
                f"unsupported apiVersion {root['apiVersion']!r}; expected {ALERT_POLICY_API_VERSION!r}"
            )
        if root["kind"] != ALERT_POLICY_KIND:
            raise LakehouseDescriptorError(f"kind must be {ALERT_POLICY_KIND!r}")
        routes = root["routes"]
        if not isinstance(routes, Mapping) or not routes:
            raise LakehouseDescriptorError("routes must be a non-empty object")
        for route, receiver in routes.items():
            _name(route, where="routes key")
            _name(receiver, where=f"routes.{route}")
        defaults = _object(root.get("defaults", {}), {"stages", "notify"}, where="defaults")
        rules = root["rules"]
        if not isinstance(rules, list) or not rules:
            raise LakehouseDescriptorError("rules must be a non-empty list")
        return AlertPolicy(
            routes=dict(routes),
            rules=tuple(_rule(rule, where=f"rules[{i}]") for i, rule in enumerate(rules)),
            default_stages=_name_list(defaults["stages"], where="defaults.stages", allowed=STAGES)
            if "stages" in defaults
            else STAGES,
            default_notify=_name_list(defaults["notify"], where="defaults.notify") if "notify" in defaults else (),
        )
    except LakehouseDescriptorError as exc:
        raise LakehouseDescriptorError(f"{source}: {exc}") from None


def load_alert_policy(path: str | Path, governance: Mapping[str, Any] | None = None) -> AlertPolicy:
    """Load, parse and validate the AlertPolicy at ``path``. ``governance`` enables the ``match`` id checks."""
    source = str(path)
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise LakehouseDescriptorError(f"{source}: descriptor must contain a YAML object")
    policy = parse_alert_policy(document, source=source)
    validate_alert_policy(policy, governance, source=source)
    return policy


def resolve_conditions(
    policy: AlertPolicy, stage: str, *, domain: str | None = None, product: str | None = None
) -> dict[str, ResolvedCondition]:
    """Per condition, the most specific matching rule for the stage wins, with its own ``notify``.

    A job of no product passes neither ``domain`` nor ``product`` and is matched by global rules only. The
    result holds a condition only when some rule names it; a ``false`` rule yields a disabled entry. Same-level
    overlap is a validation error, so at most one rule wins per condition.
    """
    best: dict[str, tuple[int, int]] = {}
    for index, rule in enumerate(policy.rules):
        if stage not in policy.stages_of(rule):
            continue
        if rule.product != WILDCARD and rule.product != product:
            continue
        if rule.domain != WILDCARD and rule.domain != domain:
            continue
        for condition in rule.when:
            if condition not in best or rule.specificity > best[condition][0]:
                best[condition] = (rule.specificity, index)
    return {
        condition: ResolvedCondition(
            options=policy.rules[index].when[condition], routes=policy.notify_of(policy.rules[index]), rule=index
        )
        for condition, (_, index) in best.items()
    }


def resolve_policy(policy: AlertPolicy, governance: Mapping[str, Any], stage: str) -> ResolvedPolicy:
    """Resolve every product job of ``governance`` for ``stage``; any other job gets the global rules."""
    jobs = {
        f"{product['id']}{JOB_SUFFIX}": resolve_conditions(policy, stage, domain=domain["name"], product=product["id"])
        for domain in governance["domains"]
        for product in domain.get("products", [])
    }
    return ResolvedPolicy(stage=stage, jobs=jobs, unmatched=resolve_conditions(policy, stage))
