import ssl

import botocore.exceptions
import pytest
import requests
from openlakeforge_runtime.bronze_retry import backoff_seconds, call_with_transient_retry


class DagsterExecutionInterruptedError(BaseException):
    """Stands in for Dagster's, which is matched by name and is a BaseException too."""


class _Wrapped(Exception):  # noqa: N818
    """Like dlt's PipelineStepFailed: keeps the original as `exception`."""

    def __init__(self, exception: BaseException) -> None:
        super().__init__("pipeline failed")
        self.exception = exception


class _Flaky:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = list(failures)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "loaded"


def _run(flaky: _Flaky, retries: int) -> tuple[object, list[float]]:
    sleeps: list[float] = []
    result = call_with_transient_retry(flaky, retries=retries, sleep=sleeps.append)
    return result, sleeps


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("reset"),
        TimeoutError("slow"),
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.ReadTimeout("slow"),
        botocore.exceptions.EndpointConnectionError(endpoint_url="http://s3"),
        botocore.exceptions.ConnectTimeoutError(endpoint_url="http://s3"),
        botocore.exceptions.ReadTimeoutError(endpoint_url="http://s3"),
        _Wrapped(botocore.exceptions.EndpointConnectionError(endpoint_url="http://s3")),
    ],
)
def test_a_transient_error_is_retried_until_a_later_attempt_succeeds(error: BaseException) -> None:
    flaky = _Flaky([error])

    result, sleeps = _run(flaky, retries=3)

    assert result == "loaded"
    assert flaky.calls == 2
    assert sleeps == [15.0]


@pytest.mark.parametrize("error", [ValueError("bad row"), PermissionError("denied"), _Wrapped(KeyError("x"))])
def test_any_other_error_is_raised_at_once(error: BaseException) -> None:
    flaky = _Flaky([error])
    sleeps: list[float] = []

    with pytest.raises(type(error)):
        call_with_transient_retry(flaky, retries=3, sleep=sleeps.append)

    assert flaky.calls == 1
    assert sleeps == []


_TLS_AND_PROXY = [
    ssl.SSLError("handshake"),
    ssl.SSLCertVerificationError("bad cert"),
    requests.exceptions.SSLError("bad cert"),
    requests.exceptions.ProxyError("proxy refused"),
    botocore.exceptions.SSLError(endpoint_url="https://s3", error="bad cert"),
    botocore.exceptions.ProxyConnectionError(proxy_url="http://proxy"),
]


@pytest.mark.parametrize("error", [*_TLS_AND_PROXY, *(_Wrapped(e) for e in _TLS_AND_PROXY)])
def test_a_tls_or_proxy_error_is_never_retried_though_it_is_a_connection_error(error: BaseException) -> None:
    flaky = _Flaky([error])
    sleeps: list[float] = []

    with pytest.raises(type(error)):
        call_with_transient_retry(flaky, retries=3, sleep=sleeps.append)

    assert flaky.calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "interrupt",
    [
        KeyboardInterrupt(),
        SystemExit(1),
        DagsterExecutionInterruptedError(),
        _Wrapped(DagsterExecutionInterruptedError()),
    ],
)
def test_an_interrupt_is_raised_at_once_without_sleeping_or_retrying(interrupt: BaseException) -> None:
    flaky = _Flaky([interrupt])
    sleeps: list[float] = []

    with pytest.raises(type(interrupt)):
        call_with_transient_retry(flaky, retries=3, sleep=sleeps.append)

    assert flaky.calls == 1
    assert sleeps == []


def test_an_interrupt_during_the_wait_stops_the_retries() -> None:
    flaky = _Flaky([ConnectionError("down")] * 3)

    def interrupted_sleep(_seconds: float) -> None:
        raise DagsterExecutionInterruptedError

    with pytest.raises(DagsterExecutionInterruptedError):
        call_with_transient_retry(flaky, retries=3, sleep=interrupted_sleep)

    assert flaky.calls == 1


def test_it_gives_up_after_the_attempt_count_and_raises_the_last_error() -> None:
    flaky = _Flaky([ConnectionError(f"down {n}") for n in range(10)])
    sleeps: list[float] = []

    with pytest.raises(ConnectionError, match="down 3"):
        call_with_transient_retry(flaky, retries=3, sleep=sleeps.append)

    assert flaky.calls == 4
    assert sleeps == [15.0, 30.0, 60.0]


def test_zero_retries_means_a_single_attempt() -> None:
    flaky = _Flaky([ConnectionError("down")])
    sleeps: list[float] = []

    with pytest.raises(ConnectionError):
        call_with_transient_retry(flaky, retries=0, sleep=sleeps.append)

    assert flaky.calls == 1
    assert sleeps == []


def test_the_backoff_doubles_and_is_capped() -> None:
    assert [backoff_seconds(n) for n in range(5)] == [15.0, 30.0, 60.0, 120.0, 120.0]
