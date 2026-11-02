"""Bounded, explicit retry primitive for deployment tooling calls.

This is deliberately separate from `olf.common.tooling.process.ProcessRunner`:
retrying is a policy a caller opts into per-call, never automatic behavior
baked into process execution.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from olf.common import log
from olf.common.deployment.errors import CommandExecutionError

# Receives the failing error and the 1-indexed attempt number that just
# failed; returns whether another attempt should be made.
RetryPredicate = Callable[[Any, int], bool]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    delay_seconds: float = 20.0
    backoff_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")

    def delay_for_attempt(self, attempt: int) -> float:
        """Delay to sleep after the given (1-indexed) attempt fails."""
        return self.delay_seconds * (self.backoff_multiplier ** (attempt - 1))


def run_with_retry[T](
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    retry_if: RetryPredicate | None = None,
    retry_on: tuple[type[Exception], ...] = (CommandExecutionError,),
    description: str | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> T:
    """Run `fn`, retrying on `retry_on` (default `CommandExecutionError`) per `policy`.

    Attempt 1 always runs immediately. No sleep happens after the final
    attempt. `retry_if`, when given, can veto a retry (e.g. only retry on
    specific return codes/messages); by default every `retry_on` error
    is retryable up to `policy.max_attempts`. `description`, when given, logs a
    warning per retry and an error when giving up.
    """
    attempt = 1
    while True:
        try:
            return fn()
        except retry_on as exc:
            should_retry = retry_if(exc, attempt) if retry_if is not None else True
            if not should_retry or attempt >= policy.max_attempts:
                if description:
                    log.error(f"{description} failed after {attempt} attempt(s).")
                raise
            delay = policy.delay_for_attempt(attempt)
            if description:
                log.warn(
                    f"{description} failed on attempt {attempt}/{policy.max_attempts} ({exc}); "
                    f"retrying in {delay:g}s..."
                )
            (sleep_fn or time.sleep)(delay)
            attempt += 1
