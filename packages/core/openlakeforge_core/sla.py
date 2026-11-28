"""The ``sla`` block of a product in ``governance.yml``: what the owner promises, validated offline.

Declaring an SLA never implies it is met. Nothing here observes a run; the values are the promise that OpenMetadata
publishes and that the compliance evaluator measures against.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .descriptors import LakehouseDescriptorError

__all__ = ["SLA_TIERS", "ProductSla", "duration_minutes", "parse_duration", "parse_sla", "validate_cron"]

# `DataProduct.sla.tier` in OpenMetadata 1.13.6; any other casing is refused by the server with HTTP 500.
SLA_TIERS = ("GOLD", "SILVER", "BRONZE", "CUSTOM")

# Calendar units are left out: a month or a year has no fixed length, so it cannot be a freshness bound.
_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_READY_BY = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_NAMES = {
    "month": ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    "weekday": ("sun", "mon", "tue", "wed", "thu", "fri", "sat"),
}
# (name, lowest, highest, first number of the name list); Sunday is both 0 and 7.
_CRON_FIELDS = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day of month", 1, 31, None),
    ("month", 1, 12, "month"),
    ("day of week", 0, 7, "weekday"),
)
_SLA_FIELDS = {"tier", "freshness", "availability", "minimumQualityScore"}
_FRESHNESS_FIELDS = {"maximumAge", "readyBy", "timezone"}
_AVAILABILITY_FIELDS = {"schedule", "gracePeriod"}


@dataclass(frozen=True)
class ProductSla:
    tier: str
    maximum_age: str | None = None
    ready_by: str | None = None
    timezone: str | None = None
    schedule: str | None = None
    grace_period: str | None = None
    minimum_quality_score: float | None = None

    @property
    def maximum_age_minutes(self) -> int | None:
        return duration_minutes(self.maximum_age) if self.maximum_age else None


def parse_duration(value: str) -> timedelta | None:
    """The length of an ISO 8601 duration made of weeks, days, hours, minutes and seconds; None when it is not one."""
    match = _DURATION.fullmatch(value)
    if not match or not any(match.groupdict().values()) or value.endswith("T"):
        return None
    parts = {unit: int(amount) for unit, amount in match.groupdict().items() if amount}
    length = timedelta(**parts)
    return length if length > timedelta(0) else None


def duration_minutes(value: str) -> int:
    """Whole minutes, rounded up: OpenMetadata's `dataFreshness` is an integer count of minutes."""
    length = parse_duration(value)
    if length is None:
        raise ValueError(f"not an ISO 8601 duration: {value!r}")
    return math.ceil(length.total_seconds() / 60)


def _cron_error(expression: str) -> str | None:
    fields = expression.split()
    if len(fields) != 5:
        return f"needs 5 space-separated fields (minute hour day-of-month month day-of-week), got {len(fields)}"
    for text, (name, low, high, names) in zip(fields, _CRON_FIELDS, strict=True):
        for item in text.split(","):
            body, _, step = item.partition("/")
            if "/" in item and not (step.isdigit() and int(step) > 0):
                return f"{name} step {step!r} must be a positive integer"
            bounds = [] if body == "*" else body.split("-")
            if len(bounds) > 2:
                return f"{name} {item!r} is not a value, a range or `*`"
            numbers = [low, high] if body == "*" else []
            lookup = _NAMES[names] if names else ()
            for bound in bounds:
                if bound.lower() in lookup:
                    numbers.append(lookup.index(bound.lower()) + (1 if names == "month" else 0))
                elif bound.isdigit():
                    numbers.append(int(bound))
                else:
                    return f"{name} {item!r} is not a value, a range or `*`"
            if not all(low <= n <= high for n in numbers) or numbers != sorted(numbers):
                return f"{name} {item!r} is outside {low}-{high} or a backwards range"
    return None


def validate_cron(expression: object) -> str | None:
    """Why `expression` is not a 5-field cron expression, or None when it is one."""
    if not isinstance(expression, str) or not expression.strip():
        return "must be a non-empty string"
    return _cron_error(expression)


def _unknown(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise LakehouseDescriptorError(f"{where}: must not contain {unexpected!r}; allowed: {sorted(allowed)!r}")


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise LakehouseDescriptorError(f"{where}: must be a non-empty object")
    return value


def _duration(value: object, where: str) -> str:
    if not isinstance(value, str) or parse_duration(value) is None:
        raise LakehouseDescriptorError(
            f"{where}: must be a positive ISO 8601 duration of weeks, days, hours, minutes or seconds "
            f"(for example PT26H or P1DT2H), got {value!r}"
        )
    return value


def parse_sla(value: object, *, where: str) -> ProductSla:
    """Validate a product's `sla` block; every error names its path under `where`."""
    sla = _mapping(value, f"{where}.sla")
    _unknown(sla, _SLA_FIELDS, f"{where}.sla")
    tier = sla.get("tier")
    if tier not in SLA_TIERS:
        raise LakehouseDescriptorError(f"{where}.sla.tier: must be one of {list(SLA_TIERS)!r}, got {tier!r}")
    maximum_age = ready_by = timezone = schedule = grace_period = None
    if "freshness" in sla:
        freshness = _mapping(sla["freshness"], f"{where}.sla.freshness")
        _unknown(freshness, _FRESHNESS_FIELDS, f"{where}.sla.freshness")
        if "maximumAge" not in freshness:
            raise LakehouseDescriptorError(f"{where}.sla.freshness.maximumAge: is required")
        maximum_age = _duration(freshness["maximumAge"], f"{where}.sla.freshness.maximumAge")
        if "readyBy" in freshness:
            ready_by = freshness["readyBy"]
            if not isinstance(ready_by, str) or not _READY_BY.fullmatch(ready_by):
                raise LakehouseDescriptorError(
                    f"{where}.sla.freshness.readyBy: must be a 24-hour HH:MM time, got {ready_by!r}"
                )
            if "timezone" not in freshness:
                raise LakehouseDescriptorError(
                    f"{where}.sla.freshness.timezone: is required with readyBy, which is a wall-clock time"
                )
        if "timezone" in freshness:
            timezone = freshness["timezone"]
            try:
                if not isinstance(timezone, str) or not timezone or timezone.startswith(("/", ".")):
                    raise ZoneInfoNotFoundError(str(timezone))
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
                raise LakehouseDescriptorError(
                    f"{where}.sla.freshness.timezone: must be an IANA timezone such as Europe/Paris, got {timezone!r}"
                ) from exc
    if "availability" in sla:
        availability = _mapping(sla["availability"], f"{where}.sla.availability")
        _unknown(availability, _AVAILABILITY_FIELDS, f"{where}.sla.availability")
        if "schedule" not in availability:
            raise LakehouseDescriptorError(f"{where}.sla.availability.schedule: is required")
        schedule = availability["schedule"]
        problem = validate_cron(schedule)
        if problem:
            raise LakehouseDescriptorError(
                f"{where}.sla.availability.schedule: not a valid 5-field cron expression ({problem}), got {schedule!r}"
            )
        if "gracePeriod" in availability:
            grace_period = _duration(availability["gracePeriod"], f"{where}.sla.availability.gracePeriod")
    score = sla.get("minimumQualityScore")
    if "minimumQualityScore" in sla and (
        isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100
    ):
        raise LakehouseDescriptorError(
            f"{where}.sla.minimumQualityScore: must be a number from 0 to 100 (percent), got {score!r}"
        )
    return ProductSla(tier, maximum_age, ready_by, timezone, schedule, grace_period, score)
