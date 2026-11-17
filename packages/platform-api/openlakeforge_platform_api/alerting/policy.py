"""A stage's alert policy, read from the revision the stage is running (ADR 0023, decision 10).

Per stage: `activations/<stage>/ACTIVE.json`, the activation it names, that revision's sidecar, then `alerts.yml`
and `governance.yml` from its `descriptors` component, each checked against the sidecar's digest. The last policy
that read cleanly is cached beside the alert state, so a stage whose policy later cannot be used keeps alerting by
the old one and says so, instead of going quiet or reverting to the defaults.

Nothing here raises for a bad policy: the reason comes back in `StagePolicy.unreadable` for the evaluator to turn
into the stage's `policy_unreadable` warning.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import yaml
from openlakeforge_core.alert_policy import (
    AlertPolicy,
    ResolvedPolicy,
    parse_alert_policy,
    resolve_policy,
    validate_alert_policy,
)
from openlakeforge_core.descriptors import LakehouseDescriptorError
from openlakeforge_core.project_activation import ProjectActivationError, read_active
from openlakeforge_core.project_revision import (
    ProjectRevisionError,
    revision_prefix,
    sidecar_components,
    sidecar_key,
)

from openlakeforge_platform_api.alerting.store import StoreError

log = logging.getLogger(__name__)

CACHE_SCHEMA = 1
ALERTS_ENTRY = "lakehouse_code/alerts.yml"
GOVERNANCE_ENTRY = "lakehouse_code/governance.yml"
_NO_GOVERNANCE: dict[str, Any] = {"domains": []}


class PolicyStore(Protocol):
    """What the reader needs of the ops bucket; `AlertStore` provides it."""

    def read_object(self, key: str) -> bytes | None: ...

    def read_policy_cache(self, stage: str) -> bytes | None: ...

    def write_policy_cache(self, stage: str, body: bytes) -> None: ...


class _PolicyUnusable(Exception):
    """The revision's policy cannot be used; the message is the reason."""


@dataclass(frozen=True)
class StagePolicy:
    stage: str
    policy: AlertPolicy
    resolved: ResolvedPolicy
    # The activation the policy came from; None when the stage has no activation or no policy was ever read.
    revision: str | None = None
    # Why the stage needs a `policy_unreadable` warning; None when the policy was read cleanly.
    unreadable: str | None = None


@dataclass(frozen=True)
class _Cached:
    revision: str
    alerts: dict[str, Any] | None
    governance: dict[str, Any]


EMPTY_POLICY = AlertPolicy(routes={}, rules=())


def read_stage_policy(
    stage: str, store: PolicyStore, now: datetime, *, receivers: Collection[str] | None = None
) -> StagePolicy:
    """The policy to evaluate `stage` by.

    `receivers` are the profile's receiver names: a route that names another receiver is reported, because the
    profile can change after the revision was activated. The policy is still used and cached.
    """
    cached = _read_cache(stage, store)
    try:
        active = read_active(_Reader(store), stage=stage)
        if active is None:
            return _empty(stage)
        if cached is not None and cached.revision == active.activation_revision:
            return _finish(stage, cached, receivers, None)
        fresh = _read_revision(store, active.activation_revision, active.project_revision)
    except (StoreError, ProjectActivationError, ProjectRevisionError, _PolicyUnusable) as error:
        return _fallback(stage, cached, receivers, str(error))
    _write_cache(stage, store, fresh, now)
    return _finish(stage, fresh, receivers, None)


class _Reader:
    """Adapts the store to the core readers' one-method `ObjectReader`."""

    def __init__(self, store: PolicyStore) -> None:
        self._store = store

    def read(self, key: str) -> bytes | None:
        return self._store.read_object(key)


