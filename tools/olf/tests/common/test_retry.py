from __future__ import annotations

import pytest

from olf.common.deployment.errors import CommandExecutionError
from olf.common.tooling.retry import RetryPolicy, run_with_retry


def _error(returncode: int = 1) -> CommandExecutionError:
    return CommandExecutionError(("cmd",), returncode, stdout="", stderr="")


def test_succeeds_first_attempt_runs_once() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    sleeps: list[float] = []
    result = run_with_retry(fn, policy=RetryPolicy(max_attempts=4, delay_seconds=1), sleep_fn=sleeps.append)

    assert result == "ok"
    assert calls == 1
    assert sleeps == []


def test_fails_once_then_succeeds_runs_twice() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _error()
        return "ok"

    sleeps: list[float] = []
    result = run_with_retry(fn, policy=RetryPolicy(max_attempts=4, delay_seconds=5), sleep_fn=sleeps.append)

    assert result == "ok"
    assert calls == 2
    assert sleeps == [5]


def test_reaches_max_attempts_raises_final_error() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise _error(returncode=calls)

    sleeps: list[float] = []
    with pytest.raises(CommandExecutionError) as excinfo:
        run_with_retry(fn, policy=RetryPolicy(max_attempts=3, delay_seconds=1), sleep_fn=sleeps.append)

    assert calls == 3
    assert excinfo.value.returncode == 3
    # No sleep after the final (non-retried) failure.
    assert sleeps == [1, 1]


def test_retry_predicate_false_stops_immediately() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise _error()

    sleeps: list[float] = []
    with pytest.raises(CommandExecutionError):
        run_with_retry(
            fn,
            policy=RetryPolicy(max_attempts=4, delay_seconds=1),
            retry_if=lambda exc, attempt: False,
            sleep_fn=sleeps.append,
        )

    assert calls == 1
    assert sleeps == []


def test_retry_predicate_receives_error_and_attempt_number() -> None:
    seen: list[tuple[int, int]] = []
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise _error(returncode=calls)

    def predicate(exc: CommandExecutionError, attempt: int) -> bool:
        seen.append((exc.returncode, attempt))
        return True

    with pytest.raises(CommandExecutionError):
        run_with_retry(
            fn,
            policy=RetryPolicy(max_attempts=3, delay_seconds=0),
            retry_if=predicate,
            sleep_fn=lambda _seconds: None,
        )

    assert seen == [(1, 1), (2, 2), (3, 3)]


def test_backoff_multiplier_scales_delay_per_attempt() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise _error()

    sleeps: list[float] = []
    with pytest.raises(CommandExecutionError):
        run_with_retry(
            fn,
            policy=RetryPolicy(max_attempts=4, delay_seconds=2, backoff_multiplier=2.0),
            sleep_fn=sleeps.append,
        )

    assert sleeps == [2, 4, 8]


def test_unit_tests_never_actually_sleep() -> None:
    """Sanity check that sleep injection is honored end to end."""
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _error()
        return "ok"

    result = run_with_retry(
        fn,
        policy=RetryPolicy(max_attempts=5, delay_seconds=999),
        sleep_fn=lambda seconds: None,
    )
    assert result == "ok"


def test_retry_on_custom_exception_logs_and_matches_legacy_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the behaviour of the removed olf.common.retry (retry_on + log messages)."""
    from olf.common import log

    warns: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(log, "warn", warns.append)
    monkeypatch.setattr(log, "error", errors.append)
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise ValueError(f"boom{calls}")

    sleeps: list[float] = []
    with pytest.raises(ValueError, match="boom3"):
        run_with_retry(
            fn,
            policy=RetryPolicy(max_attempts=3, delay_seconds=3.0),
            retry_on=(ValueError,),
            description="thing",
            sleep_fn=sleeps.append,
        )

    assert calls == 3
    assert sleeps == [3.0, 3.0]
    assert warns == [
        "thing failed on attempt 1/3 (boom1); retrying in 3s...",
        "thing failed on attempt 2/3 (boom2); retrying in 3s...",
    ]
    assert errors == ["thing failed after 3 attempt(s)."]


def test_exceptions_outside_retry_on_propagate_immediately() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise KeyError("x")

    with pytest.raises(KeyError):
        run_with_retry(fn, policy=RetryPolicy(max_attempts=4), retry_on=(ValueError,), sleep_fn=lambda _s: None)

    assert calls == 1
