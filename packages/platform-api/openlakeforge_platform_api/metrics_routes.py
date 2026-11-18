"""The metrics read path (ADR 0024, decision 4).

A backend the profile turns off is `disabled`, a provider without one `not_provided`, and a backend that does not
answer `unavailable`: never healthy, and never a zero. Metrics never feed `HealthState`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import Request
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.prometheus import PrometheusClient

from openlakeforge_platform_api.models import IntegrationStatus
from openlakeforge_platform_api.settings import Settings

log = logging.getLogger(__name__)


def _backend(settings: Settings) -> PrometheusClient | IntegrationStatus:
    """The client for the configured backend, or why none is asked at all."""
    if not settings.metrics_enabled:
        return IntegrationStatus(name="metrics", status="disabled", reason="the deployment profile turns metrics off")
    if settings.metrics_url is None:
        return IntegrationStatus(name="metrics", status="not_provided", reason="the provider offers no metrics backend")
    return PrometheusClient(settings.metrics_url)


def metrics_integration(request: Request) -> IntegrationStatus:
    """The health routes' `metrics` integration: whether the backend answers a query now. Blocks for up to 5 s."""
    backend = _backend(request.app.state.settings)
    if isinstance(backend, IntegrationStatus):
        return backend
    try:
        backend.query("vector(1)", time=datetime.now(UTC).timestamp())
    except ServiceClientError as exc:
        log.warning("the metrics backend could not be read: %s", exc)
        return IntegrationStatus(name="metrics", status="unavailable", reason="the metrics backend did not answer")
    return IntegrationStatus(name="metrics", status="available", reason="the metrics backend answered a query")
