from __future__ import annotations

import pytest

from olf.e2e._shell import E2EError
from olf.e2e._trino import retry_until_deadline


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_retry_succeeds_after_transient_failures() -> None:
    clock = _Clock()
    calls = 0

    def action() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise E2EError("empty response")

    retry_until_deadline(action, timeout=60, interval=5, sleep=clock.sleep, clock=lambda: clock.now)

    assert calls == 3


def test_retry_fails_with_last_error_after_deadline() -> None:
    clock = _Clock()
    calls = 0

    def action() -> None:
        nonlocal calls
        calls += 1
        raise E2EError(f"attempt {calls}")

    with pytest.raises(E2EError) as excinfo:
        retry_until_deadline(action, timeout=20, interval=5, sleep=clock.sleep, clock=lambda: clock.now)

    assert str(excinfo.value) == f"attempt {calls}"
    assert clock.now >= 20
