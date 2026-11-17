"""Who a notification goes to (ADR 0023, decisions 4 and 11).

A product condition goes to the routes of the rule that wins for it, mapped to receivers by the policy's `routes`. A
platform condition goes to the receivers the profile marks for platform conditions. Either way a receiver's `stages`
is a ceiling the ops team owns. Routing is recomputed whenever a notification is sent, so a policy or profile change
applies to what is still queued.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from openlakeforge_core.alerts import PLATFORM_KINDS, POLICY_CONDITION, Notification

from openlakeforge_platform_api.alerting.policy import StagePolicy
from openlakeforge_platform_api.alerting.settings import EvaluatorSettings

log = logging.getLogger(__name__)


def routed_receivers(
    notification: Notification, settings: EvaluatorSettings, policies: Mapping[str, StagePolicy]
) -> set[str] | None:
    """Names of the receivers `notification` goes to now; empty when nobody is routed.

    None when a product notification cannot be routed because its stage has no policy that could be read, not even a
    cached one: that says nothing about whether anyone is routed, so the caller must leave the notification queued.
    """
    stage = notification.scope.stage
    if notification.kind in PLATFORM_KINDS:
        wanted = {receiver.name for receiver in settings.receivers if receiver.platform}
    else:
        found = policies.get(stage)
        if found is not None and found.revision is None and found.unreadable:
            return None
        wanted = _product_receivers(notification, settings, found)
    return {receiver.name for receiver in settings.receivers if receiver.name in wanted and receiver.routes(stage)}


def _product_receivers(notification: Notification, settings: EvaluatorSettings, found: StagePolicy | None) -> set[str]:
    if found is None:
        return set()
    scope = notification.scope
    conditions = found.resolved.for_job(scope.product) if scope.product else found.resolved.unmatched
    winner = conditions.get(POLICY_CONDITION[notification.kind])
    if winner is None or not winner.enabled:
        return set()
    declared = {receiver.name for receiver in settings.receivers}
    receivers: set[str] = set()
    for route in winner.routes:
        receiver = found.policy.routes.get(route)
        if receiver in declared:
            receivers.add(str(receiver))
        else:
            # The profile can lose a receiver after activation; the rest of the routes still deliver, and the stage
            # raises `policy_unreadable` for it (policy.py).
            log.warning(
                "route %s of stage %s names receiver %s, which the profile does not declare",
                route,
                scope.stage,
                receiver,
            )
    return receivers
