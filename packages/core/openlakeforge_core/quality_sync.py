"""What publishing quality results to a catalog needs to decide and to remember (#797).

Pure data and rules, no I/O: how an outcome becomes an OpenMetadata status, what a case is called, when a failed
publication is tried again, and the per-run state and stage summary the reconciler keeps in the ops bucket. The
summary is the stable signal the health route reads, so its shape is versioned like the quality manifest's.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from openlakeforge_core.health import HealthState, Scope, Signal, service_signal, worst_state
from openlakeforge_core.quality import QualityOutcome, QualityRecordError, QualityResult

STATE_VERSION = 1
SUMMARY_VERSION = 1

# Retried after 1, 2, 4 ... minutes up to the cap; after MAX_ATTEMPTS the result is `failed` and waits for a replay.
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_CAP = timedelta(hours=1)
MAX_ATTEMPTS = 10

# Keeps one run's response text, which could echo a request, from growing the state object or carrying a payload.
MAX_ERROR_LENGTH = 300


class OmStatus(StrEnum):
    """The `TestCaseStatus` values OpenMetadata 1.13.6 accepts for a result; `Queued` is never sent."""

    SUCCESS = "Success"
    FAILED = "Failed"
    ABORTED = "Aborted"


def om_status(outcome: QualityOutcome, *, warn_status: OmStatus = OmStatus.ABORTED) -> OmStatus | None:
    """OpenMetadata's status for an outcome, or None for a check that has no result to publish.

    OpenMetadata has no warning. `Success` would hide a WARN, and `Failed` opens an incident in OpenMetadata
    (`TestCaseResultRepository.setTestCaseResultIncidentId`, 1.13.6) for a check the pipeline survived. `Aborted`
    shows without one, so it is the default; the original outcome travels in the result's details either way.
    `not_run` and `unknown` are None: an unexecuted check never gets a result, as in the quality manifest.
    """
    if outcome is QualityOutcome.PASSED:
        return OmStatus.SUCCESS
    if outcome is QualityOutcome.ERROR:
        return OmStatus.FAILED
    if outcome is QualityOutcome.WARN:
        return warn_status
    return None


def case_name(result: QualityResult) -> str:
    """Stable across runs: the engine and the check, never the run. The table and column are the case's parent."""
    return f"olf.{result.engine}.{result.check_id}"


def definition_name(result: QualityResult) -> str:
    return f"olf_{result.engine}_{'column' if result.column else 'table'}"


