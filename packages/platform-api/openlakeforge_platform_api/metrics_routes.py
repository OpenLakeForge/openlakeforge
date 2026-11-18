"""The metrics read path (ADR 0024, decision 4).

A backend the profile turns off is `disabled`, a provider without one `not_provided`, and a backend that does not
answer `unavailable`: never healthy, and never a zero. Metrics never feed `HealthState`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime

from fastapi import Request
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.prometheus import PrometheusClient

from openlakeforge_platform_api.models import IntegrationStatus
from openlakeforge_platform_api.settings import Settings

log = logging.getLogger(__name__)

# The health routes' bound on the metrics probe, as on the service probes (PROBE_TIMEOUT): the whole call, any wait
# included. The client's own timeout bounds each socket read, not the call, so the deadline is held here.
PROBE_DEADLINE_SECONDS = 5.0
# A probe abandoned at the deadline keeps its thread until the client's timeout ends it; a small pool caps how many.
_PROBES = ThreadPoolExecutor(max_workers=4, thread_name_prefix="metrics-probe")


def _backend(settings: Settings) -> PrometheusClient | IntegrationStatus:
    """The client for the configured backend, or why none is asked at all."""
    if not settings.metrics_enabled:
        return IntegrationStatus(name="metrics", status="disabled", reason="the deployment profile turns metrics off")
    if settings.metrics_url is None:
        return IntegrationStatus(name="metrics", status="not_provided", reason="the provider offers no metrics backend")
    return PrometheusClient(settings.metrics_url)


def metrics_integration(request: Request) -> IntegrationStatus:
    """The health routes' `metrics` integration: whether the backend answers a query now.

    One attempt, no retry after a 429, and never longer than PROBE_DEADLINE_SECONDS: the stage health route awaits
    it, so a slow or rate-limiting backend must read `unavailable`, not hold up the health answer.
    """
    settings: Settings = request.app.state.settings
    backend = _backend(settings)
    if isinstance(backend, IntegrationStatus):
        return backend
    probe = PrometheusClient(str(settings.metrics_url), timeout=PROBE_DEADLINE_SECONDS, rate_limit_retries=0)
    answer = _PROBES.submit(probe.query, "vector(1)", time=datetime.now(UTC).timestamp())
    try:
        answer.result(timeout=PROBE_DEADLINE_SECONDS)
    except FutureTimeout:
        log.warning("the metrics backend did not answer within %s s", PROBE_DEADLINE_SECONDS)
        return IntegrationStatus(
            name="metrics",
            status="unavailable",
            reason=f"the metrics backend did not answer within {PROBE_DEADLINE_SECONDS:g} s",
        )
    except ServiceClientError as exc:
        log.warning("the metrics backend could not be read: %s", exc)
        return IntegrationStatus(name="metrics", status="unavailable", reason="the metrics backend did not answer")
    return IntegrationStatus(name="metrics", status="available", reason="the metrics backend answered a query")
