"""Bounded, explicit retry primitive for deployment tooling calls.

This is deliberately separate from `olf.tooling.process.ProcessRunner`:
retrying is a policy a caller opts into per-call, never automatic behavior
baked into process execution.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from olf.deployment.errors import CommandExecutionError

# Receives the failing error and the 1-indexed attempt number that just
# failed; returns whether another attempt should be made.
RetryPredicate = Callable[[CommandExecutionError, int], bool]


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
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Run `fn`, retrying on `CommandExecutionError` per `policy`.

    Attempt 1 always runs immediately. No sleep happens after the final
    attempt. `retry_if`, when given, can veto a retry (e.g. only retry on
    specific return codes/messages); by default every `CommandExecutionError`
    is retryable up to `policy.max_attempts`.
    """
    attempt = 1
    while True:
        try:
            return fn()
        except CommandExecutionError as exc:
            should_retry = retry_if(exc, attempt) if retry_if is not None else True
            if not should_retry or attempt >= policy.max_attempts:
                raise
            sleep_fn(policy.delay_for_attempt(attempt))
            attempt += 1
