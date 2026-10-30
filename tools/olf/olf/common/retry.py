"""Retry helper mirroring scripts/lib/common.sh run_with_retry."""

from __future__ import annotations

import time
from collections.abc import Callable

from olf import log


def run_with_retry[T](
    description: str,
    fn: Callable[[], T],
    *,
    max_attempts: int = 4,
    delay_seconds: float = 20.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    attempt = 1
    while True:
        try:
            return fn()
        except retry_on as exc:
            if attempt >= max_attempts:
                log.error(f"{description} failed after {attempt} attempt(s).")
                raise
            log.warn(
                f"{description} failed on attempt {attempt}/{max_attempts} ({exc}); "
                f"retrying in {delay_seconds:g}s..."
            )
            time.sleep(delay_seconds)
            attempt += 1
