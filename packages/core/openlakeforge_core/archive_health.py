"""Delivery health of the pod-log archive's S3 output (ADR 0022, decision 2).

The Fluent Bit S3 plugin buffers every chunk into its `store_dir` and reports success to the engine, so the engine's
output error counters stay at zero while the object store is down. Delivery is judged from the spool instead: how old
its oldest file is, and how full it is against `store_dir_limit_size`. Like `health`, nothing here performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .health import HealthState, Scope, Signal, service_signal

SPOOL_AGE = "log-archive.spool-age"
SPOOL_SIZE = "log-archive.spool-size"


@dataclass(frozen=True)
class ArchiveThresholds:
    """When a delivery signal turns `warning` and `critical`.

    Proposed defaults, for the maintainers to review (ADR 0022, decision 2). A healthy spool holds a file for at most
    `upload_timeout` (5 minutes) plus the upload itself, so the age thresholds are two and six times that. The size
    thresholds follow the ADR: half the limit means half the stated outage budget is spent, and at nine tenths the
    plugin is about to discard data.
    """

    spool_age_warning: timedelta = timedelta(minutes=10)
    spool_age_critical: timedelta = timedelta(minutes=30)
    spool_size_warning: float = 0.5
    spool_size_critical: float = 0.9

    def __post_init__(self) -> None:
        if not timedelta(0) < self.spool_age_warning < self.spool_age_critical:
            raise ValueError("the spool age thresholds must be positive, warning below critical")
        if not 0 < self.spool_size_warning < self.spool_size_critical <= 1:
            raise ValueError("the spool size thresholds are fractions of the limit, warning below critical")


@dataclass(frozen=True)
class SpoolObservation:
    """One collector pod's spool for one stage, as its probe measured it."""

    stage: str
    observed_at: datetime
    # None when the spool holds no file.
    oldest_file_age: timedelta | None
    size_bytes: int
    limit_bytes: int

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("health timestamps must be timezone-aware")
        if self.limit_bytes <= 0:
            raise ValueError("an unbounded spool has no fraction to judge: the limit must be positive")
        if self.size_bytes < 0 or (self.oldest_file_age is not None and self.oldest_file_age < timedelta(0)):
            raise ValueError("a spool cannot have a negative size or age")


def evaluate_spool_age(
    stage: str,
    observation: SpoolObservation | None,
    *,
    now: datetime,
    max_age: timedelta,
    thresholds: ArchiveThresholds = ArchiveThresholds(),  # noqa: B008 - frozen, so sharing the default is safe
) -> Signal:
    """An old file in the spool means uploads are failing; an empty spool is healthy."""
    if (unknown := _unusable(SPOOL_AGE, stage, observation, now, max_age)) is not None:
        return unknown
    assert observation is not None
    age = observation.oldest_file_age
    if age is None:
        state, reason = HealthState.HEALTHY, "the spool is empty"
    elif age >= thresholds.spool_age_critical:
        state, reason = HealthState.CRITICAL, f"the oldest spooled file is {_minutes(age)} old"
    elif age >= thresholds.spool_age_warning:
        state, reason = HealthState.WARNING, f"the oldest spooled file is {_minutes(age)} old"
    else:
        state, reason = HealthState.HEALTHY, f"the oldest spooled file is {_minutes(age)} old"
    return service_signal(Scope(stage=stage), SPOOL_AGE, state, reason, now=observation.observed_at, max_age=max_age)


def evaluate_spool_size(
    stage: str,
    observation: SpoolObservation | None,
    *,
    now: datetime,
    max_age: timedelta,
    thresholds: ArchiveThresholds = ArchiveThresholds(),  # noqa: B008 - frozen, so sharing the default is safe
) -> Signal:
    """The spool's fill against its limit: at the limit the plugin is already discarding data."""
    if (unknown := _unusable(SPOOL_SIZE, stage, observation, now, max_age)) is not None:
        return unknown
    assert observation is not None
    fraction = observation.size_bytes / observation.limit_bytes
    reason = f"the spool holds {fraction:.0%} of its limit"
    if fraction >= thresholds.spool_size_critical:
        state = HealthState.CRITICAL
    elif fraction >= thresholds.spool_size_warning:
        state = HealthState.WARNING
    else:
        state = HealthState.HEALTHY
    return service_signal(Scope(stage=stage), SPOOL_SIZE, state, reason, now=observation.observed_at, max_age=max_age)


def _unusable(
    component: str, stage: str, observation: SpoolObservation | None, now: datetime, max_age: timedelta
) -> Signal | None:
    """`unknown` for a missing or stale observation, so a collector nobody could ask never reads healthy."""
    if now.tzinfo is None:
        raise ValueError("health timestamps must be timezone-aware")
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    if observation is None:
        return service_signal(
            Scope(stage=stage), component, HealthState.UNKNOWN, "no observation", now=now, max_age=max_age
        )
    if observation.stage != stage:
        raise ValueError("an observation of one stage cannot be judged as another")
    if now >= observation.observed_at + max_age:
        return service_signal(
            Scope(stage=stage),
            component,
            HealthState.UNKNOWN,
            "observation is stale",
            now=observation.observed_at,
            max_age=max_age,
        )
    return None


def _minutes(age: timedelta) -> str:
    return f"{age.total_seconds() / 60:.0f} min"