class ResultState(StrEnum):
    PENDING = "pending"
    SYNCED = "synced"
    # Nothing to publish: a check that did not run.
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ResultSync:
    identity: str
    state: ResultState = ResultState.PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    # "table_not_registered" or a short sanitized message; never a response body.
    reason: str | None = None

    def due(self, now: datetime) -> bool:
        if self.state is not ResultState.PENDING:
            return False
        return self.next_attempt_at is None or self.next_attempt_at <= now

    def retry(self, now: datetime, reason: str) -> ResultSync:
        """One more failed attempt: back off, or give up to `failed` once the attempts are spent."""
        attempts = self.attempts + 1
        reason = sanitize(reason)
        if attempts >= MAX_ATTEMPTS:
            return replace(self, state=ResultState.FAILED, attempts=attempts, next_attempt_at=None, reason=reason)
        delay = min(BACKOFF_BASE * 2 ** (attempts - 1), BACKOFF_CAP)
        return replace(self, attempts=attempts, next_attempt_at=now + delay, reason=reason)

    def synced(self) -> ResultSync:
        return replace(self, state=ResultState.SYNCED, next_attempt_at=None, reason=None)

    def reset(self) -> ResultSync:
        return ResultSync(self.identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "state": self.state.value,
            "attempts": self.attempts,
            "next_attempt_at": _format(self.next_attempt_at),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResultSync:
        try:
            return cls(
                identity=str(data["identity"]),
                state=ResultState(data["state"]),
                attempts=int(data["attempts"]),
                next_attempt_at=_parse(data.get("next_attempt_at")),
                reason=data.get("reason"),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise QualityRecordError(f"quality sync result is invalid: {exc}") from exc


def sanitize(text: str) -> str:
    """One line, bounded. Callers pass a reason they composed, not a response body."""
    return " ".join(text.split())[:MAX_ERROR_LENGTH]


def run_key(stage: str, domain: str, product: str, dagster_run_id: str) -> str:
    """The run-scoped idempotence key: the same run of the same product in the same stage, whatever the manifest
    revision, so a manifest rewritten by a later step continues the run's state instead of starting another."""
    return hashlib.sha256(json.dumps([stage, domain, product, dagster_run_id]).encode()).hexdigest()


@dataclass(frozen=True)
class RunSync:
    """Everything published, pending or given up on for one run's manifest."""

    stage: str
    domain: str
    product: str
    dagster_run_id: str
    # Earliest `observed_at` of the manifest's results: lag is measured from the first thing left unpublished.
    observed_at: datetime
    results: Mapping[str, ResultSync] = field(default_factory=dict)
    # The manifest object's version the state was last reconciled against, so an unchanged one is not read again.
    manifest_etag: str | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    # Why the manifest cannot be published (malformed, foreign, unsupported version). Terminal for this ETag: the
    # run is looked at again only when the object changes.
    rejected: str | None = None

    @property
    def key(self) -> str:
        return run_key(self.stage, self.domain, self.product, self.dagster_run_id)

    def count(self, state: ResultState) -> int:
        return sum(1 for r in self.results.values() if r.state is state)

    @property
    def settled(self) -> bool:
        return self.count(ResultState.PENDING) == 0

    def to_json(self) -> str:
        document = {
            "version": STATE_VERSION,
            "key": self.key,
            "stage": self.stage,
            "domain": self.domain,
            "product": self.product,
            "dagster_run_id": self.dagster_run_id,
            "observed_at": _format(self.observed_at),
            "manifest_etag": self.manifest_etag,
            "last_success_at": _format(self.last_success_at),
            "last_failure_at": _format(self.last_failure_at),
            "rejected": self.rejected,
            "results": [r.to_dict() for _, r in sorted(self.results.items())],
        }
        return json.dumps(document, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RunSync:
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise QualityRecordError(f"unsupported quality sync state version {data.get('version')!r}")
            results = [ResultSync.from_dict(r) for r in data["results"]]
            run = cls(
                stage=data["stage"],
                domain=data["domain"],
                product=data["product"],
                dagster_run_id=data["dagster_run_id"],
                observed_at=_require(_parse(data["observed_at"])),
                results={r.identity: r for r in results},
                manifest_etag=data.get("manifest_etag"),
                last_success_at=_parse(data.get("last_success_at")),
                last_failure_at=_parse(data.get("last_failure_at")),
                rejected=data.get("rejected"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise QualityRecordError(f"quality sync state is invalid: {exc}") from exc
        if data.get("key") != run.key:
            raise QualityRecordError("quality sync state key does not match its run")
        return run


@dataclass(frozen=True)
class SyncSummary:
    """One stage's publication health, in the one object a health reader needs (#798).

    `lag_seconds` is how long the oldest unpublished result has been waiting, None when nothing is. A failed
    publication is a state of its own: data quality is read from the manifest, never from this.
    """

    stage: str
    observed_at: datetime
    pending: int = 0
    failed: int = 0
    synced: int = 0
    # Runs whose manifest exists but has not been read yet (a bounded pass stopped before them): their results
    # are unpublished and uncounted, and their age counts toward the lag.
    unread_runs: int = 0
    # Manifests that cannot be published at all; counted so a broken writer is visible, never retried.
    rejected_runs: int = 0
    oldest_pending_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None

    @property
    def lag_seconds(self) -> int | None:
        if self.oldest_pending_at is None:
            return None
        return max(0, int((self.observed_at - self.oldest_pending_at).total_seconds()))

    def to_json(self) -> str:
        document = {
            "version": SUMMARY_VERSION,
            "stage": self.stage,
            "observed_at": _format(self.observed_at),
            "pending": self.pending,
            "failed": self.failed,
            "synced": self.synced,
            "unread_runs": self.unread_runs,
            "rejected_runs": self.rejected_runs,
            "lag_seconds": self.lag_seconds,
            "oldest_pending_at": _format(self.oldest_pending_at),
            "last_success_at": _format(self.last_success_at),
            "last_failure_at": _format(self.last_failure_at),
            "last_error": self.last_error,
        }
        return json.dumps(document, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> SyncSummary:
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("version") != SUMMARY_VERSION:
                raise QualityRecordError("unsupported quality sync summary version")
            return cls(
                stage=data["stage"],
                observed_at=_require(_parse(data["observed_at"])),
                pending=int(data["pending"]),
                failed=int(data["failed"]),
                synced=int(data["synced"]),
                unread_runs=int(data.get("unread_runs", 0)),
                rejected_runs=int(data.get("rejected_runs", 0)),
                oldest_pending_at=_parse(data.get("oldest_pending_at")),
                last_success_at=_parse(data.get("last_success_at")),
                last_failure_at=_parse(data.get("last_failure_at")),
                last_error=data.get("last_error"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise QualityRecordError(f"quality sync summary is invalid: {exc}") from exc


def summarize(
    stage: str,
    runs: list[RunSync],
    *,
    now: datetime,
    last_error: str | None = None,
    unread: list[datetime] | None = None,
) -> SyncSummary:
    unread = unread or []
    oldest = [
        *unread,
        *(run.observed_at for run in runs if run.count(ResultState.PENDING) or run.count(ResultState.FAILED)),
    ]
    successes = [r.last_success_at for r in runs if r.last_success_at]
    failures = [r.last_failure_at for r in runs if r.last_failure_at]
    return SyncSummary(
        stage=stage,
        observed_at=now.astimezone(UTC),
        pending=sum(run.count(ResultState.PENDING) for run in runs),
        failed=sum(run.count(ResultState.FAILED) for run in runs),
        synced=sum(run.count(ResultState.SYNCED) for run in runs),
        unread_runs=len(unread),
        rejected_runs=sum(1 for run in runs if run.rejected),
        oldest_pending_at=min(oldest) if oldest else None,
        last_success_at=max(successes) if successes else None,
        last_failure_at=max(failures) if failures else None,
        last_error=last_error,
    )


PUBLICATION_COMPONENT = "quality-sync.publication"


@dataclass(frozen=True)
class PublicationThresholds:
    """When the publication signal turns `warning` and `critical` (#798).

    The reconciler runs every 5 minutes and a pass stops after 240 seconds, so a working one leaves a summary at most
    about 9 minutes old: the staleness warning is three schedule intervals (two passes missed in a row), critical is
    twelve. A result is published by the first pass after its manifest lands; one that keeps failing is retried after
    1, 2, 4, 8 minutes, so 30 minutes of lag is several refused passes and 2 hours is a backoff that has reached its
    1-hour cap. A result that gave up (`failed`) waits for an operator, so it is critical at once.
    """

    stale_warning: timedelta = timedelta(minutes=15)
    stale_critical: timedelta = timedelta(hours=1)
    lag_warning: timedelta = timedelta(minutes=30)
    lag_critical: timedelta = timedelta(hours=2)

    def __post_init__(self) -> None:
        if not timedelta(0) < self.stale_warning < self.stale_critical:
            raise ValueError("the staleness thresholds must be positive, warning below critical")
        if not timedelta(0) < self.lag_warning < self.lag_critical:
            raise ValueError("the lag thresholds must be positive, warning below critical")


def evaluate_publication(
    stage: str,
    summary: SyncSummary | None,
    *,
    now: datetime,
    max_age: timedelta,
    thresholds: PublicationThresholds = PublicationThresholds(),  # noqa: B008 - frozen, so sharing the default is safe
) -> Signal:
    """Whether the stage's quality results are reaching OpenMetadata, from the reconciler's last summary.

    Publication only: whether the data passed its checks is the `quality` signal, read from the manifests. `None`
    is a summary nobody could read or that does not exist yet, and is `unknown`: a reconciler that never finished a
    pass must not read healthy.
    """
    scope = Scope(stage=stage)

    def signal(state: HealthState, reason: str) -> Signal:
        return service_signal(scope, PUBLICATION_COMPONENT, state, reason, now=now, max_age=max_age)

    if summary is None:
        return signal(HealthState.UNKNOWN, "no publication summary: the reconciler has not finished a pass")
    if summary.stage != stage:
        return signal(HealthState.UNKNOWN, f"the publication summary is stage {summary.stage!r}'s")
    age = max(timedelta(0), now - summary.observed_at)
    lag = None if summary.oldest_pending_at is None else max(timedelta(0), now - summary.oldest_pending_at)
    error = f": {sanitize(summary.last_error)}" if summary.last_error else ""
    findings = [(HealthState.HEALTHY, f"last pass {_minutes(age)} ago, {summary.pending} result(s) waiting")]
    if age >= thresholds.stale_warning:
        state = HealthState.CRITICAL if age >= thresholds.stale_critical else HealthState.WARNING
        findings.append((state, f"no publication pass for {_minutes(age)}"))
    if summary.failed:
        gave_up = f"{summary.failed} result(s) gave up after {MAX_ATTEMPTS} attempts{error}"
        findings.append((HealthState.CRITICAL, gave_up))
    if lag is not None and lag >= thresholds.lag_warning:
        state = HealthState.CRITICAL if lag >= thresholds.lag_critical else HealthState.WARNING
        findings.append((state, f"{summary.pending} result(s) waiting, the oldest for {_minutes(lag)}{error}"))
    if summary.rejected_runs:
        findings.append((HealthState.WARNING, f"{summary.rejected_runs} manifest(s) cannot be published"))
    worst = worst_state(state for state, _ in findings)
    return signal(worst, next(reason for state, reason in findings if state is worst))


def _minutes(age: timedelta) -> str:
    return f"{age.total_seconds() / 60:.0f} min"


def epoch_millis(moment: datetime) -> int:
    """The event time in the unit OpenMetadata's `timestamp` takes."""
    return int(moment.timestamp() * 1000)


def _format(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise QualityRecordError("a time must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise QualityRecordError("a time must be timezone-aware")
    return parsed


def _require(value: datetime | None) -> datetime:
    if value is None:
        raise QualityRecordError("a required time is missing")
    return value