def _read_revision(store: PolicyStore, activation_revision: str, project_revision: str) -> _Cached:
    key = sidecar_key(project_revision)
    sidecar = store.read_object(key)
    if sidecar is None:
        raise _PolicyUnusable(f"revision sidecar {key} is missing")
    components = sidecar_components(sidecar.decode("utf-8", errors="replace"), project_revision)
    descriptors = next((c for c in components if c.name == "descriptors"), None)
    if descriptors is None or ALERTS_ENTRY not in descriptors.entries:
        # Both are normal: a project that never declared a policy gets no product alerts.
        return _Cached(activation_revision, None, _NO_GOVERNANCE)
    if GOVERNANCE_ENTRY not in descriptors.entries:
        raise _PolicyUnusable("the revision has alerts.yml but no governance.yml")
    prefix = f"{revision_prefix(project_revision)}/descriptors"
    alerts = _checked(store, prefix, ALERTS_ENTRY, descriptors.entries[ALERTS_ENTRY])
    governance = _checked(store, prefix, GOVERNANCE_ENTRY, descriptors.entries[GOVERNANCE_ENTRY])
    try:
        alerts_doc = yaml.safe_load(alerts)
        governance_doc = yaml.safe_load(governance)
        if not isinstance(alerts_doc, dict) or not isinstance(governance_doc, dict):
            raise LakehouseDescriptorError("alerts.yml and governance.yml must each hold a YAML object")
        policy = parse_alert_policy(alerts_doc)
        validate_alert_policy(policy, governance_doc)
        products = _products(governance_doc)
    except (yaml.YAMLError, LakehouseDescriptorError, KeyError, TypeError, UnicodeDecodeError) as error:
        raise _PolicyUnusable(f"alerts.yml is not valid: {error}") from error
    return _Cached(activation_revision, alerts_doc, products)


def _checked(store: PolicyStore, prefix: str, entry: str, expected: str) -> str:
    key = f"{prefix}/{entry}"
    content = store.read_object(key)
    if content is None:
        raise _PolicyUnusable(f"{entry} is missing from the revision")
    if hashlib.sha256(content).hexdigest() != expected:
        raise _PolicyUnusable(f"{entry} does not match the digest in the revision sidecar")
    return content.decode("utf-8")


def _products(governance: dict[str, Any]) -> dict[str, Any]:
    """Only the ids the resolver and the `match` checks read: the cache holds no business metadata."""
    return {
        "domains": [
            {"name": domain["name"], "products": [{"id": product["id"]} for product in domain.get("products", [])]}
            for domain in governance["domains"]
        ]
    }


def _finish(stage: str, cached: _Cached, receivers: Collection[str] | None, unreadable: str | None) -> StagePolicy:
    if cached.alerts is None:
        return StagePolicy(
            stage, EMPTY_POLICY, resolve_policy(EMPTY_POLICY, _NO_GOVERNANCE, stage), cached.revision, unreadable
        )
    policy = parse_alert_policy(cached.alerts)
    resolved = resolve_policy(policy, cached.governance, stage)
    if unreadable is None and receivers is not None:
        missing = sorted({receiver for receiver in policy.routes.values() if receiver not in receivers})
        if missing:
            unreadable = f"routes name receivers the profile does not declare: {', '.join(missing)}"
    return StagePolicy(stage, policy, resolved, cached.revision, unreadable)


def _fallback(stage: str, cached: _Cached | None, receivers: Collection[str] | None, reason: str) -> StagePolicy:
    log.warning("alert policy of stage %s cannot be used: %s", stage, reason)
    if cached is None:
        return StagePolicy(stage, EMPTY_POLICY, resolve_policy(EMPTY_POLICY, _NO_GOVERNANCE, stage), None, reason)
    return _finish(stage, cached, receivers, reason)


def _empty(stage: str) -> StagePolicy:
    return StagePolicy(stage, EMPTY_POLICY, resolve_policy(EMPTY_POLICY, _NO_GOVERNANCE, stage))


def _read_cache(stage: str, store: PolicyStore) -> _Cached | None:
    try:
        body = store.read_policy_cache(stage)
        if body is None:
            return None
        document = json.loads(body)
        if document["schema_version"] != CACHE_SCHEMA:
            raise ValueError(f"schema_version {document['schema_version']!r}")
        cached = _Cached(str(document["revision"]), document["alerts"], dict(document["governance"]))
        if cached.alerts is not None:
            validate_alert_policy(parse_alert_policy(cached.alerts), cached.governance)
    except (StoreError, ValueError, KeyError, TypeError, LakehouseDescriptorError) as error:
        # A cache that cannot be used is no worse than none: the next clean read rewrites it.
        log.warning("cached alert policy of stage %s is unusable: %s", stage, error)
        return None
    return cached


def _write_cache(stage: str, store: PolicyStore, cached: _Cached, now: datetime) -> None:
    document = {
        "schema_version": CACHE_SCHEMA,
        "stage": stage,
        "revision": cached.revision,
        "read_at": now.isoformat(),
        "alerts": cached.alerts,
        "governance": cached.governance,
    }
    try:
        store.write_policy_cache(stage, json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
    except StoreError as error:
        # The policy just read is good and is used now; only the fallback for a later failure is lost.
        log.warning("alert policy cache of stage %s was not written: %s", stage, error)
