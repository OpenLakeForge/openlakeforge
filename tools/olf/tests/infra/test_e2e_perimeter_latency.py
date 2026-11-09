from __future__ import annotations

import pytest

from olf.infra.e2e import _perimeter_latency as lat
from olf.infra.e2e._shell import E2EError


def test_summarize_reports_median_and_p95_in_ms() -> None:
    median, p95 = lat.summarize([i / 1000 for i in range(1, 21)])
    assert median == pytest.approx(10.5)
    assert 19 < p95 <= 20


def test_only_a_large_added_latency_fails() -> None:
    assert lat.require_acceptable((50, 80), (60, 120)) == (10, 40)
    with pytest.raises(E2EError, match="adds"):
        lat.require_acceptable((50, 80), (160, 120))
    # A spiky p95 alone is reported, not failed: on a shared runner it is one slow sample.
    assert lat.require_acceptable((50, 80), (60, 400)) == (10, 320)


class _Reply:
    def __init__(self, status: int = 200, retry_after: str | None = None) -> None:
        self.status_code = status
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}
        self.url = "http://superset/api/v1/chart/data"
        self.text = "too many"

    def raise_for_status(self) -> None:
        assert self.status_code < 400

    def json(self) -> dict[str, str]:
        return {"result": "csrf"}


class _Time:
    """A clock the fake requests and the pacer's sleeps advance."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _pacer(time: _Time, **kwargs: float) -> lat._Pacer:
    return lat._Pacer(sleep=time.sleep, clock=time.clock, **kwargs)


def test_requests_are_spaced_and_only_the_request_is_timed() -> None:
    time = _Time()
    pacer = _pacer(time, requests_per_second=10)

    def request() -> _Reply:
        time.now += 0.02
        return _Reply()

    first = pacer.send(request)[1]
    second = pacer.send(request)[1]

    assert (first, second) == (pytest.approx(0.02), pytest.approx(0.02))
    # 0.1 s between starts, of which the request took 0.02: the wait is 0.08, and the first request waits for nothing.
    assert time.sleeps == [pytest.approx(0.08)]


def test_a_rate_limited_request_is_retried_after_its_retry_after_and_the_wait_is_not_measured() -> None:
    time = _Time()
    pacer = _pacer(time, requests_per_second=1000)
    replies = iter([_Reply(429, "1"), _Reply(429, "1"), _Reply(200)])

    def request() -> _Reply:
        time.now += 0.01
        return next(replies)

    reply, elapsed = pacer.send(request)

    assert reply.status_code == 200
    assert elapsed == pytest.approx(0.01)
    assert [s for s in time.sleeps if s >= 1] == [1.0, 1.0]


def test_a_rate_limit_that_persists_fails_the_drill() -> None:
    time = _Time()
    pacer = _pacer(time, retries=2)

    with pytest.raises(E2EError, match="HTTP 429 after 2 retries"):
        pacer.send(lambda: _Reply(429, "0"))


@pytest.mark.parametrize(
    ("header", "expected"), [("3", 2.0), ("0.5", 0.5), ("Wed, 21 Oct 2026 07:28:00 GMT", 1.0), ("-4", 0.0)]
)
def test_retry_after_is_bounded_and_tolerates_a_date(header: str, expected: float) -> None:
    assert lat._retry_after(_Reply(429, header)) == expected


def test_a_load_measures_its_requests_and_surfaces_other_errors() -> None:
    time = _Time()
    calls: list[str] = []

    class Session:
        def __init__(self, statuses: list[int]) -> None:
            self.cookies = {}
            self.statuses = iter(statuses)

        def get(self, url: str, **_: object) -> _Reply:
            calls.append(url)
            time.now += 0.01
            return _Reply(next(self.statuses, 200))

        post = get

    datasets = [{"id": 1, "type": "table"}, {"id": 2, "type": "table"}]
    load = lat._dashboard_load("http://s", "1", datasets, {}, {}, Session([200]), _pacer(time))  # type: ignore[arg-type]
    assert load() == pytest.approx(0.03)  # the dashboard and two charts, not the pacing waits between them
    assert len(calls) == 3  # the dashboard, then each chart's dataset

    broken = lat._dashboard_load("http://s", "1", datasets, {}, {}, Session([200, 500]), _pacer(time))  # type: ignore[arg-type]
    with pytest.raises(E2EError, match="HTTP 500"):
        broken()
