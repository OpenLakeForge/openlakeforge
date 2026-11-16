from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from openlakeforge_core import (
    ArchiveThresholds,
    HealthState,
    SpoolObservation,
    evaluate_spool_age,
    evaluate_spool_size,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(seconds=60)
LIMIT = 512_000_000


def _spool(*, age: timedelta | None = None, size: int = 0, seen: timedelta = timedelta(seconds=5)) -> SpoolObservation:
    return SpoolObservation("dev", NOW - seen, age, size, LIMIT)


@pytest.mark.parametrize(
    ("age", "state"),
    [
        (None, HealthState.HEALTHY),
        (timedelta(minutes=4), HealthState.HEALTHY),
        (timedelta(minutes=10), HealthState.WARNING),
        (timedelta(minutes=29), HealthState.WARNING),
        (timedelta(minutes=30), HealthState.CRITICAL),
    ],
)
def test_the_age_of_the_oldest_file_sets_the_state(age: timedelta | None, state: HealthState) -> None:
    signal = evaluate_spool_age("dev", _spool(age=age), now=NOW, max_age=MAX_AGE)

    assert signal.state is state
    assert signal.stage == "dev"
    assert signal.component == "log-archive.spool-age"


@pytest.mark.parametrize(
    ("size", "state"),
    [
        (0, HealthState.HEALTHY),
        (LIMIT // 2 - 1, HealthState.HEALTHY),
        (LIMIT // 2, HealthState.WARNING),
        (int(LIMIT * 0.9), HealthState.CRITICAL),
        (LIMIT, HealthState.CRITICAL),
    ],
)
def test_the_fill_of_the_spool_sets_the_state(size: int, state: HealthState) -> None:
    assert evaluate_spool_size("dev", _spool(size=size), now=NOW, max_age=MAX_AGE).state is state


@pytest.mark.parametrize("evaluate", [evaluate_spool_age, evaluate_spool_size])
def test_no_observation_and_a_stale_one_are_unknown_never_healthy(evaluate) -> None:  # type: ignore[no-untyped-def]
    assert evaluate("dev", None, now=NOW, max_age=MAX_AGE).state is HealthState.UNKNOWN
    stale = evaluate("dev", _spool(seen=timedelta(seconds=61)), now=NOW, max_age=MAX_AGE)
    assert stale.state is HealthState.UNKNOWN
    assert stale.reason == "observation is stale"


def test_the_signal_carries_when_the_probe_looked() -> None:
    signal = evaluate_spool_size("dev", _spool(seen=timedelta(seconds=20)), now=NOW, max_age=MAX_AGE)

    assert signal.observed_at == NOW - timedelta(seconds=20)
    assert signal.expires_at == NOW - timedelta(seconds=20) + MAX_AGE


def test_thresholds_can_be_tightened() -> None:
    tight = ArchiveThresholds(spool_age_warning=timedelta(minutes=1), spool_age_critical=timedelta(minutes=2))

    signal = evaluate_spool_age("dev", _spool(age=timedelta(minutes=1)), now=NOW, max_age=MAX_AGE, thresholds=tight)

    assert signal.state is HealthState.WARNING


@pytest.mark.parametrize(
    "kwargs",
    [
        {"spool_age_warning": timedelta(minutes=30)},
        {"spool_size_warning": 0.95},
        {"spool_size_critical": 1.5},
        {"spool_age_warning": timedelta(0)},
    ],
)
def test_thresholds_out_of_order_are_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ArchiveThresholds(**kwargs)  # type: ignore[arg-type]


def test_an_observation_cannot_be_judged_as_another_stage() -> None:
    with pytest.raises(ValueError, match="another"):
        evaluate_spool_age("prod", _spool(), now=NOW, max_age=MAX_AGE)


def test_an_unbounded_spool_is_not_an_observation() -> None:
    with pytest.raises(ValueError, match="limit"):
        SpoolObservation("dev", NOW, None, 0, 0)


@pytest.mark.parametrize("evaluate", [evaluate_spool_age, evaluate_spool_size])
def test_a_spool_the_probe_could_not_read_is_unknown_never_healthy(evaluate) -> None:  # type: ignore[no-untyped-def]
    unreadable = SpoolObservation("dev", NOW - timedelta(seconds=5), None, 0, LIMIT, unreadable=True)

    signal = evaluate("dev", unreadable, now=NOW, max_age=MAX_AGE)

    assert signal.state is HealthState.UNKNOWN
    assert "could not read" in signal.reason
