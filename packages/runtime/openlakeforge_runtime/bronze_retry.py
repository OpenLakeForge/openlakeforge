"""Retry of a Bronze extract/load on a transient network error, inside the Dagster op.

Only Bronze retries, and only here. A Dagster `RetryPolicy` on the job would also restart a step that a
terminated run interrupted: Dagster turns the SIGTERM into a retry request for any op that has a policy, and the
in-process executor sleeps through the delay and runs the step again, so the run never stops (and a restarted
Silver step starts a second Floe Job). Silver's failures include a deterministic `abort` or contract failure that
must stop the run, and Gold leaves a half-built generation behind, so neither retries.

The allowlist names what dlt 1.29.0 and botocore 1.43 raise for a dropped or timed-out connection to the source or
the object store. dlt wraps what its steps raise in `PipelineStepFailed` (the original is its `exception`), so the
chain of causes is searched, not just the exception itself. dlt's own `DestinationTransientException` and
`DatabaseTransientException` are left out on purpose: dlt already retries its load jobs, they are BaseExceptions, and
they also wrap non-network database errors.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from importlib import import_module

__all__: list[str] = []

DEFAULT_BRONZE_RETRY_ATTEMPTS = 3
# Retry n (from 0) waits 15 s * 2**n, at most 2 minutes: 15 s, 30 s, 60 s for the default three retries. No
# jitter: the wait is a sleep in the run pod, which keeps its queue slot meanwhile.
BACKOFF_BASE_SECONDS = 15.0
BACKOFF_CAP_SECONDS = 120.0

_TRANSIENT_NAMES = (
    "builtins.ConnectionError",  # reset, refused, aborted, broken pipe
    "builtins.TimeoutError",
    "requests.exceptions.ConnectionError",  # source clients; dlt's own default retry set
    "requests.exceptions.Timeout",
    "requests.exceptions.ChunkedEncodingError",
    "botocore.exceptions.ConnectionError",  # EndpointConnectionError, ConnectTimeoutError, ProxyConnectionError
    "botocore.exceptions.ReadTimeoutError",
    "botocore.exceptions.ConnectionClosedError",
    "botocore.exceptions.ResponseStreamingError",
)
# Dagster's `DagsterExecutionInterruptedError` is a BaseException like these; it is matched by name so this
# module needs no dagster import.
_NEVER_RETRY_NAMES = ("DagsterExecutionInterruptedError",)


def _transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = []
    for name in _TRANSIENT_NAMES:
        module, _, attr = name.rpartition(".")
        try:
            types.append(getattr(import_module(module), attr))
        except (ImportError, AttributeError):
            continue
    return tuple(types)


def _is_interrupt(exc: BaseException) -> bool:
    return isinstance(exc, KeyboardInterrupt | SystemExit) or any(
        cls.__name__ in _NEVER_RETRY_NAMES for cls in type(exc).__mro__
    )


def _causes(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: BaseException | None = exc
    while pending is not None and id(pending) not in seen:
        seen.add(id(pending))
        chain.append(pending)
        wrapped = getattr(pending, "exception", None)  # dlt's PipelineStepFailed
        pending = wrapped if isinstance(wrapped, BaseException) else pending.__cause__
    return chain


def is_transient(exc: BaseException) -> bool:
    chain = _causes(exc)
    if any(_is_interrupt(link) for link in chain):
        return False
    return any(isinstance(link, _transient_types()) for link in chain)


def backoff_seconds(retry: int) -> float:
    return min(BACKOFF_BASE_SECONDS * 2**retry, BACKOFF_CAP_SECONDS)


def call_with_transient_retry[T](
    call: Callable[[], T],
    *,
    retries: int,
    sleep: Callable[[float], None] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run `call`, retrying up to `retries` more times after a transient network error.

    Anything else, an interrupt above all, propagates at once without a sleep. `retries=0` runs the call once.
    """
    wait = sleep or time.sleep
    retry = 0
    while True:
        try:
            return call()
        except BaseException as exc:
            if retry >= retries or not is_transient(exc):
                raise
            delay = backoff_seconds(retry)
            if on_retry is not None:
                on_retry(retry + 1, exc, delay)
            wait(delay)
            retry += 1
