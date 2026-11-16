"""Outbound notifications: a provider-neutral `Notifier` and a generic HTTPS webhook implementation.

The webhook URL is the credential (no HMAC, no header token), so nothing here logs it, puts it in an exception message
or exposes it through `repr`. Like every client in this package the notifier is handed its URL and an optional CA file
and never reads the environment (ADR 0021, decision 4).
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

DeliveryStatus = Literal["delivered", "retryable", "rejected"]

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 4.0
# A receiver can ask for a long wait; the evaluator runs every few minutes and retries on the next run, so a longer
# request is cut to this rather than blocking the run.
RETRY_AFTER_MAX_SECONDS = 10.0
# Upper bound on one `send`: every attempt timing out at connect plus read, and every wait at the larger of the
# backoff and Retry-After caps.
MAX_TOTAL_SECONDS = MAX_ATTEMPTS * (CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS) + (MAX_ATTEMPTS - 1) * max(
    BACKOFF_MAX_SECONDS, RETRY_AFTER_MAX_SECONDS
)

_RETRYABLE_STATUSES = frozenset({408, 429})


@dataclass(frozen=True)
class DeliveryResult:
    """`retryable` means attempts ran out on a transient failure; `rejected` means the receiver refused the request."""

    status: DeliveryStatus
    http_status: int | None
    attempts: int


class Notifier(Protocol):
    def send(self, payload: Mapping[str, Any], *, delivery_id: str) -> DeliveryResult: ...


class _DropConnectionLogs(logging.Filter):
    """urllib3 logs the request line at DEBUG, which is the webhook path and so the credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


_CONNECTION_LOGGER = "urllib3.connectionpool"


def quiet_transport_logs() -> None:
    """Drop urllib3's connection-pool records, which carry the webhook path (the credential) at DEBUG.

    A process that sends webhooks calls this once at startup. It is not done on import: importing this module must not
    change logging for every other HTTP client in the process.
    """
    pool = logging.getLogger(_CONNECTION_LOGGER)
    if not any(isinstance(existing, _DropConnectionLogs) for existing in pool.filters):
        pool.addFilter(_DropConnectionLogs())


def _random_jitter(ceiling: float) -> float:
    return random.uniform(0.0, ceiling)


@dataclass(frozen=True)
class WebhookNotifier:
    name: str
    url: str = field(repr=False)
    ca_file: str | None = None
    # Seams for tests; production uses the real clock and RNG.
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False, compare=False)
    jitter: Callable[[float], float] = field(default=_random_jitter, repr=False, compare=False)

    def __post_init__(self) -> None:
        # urlsplit lowercases the scheme. The message never names the URL.
        if urlsplit(self.url).scheme != "https":
            raise ValueError(f"webhook receiver {self.name!r}: the URL must use https")

    def __repr__(self) -> str:
        return f"WebhookNotifier(name={self.name!r})"

    def send(self, payload: Mapping[str, Any], *, delivery_id: str) -> DeliveryResult:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": delivery_id,
            "X-OpenLakeForge-Delivery": delivery_id,
        }
        result = DeliveryResult("retryable", None, 0)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after = 0.0
            try:
                response = requests.post(
                    self.url,
                    data=body,
                    headers=headers,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    allow_redirects=False,
                    verify=self.ca_file or True,
                    # Only the status and headers matter: never read the body, which a receiver could make
                    # arbitrarily large or slow and so break the bound on one `send`.
                    stream=True,
                )
            except requests.RequestException:
                # The exception text carries the URL, so it is neither logged nor chained.
                logger.warning("webhook %s: attempt %d failed to connect", self.name, attempt)
                result = DeliveryResult("retryable", None, attempt)
            else:
                status = response.status_code
                retry_after = _retry_after(response)
                response.close()
                if 200 <= status < 300:
                    return DeliveryResult("delivered", status, attempt)
                if status in _RETRYABLE_STATUSES or status >= 500:
                    logger.warning("webhook %s: attempt %d got HTTP %d", self.name, attempt, status)
                    result = DeliveryResult("retryable", status, attempt)
                else:
                    logger.warning("webhook %s: rejected with HTTP %d", self.name, status)
                    return DeliveryResult("rejected", status, attempt)
            if attempt < MAX_ATTEMPTS:
                self.sleep(self._wait(attempt, retry_after))
        return result

    def _wait(self, attempt: int, retry_after: float) -> float:
        ceiling = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)
        return max(ceiling / 2 + self.jitter(ceiling / 2), retry_after)


def _retry_after(response: requests.Response, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> float:
    """`Retry-After` as delay-seconds or an HTTP-date (RFC 9110), as a non-negative delay capped at the maximum."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 0.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now()).total_seconds()
    return min(max(seconds, 0.0), RETRY_AFTER_MAX_SECONDS)
