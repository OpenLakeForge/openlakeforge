"""The metrics read path (ADR 0024, decision 4).

A backend the profile turns off is `disabled`, a provider without one `not_provided`, and a backend that does not
answer `unavailable`: never healthy, and never a zero. Metrics never feed `HealthState`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime

from fastapi import Request
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.prometheus import PrometheusClient

from openlakeforge_platform_api.models import IntegrationStatus
from openlakeforge_platform_api.settings import Settings

log = logging.getLogger(__name__)

# Every Prometheus read the API makes is bounded by this, as the service probes are by PROBE_TIMEOUT: the whole read,
# any wait included, so a slow or rate-limiting backend reads `unavailable` and never holds a worker or the stage
# health answer. The client's own timeout bounds each socket read, not the call, so the deadline is held here.
READ_DEADLINE_SECONDS = 5.0
# A read abandoned at the deadline keeps its thread until the client's socket timeout ends it; a small pool caps how
# many, and reads queued behind them are dropped once their caller has stopped waiting.
_CLIENT_TIMEOUT_SECONDS = READ_DEADLINE_SECONDS
_READS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="metrics-read")


class ReadDeadlineError(ServiceClientError):
    """The metrics backend did not answer within READ_DEADLINE_SECONDS."""


def _backend(settings: Settings) -> PrometheusClient | IntegrationStatus:
    """The client for the configured backend, or why none is asked at all.

    One attempt per read: a 429 fails at once rather than waiting out a Retry-After the deadline cannot afford.
    """
    if not settings.metrics_enabled:
        return IntegrationStatus(name="metrics", status="disabled", reason="the deployment profile turns metrics off")
    if settings.metrics_url is None:
        return IntegrationStatus(name="metrics", status="not_provided", reason="the provider offers no metrics backend")
    return PrometheusClient(settings.metrics_url, timeout=_CLIENT_TIMEOUT_SECONDS, rate_limit_retries=0)


def bounded[T](read: Callable[[], T]) -> T:
    """`read()` within READ_DEADLINE_SECONDS. Raises ServiceClientError, or ReadDeadlineError past the deadline."""
    expires = time.monotonic() + READ_DEADLINE_SECONDS

    def run() -> T:
        # cancel() below cannot stop a read a worker picked up in the same instant; this check can.
        if time.monotonic() >= expires:
            raise ReadDeadlineError("the read was dropped: its caller had stopped waiting")
        return read()

    answer = _READS.submit(run)
    try:
        return answer.result(timeout=READ_DEADLINE_SECONDS)
    except FutureTimeout as exc:
        # A read still queued behind stalled ones is dropped, not run after its caller has given up on it.
        answer.cancel()
        raise ReadDeadlineError(f"the metrics backend did not answer within {READ_DEADLINE_SECONDS:g} s") from exc


def unavailable_reason(exc: ServiceClientError) -> str:
    """The reason a caller is given: the deadline, or a generic one; the backend's own message goes to the log."""
    return str(exc) if isinstance(exc, ReadDeadlineError) else "the metrics backend did not answer"


def metrics_integration(request: Request) -> IntegrationStatus:
    """The health routes' `metrics` integration: whether the backend answers a query now, within the deadline."""
    backend = _backend(request.app.state.settings)
    if isinstance(backend, IntegrationStatus):
        return backend
    try:
        bounded(lambda: backend.query("vector(1)", time=datetime.now(UTC).timestamp()))
    except ServiceClientError as exc:
        log.warning("the metrics backend could not be read: %s", exc)
        return IntegrationStatus(name="metrics", status="unavailable", reason=unavailable_reason(exc))
    return IntegrationStatus(name="metrics", status="available", reason="the metrics backend answered a query")
