"""Engine-neutral `quality_result` record and the per-run manifest that carries it.

Pure data and serialization, no I/O: adapters for Floe and dbt build records, a writer puts the manifest
bytes in the ops bucket, and the OpenMetadata publisher replays it. None of them are in this module, so
it stays in `core` and a consumer needs neither boto3 nor a service client to read evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1

MANIFEST_FILENAME = "manifest.json"


class QualityOutcome(StrEnum):
    """What the assertion itself reported. `not_run` is a check that was selected but never executed."""

    PASSED = "passed"
    WARN = "warn"
    ERROR = "error"
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"


class PipelineOutcome(StrEnum):
    """What a failed assertion did to the pipeline, which the engine's own severity does not say.

    Floe `policy.severity: reject` quarantines rows and the run carries on, so it is `QUARANTINED`, not
    `BLOCKED`. Only the adapter knows which one happened; this module never derives it from severity.
    """

    CONTINUED = "continued"
    QUARANTINED = "quarantined"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


# Row payloads, sample values and secrets must never reach the normalized record (#796). The record has no
# field for them, so the guard is on the way in: a payload smuggled into the JSON or the metrics is refused.
_FORBIDDEN_KEYS = frozenset(
    "row rows sample samples sample_values value values payload record records secret password token "
    "credential credentials access_key secret_key".split()
)

# `\Z`, not `$`: `$` also matches before a trailing newline, which would pass into an object key.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

_RECORD_KEYS = frozenset(
    {
        "version",
        "identity",
        "engine",
        "stage",
        "domain",
        "product",
        "table",
        "column",
        "check_id",
        "engine_severity",
        "outcome",
        "pipeline_outcome",
        "observed_at",
        "dagster_run_id",
        "engine_run_id",
        "artifact_uri",
        "metrics",
    }
)


class QualityRecordError(ValueError):
    """A record or manifest that is malformed, of an unknown version, or carries forbidden content."""


@dataclass(frozen=True)
class QualityResult:
    """One execution of one check, as the engine reported it and as the pipeline experienced it."""

    engine: str
    stage: str
    check_id: str
    # The engine's own severity or policy, verbatim (`reject`, `warn`, `error`...), never normalized.
    engine_severity: str
    outcome: QualityOutcome
    pipeline_outcome: PipelineOutcome
    observed_at: datetime
    dagster_run_id: str
    domain: str | None = None
    product: str | None = None
    table: str | None = None
    column: str | None = None
    engine_run_id: str | None = None
    # An opaque pointer into ops storage, never log text and never a presigned URL.
    artifact_uri: str | None = None
    # Aggregate counts and durations only; a string value could carry a sample, so numbers are all it takes.
    metrics: Mapping[str, int | float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # By type, not only emptiness: unhashed fields such as engine_severity would otherwise carry a payload.
        for name in ("engine", "stage", "check_id", "engine_severity", "dagster_run_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise QualityRecordError(f"a quality result needs a non-empty string {name}")
        for name in ("domain", "product", "table", "column", "engine_run_id", "artifact_uri"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise QualityRecordError(f"{name} must be a string or null, not {type(value).__name__}")
        if not isinstance(self.observed_at, datetime):
            raise QualityRecordError("observed_at must be a datetime")
        if not isinstance(self.outcome, QualityOutcome) or not isinstance(self.pipeline_outcome, PipelineOutcome):
            raise QualityRecordError("outcome and pipeline_outcome must be enum members")
        if self.observed_at.tzinfo is None:
            raise QualityRecordError("observed_at must be timezone-aware")
        if self.column is not None and self.table is None:
            raise QualityRecordError("a column needs the table it belongs to")
        if self.artifact_uri is not None:
            _check_artifact_uri(self.artifact_uri)
        for key, value in self.metrics.items():
            if key.lower() in _FORBIDDEN_KEYS:
                raise QualityRecordError(f"metrics key {key!r} could carry row data; only aggregates are allowed")
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise QualityRecordError(f"metric {key!r} must be a number, not {type(value).__name__}")
            if not math.isfinite(value):
                raise QualityRecordError(f"metric {key!r} must be finite, not {value!r}")
        object.__setattr__(self, "metrics", dict(sorted(self.metrics.items())))

    @property
    def identity(self) -> str:
        """Stable per check execution: replaying the same execution yields the same id, so a replay upserts.

        The outcome, timestamp and metrics are left out on purpose: a re-read of the same report may add a
        counter or round a time, and that must not create a second result in the consumer.
        """
        parts = [
            self.stage,
            self.domain,
            self.product,
            self.table,
            self.column,
            self.engine,
            self.check_id,
            self.dagster_run_id,
            self.engine_run_id,
        ]
        # JSON, not a delimiter join, so ("a|b", None) and ("a", "b") cannot collide.
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "identity": self.identity,
            "engine": self.engine,
            "stage": self.stage,
            "domain": self.domain,
            "product": self.product,
            "table": self.table,
            "column": self.column,
            "check_id": self.check_id,
            "engine_severity": self.engine_severity,
            "outcome": self.outcome.value,
            "pipeline_outcome": self.pipeline_outcome.value,
            "observed_at": _format_time(self.observed_at),
            "dagster_run_id": self.dagster_run_id,
            "engine_run_id": self.engine_run_id,
            "artifact_uri": self.artifact_uri,
            "metrics": dict(self.metrics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QualityResult:
        _check_version(data)
        unknown = set(data) - _RECORD_KEYS
        if unknown:
            forbidden = sorted(k for k in unknown if k.lower() in _FORBIDDEN_KEYS)
            if forbidden:
                raise QualityRecordError(f"quality result carries forbidden fields {forbidden}: no row data or secrets")
            raise QualityRecordError(f"quality result has unknown fields {sorted(unknown)}")
        try:
            record = cls(
                engine=data["engine"],
                stage=data["stage"],
                domain=data.get("domain"),
                product=data.get("product"),
                table=data.get("table"),
                column=data.get("column"),
                check_id=data["check_id"],
                engine_severity=data["engine_severity"],
                outcome=QualityOutcome(data["outcome"]),
                pipeline_outcome=PipelineOutcome(data["pipeline_outcome"]),
                observed_at=_parse_time(data["observed_at"]),
                dagster_run_id=data["dagster_run_id"],
                engine_run_id=data.get("engine_run_id"),
                artifact_uri=data.get("artifact_uri"),
                metrics=data.get("metrics") or {},
            )
        except KeyError as exc:
            raise QualityRecordError(f"quality result is missing {exc.args[0]!r}") from exc
        except QualityRecordError:
            raise
        except (ValueError, TypeError, AttributeError) as exc:
            raise QualityRecordError(f"quality result is invalid: {exc}") from exc
        stored = data.get("identity")
        if stored is not None and stored != record.identity:
            raise QualityRecordError("quality result identity does not match its fields")
        return record


@dataclass(frozen=True)
class QualityManifest:
    """Every `QualityResult` of one Dagster run of one product pipeline in one stage."""

    stage: str
    domain: str
    product: str
    dagster_run_id: str
    results: tuple[QualityResult, ...] = ()
    # Set only by the run's last publish, once every step that reports has reported. A manifest without it may lack
    # the later steps' results, so a pass in it is not evidence of a healthy run.
    complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise QualityRecordError("manifest complete must be a boolean")
        for name in ("stage", "domain", "product", "dagster_run_id"):
            if not _SEGMENT.match(getattr(self, name)):
                raise QualityRecordError(f"manifest {name} {getattr(self, name)!r} is not a safe path segment")
        seen: set[str] = set()
        for result in self.results:
            if (result.stage, result.dagster_run_id) != (self.stage, self.dagster_run_id):
                raise QualityRecordError(
                    f"result {result.check_id!r} belongs to stage {result.stage!r} run {result.dagster_run_id!r}, "
                    f"not this manifest's stage {self.stage!r} run {self.dagster_run_id!r}"
                )
            for name in ("domain", "product"):
                if getattr(result, name) not in (None, getattr(self, name)):
                    raise QualityRecordError(
                        f"result {result.check_id!r} has {name} {getattr(result, name)!r}, "
                        f"not this manifest's {getattr(self, name)!r}"
                    )
            if result.identity in seen:
                raise QualityRecordError(f"duplicate quality result for check {result.check_id!r}")
            seen.add(result.identity)
        # Sorted so the same results always serialize to the same bytes, whatever order the adapters ran in.
        object.__setattr__(self, "results", tuple(sorted(self.results, key=lambda r: r.identity)))

    @property
    def key(self) -> str:
        """Object key under the stage's artifact prefix, beside `run-artifacts/dbt/<domain>/<product>/<run>/`."""
        return manifest_key(self.domain, self.product, self.dagster_run_id)

    @property
    def ops_key(self) -> str:
        """Object key in the shared ops bucket, where the stage's grant is `activations/<stage>/`."""
        return f"activations/{self.stage}/{self.key}"

    def to_json(self) -> str:
        document = {
            "version": SCHEMA_VERSION,
            "stage": self.stage,
            "domain": self.domain,
            "product": self.product,
            "dagster_run_id": self.dagster_run_id,
            "complete": self.complete,
            "results": [r.to_dict() for r in self.results],
        }
        return json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> QualityManifest:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise QualityRecordError(f"quality manifest is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise QualityRecordError("quality manifest must be a JSON object")
        _check_version(data)
        unknown = set(data) - {"version", "stage", "domain", "product", "dagster_run_id", "results", "complete"}
        if unknown:
            raise QualityRecordError(f"quality manifest has unknown fields {sorted(unknown)}")
        results = data.get("results")
        if not isinstance(results, list):
            raise QualityRecordError("quality manifest results must be a list")
        for entry in results:
            if not isinstance(entry, dict):
                raise QualityRecordError(f"quality manifest result must be an object, not {type(entry).__name__}")
        try:
            return cls(
                stage=data["stage"],
                domain=data["domain"],
                product=data["product"],
                dagster_run_id=data["dagster_run_id"],
                results=tuple(QualityResult.from_dict(r) for r in results),
                complete=data.get("complete", False),
            )
        except KeyError as exc:
            raise QualityRecordError(f"quality manifest is missing {exc.args[0]!r}") from exc
        except (TypeError, AttributeError) as exc:
            raise QualityRecordError(f"quality manifest is invalid: {exc}") from exc


def manifest_key(domain: str, product: str, dagster_run_id: str) -> str:
    """The manifest's key relative to a stage's artifact prefix, keyed by Dagster run like the dbt artifacts."""
    for name, value in (("domain", domain), ("product", product), ("dagster_run_id", dagster_run_id)):
        if not _SEGMENT.match(value):
            raise QualityRecordError(f"{name} {value!r} is not a safe path segment")
    return f"run-artifacts/quality/{domain}/{product}/{dagster_run_id}/{MANIFEST_FILENAME}"


@dataclass(frozen=True)
class HealthQualityFields:
    """The arguments `health.QualityObservation` takes."""

    outcome: QualityOutcome
    observed_at: datetime
    blocking: bool
    run: str
    evidence_uri: str | None


def health_quality_fields(result: QualityResult) -> HealthQualityFields | None:
    """Seam to the health vocabulary: `QualityObservation(f.outcome, f.observed_at, f.blocking, f.run, ...)`.

    `None` for `not_run` and `unknown`: no observation is how a caller keeps a missing outcome out of a
    pass. A caller that passes such an outcome to `evaluate_quality` anyway gets `unknown` too.
    """
    if result.outcome in (QualityOutcome.NOT_RUN, QualityOutcome.UNKNOWN):
        return None
    return HealthQualityFields(
        outcome=result.outcome,
        observed_at=result.observed_at,
        # An error that quarantined rows or let the pipeline continue is a warning to health, not a failure.
        blocking=result.pipeline_outcome is PipelineOutcome.BLOCKED,
        run=result.dagster_run_id,
        evidence_uri=result.artifact_uri,
    )


def _check_version(data: Mapping[str, Any]) -> None:
    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise QualityRecordError(f"unsupported quality record version {version!r}; reader handles {SCHEMA_VERSION}")


def _check_artifact_uri(uri: str) -> None:
    parts = urlsplit(uri)
    # Userinfo and query strings are where presigned URLs and embedded keys live.
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise QualityRecordError("artifact_uri must be a plain object pointer, without credentials or query string")


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise QualityRecordError("observed_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QualityRecordError(f"observed_at {value!r} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise QualityRecordError("observed_at must be timezone-aware")
    return parsed
