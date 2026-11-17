"""Publishes a stage's quality manifests to OpenMetadata as test case results (#797, ADR 0007).

The manifest in the ops bucket is the evidence; this module only replays it. It never runs a check, never
creates a table, and a failure here is a state in the bucket, not an exception into whatever called it. One
`QualityPublisher` serves one stage: its database FQN is the only prefix it can build a table name from, so a
result of another stage has nowhere to go.

Layout under the stage's activation prefix: `run-artifacts/quality/<domain>/<product>/<run>/manifest.json`
(written by the pipeline), and, written here, `quality-sync/state/<domain>/<product>/<run>.json` and
`quality-sync/summary.json`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityRecordError, QualityResult
from openlakeforge_core.quality_sync import (
    OmStatus,
    ResultState,
    ResultSync,
    RunSync,
    SyncSummary,
    case_name,
    definition_name,
    epoch_millis,
    om_status,
    summarize,
)

from .logs import ObjectStore
from .openmetadata import OpenMetadataClient, OpenMetadataError, OpenMetadataTransientError

log = logging.getLogger(__name__)

STATE_PREFIX = "quality-sync/state"
SUMMARY_KEY = "quality-sync/summary.json"
MANIFEST_PREFIX = "run-artifacts/quality"
DEFAULT_LIMIT = 25
# The `testResultValue` name carrying a result's execution identity, and how far around a timestamp it is looked
# for: a run's records are restamped while it runs, never by days.
IDENTITY_VALUE = "olf_identity"
IDENTITY_WINDOW_MS = 24 * 60 * 60 * 1000
# Manifests older than this are not scanned: it bounds the listing, and an older run is replayed by naming it.
DEFAULT_MAX_AGE = timedelta(days=30)

_MISSING = frozenset({"NoSuchKey", "404"})


class SyncStore(ObjectStore, Protocol):
    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> Any: ...  # noqa: N803


class StoreUnavailableError(Exception):
    """The bucket cannot be read or written, so nothing can be recorded and nothing is published."""


@dataclass(frozen=True)
class ManifestRef:
    domain: str
    product: str
    dagster_run_id: str
    etag: str | None
    modified: datetime


class QualityCatalog:
    """The OpenMetadata 1.13.6 routes the publisher uses, and nothing else. Request bodies follow
    `api/tests/createTestCase.json`, `createTestDefinition.json` and `createTestCaseResult.json` of that release."""

    def __init__(self, client: OpenMetadataClient) -> None:
        self._client = client

    def table_columns(self, table_fqn: str) -> frozenset[str] | None:
        """The registered table's column names, or None when no such table is registered."""
        try:
            table = self._client.request("GET", f"/api/v1/tables/name/{_path(table_fqn)}", params={"fields": "columns"})
        except OpenMetadataError as exc:
            if exc.status == 404:
                return None
            raise
        # A lookup that answers with another entity must not be taken as the table asked for.
        if table.get("fullyQualifiedName") != table_fqn:
            return None
        return frozenset(c["name"] for c in table.get("columns") or [] if "name" in c)

    def ensure_definition(self, name: str, *, column: bool) -> None:
        self._client.request(
            "PUT",
            "/api/v1/dataQuality/testDefinitions",
            json_body={
                "name": name,
                "displayName": name,
                "description": "A check an OpenLakeForge pipeline engine ran; published after the run, never rerun.",
                "entityType": "COLUMN" if column else "TABLE",
                "testPlatforms": ["Other"],
                "parameterDefinition": [],
            },
            ok_statuses=(200, 201),
        )

    def upsert_case(
        self, *, name: str, definition: str, table_fqn: str, column: str | None, description: str
    ) -> str:
        """The case's FQN as OpenMetadata names it. It is not `<table>.<name>`: `FullyQualifiedName.add` quotes a
        segment that contains a dot, and case names do, so every later call addresses the case by this value."""
        link = f"<#E::table::{table_fqn}" + (f"::columns::{column}>" if column else ">")
        case = self._client.request(
            "PUT",
            "/api/v1/dataQuality/testCases",
            json_body={
                "name": name,
                "displayName": name,
                "description": description,
                "testDefinition": definition,
                "entityLink": link,
                "parameterValues": [],
            },
            ok_statuses=(200, 201),
        )
        fqn = case.get("fullyQualifiedName")
        if not isinstance(fqn, str) or not fqn:
            raise OpenMetadataError("OpenMetadata did not name the test case it stored")
        return fqn

    def has_result(self, case_fqn: str, timestamp: int, identity: str) -> bool:
        """Whether this execution is already there, by its identity and not only by its timestamp: a manifest
        rewritten by a later step of the run restamps a record, and the same execution must not be added twice."""
        page = self._client.request(
            "GET",
            f"/api/v1/dataQuality/testCases/testCaseResults/{_path(case_fqn)}",
            params={"startTs": timestamp - IDENTITY_WINDOW_MS, "endTs": timestamp + IDENTITY_WINDOW_MS},
        )
        for stored in page.get("data") or []:
            values = {v.get("name"): v.get("value") for v in stored.get("testResultValue") or []}
            if stored.get("timestamp") == timestamp or values.get(IDENTITY_VALUE) == identity:
                return True
        return False

    def add_result(self, case_fqn: str, body: Mapping[str, Any]) -> None:
        self._client.request(
            "POST",
            f"/api/v1/dataQuality/testCases/testCaseResults/{_path(case_fqn)}",
            json_body=body,
            ok_statuses=(200, 201),
        )


@dataclass(frozen=True)
class ReconcileReport:
    runs_examined: int = 0
    published: int = 0
    deferred: int = 0
    failed: int = 0
    skipped: int = 0
    summary: SyncSummary | None = None
    # Stopped early because OpenMetadata or the bucket could not be reached; the rest stays pending.
    interrupted: bool = False


@dataclass
class _Pass:
    published: int = 0
    deferred: int = 0
    failed: int = 0
    skipped: int = 0
    interrupted: bool = False
    errors: list[str] = field(default_factory=list)


class QualityPublisher:
    def __init__(
        self,
        *,
        store: SyncStore,
        base_uri: str,
        stage: str,
        catalog: QualityCatalog,
        database_fqn: str,
        warn_status: OmStatus = OmStatus.ABORTED,
        max_age: timedelta = DEFAULT_MAX_AGE,
        silver_schemas: Mapping[str, str] | None = None,
        gold_schemas: Mapping[str, str] | None = None,
    ) -> None:
        parts = urlsplit(base_uri)
        if parts.scheme != "s3" or not parts.netloc:
            raise ValueError(f"expected an s3:// activation prefix, got {base_uri!r}")
        if not database_fqn or database_fqn.count(".") != 1 or not database_fqn.endswith(f".lakehouse_{stage}"):
            # `<catalog service>.lakehouse_<stage>`: a database root of another stage is a misconfiguration to
            # refuse here rather than a place to publish to.
            raise ValueError(f"database FQN {database_fqn!r} is not the lakehouse_{stage} root of stage {stage!r}")
        self._store = store
        self._bucket = parts.netloc
        self._prefix = parts.path.strip("/")
        self._stage = stage
        self._catalog = catalog
        self._database = database_fqn
        self._warn = warn_status
        self._max_age = max_age
        # Domain -> Silver schema FQN and product -> Gold schema FQN from the provider contract. A provider that
        # prefixes physical schemas per stage (AWS Glue) is only reachable through them, never by rebuilding
        # `<domain>_silver` from the logical name.
        self._silver = _scoped(silver_schemas, database_fqn)
        self._gold = _scoped(gold_schemas, database_fqn)

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        run_id: str | None = None,
        retry_failed: bool = False,
    ) -> ReconcileReport:
        """One bounded pass: at most `limit` runs are worked on. `run_id` replays that run whatever its state;
        `retry_failed` gives every given-up result a fresh set of attempts."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if limit < 1:
            raise ValueError("limit must be at least 1")
        # The summary is the whole stage's backlog, so every run in the scan window is read, whatever this pass works
        # on; a replay of one run must not hide another run's failure.
        listed = self._list_manifests()
        refs = [r for r in listed if now - r.modified <= self._max_age or r.dagster_run_id == run_id]
        states = {_id(ref): self._read_state(ref) for ref in refs}
        work: list[tuple[ManifestRef, RunSync | None]] = []
        for ref in sorted(refs, key=lambda r: r.modified):
            state = states[_id(ref)]
            if run_id is not None:
                if ref.dagster_run_id == run_id:
                    work.append((ref, state))
            elif _needs_work(ref, state, now) or (retry_failed and _has_failed(state)):
                work.append((ref, state))
        outcome = _Pass()
        examined = 0
        for ref, state in work[:limit]:
            examined += 1
            updated = self._reconcile_run(ref, state, now, outcome, force=run_id is not None, retry_failed=retry_failed)
            if updated is not None:
                states[_id(ref)] = updated
            if outcome.interrupted:
                break
        runs = [s for s in states.values() if s is not None]
        # A manifest with no state and not worked on is unpublished work no state object describes yet.
        unread = [r.modified for r in refs if states[_id(r)] is None]
        summary = summarize(self._stage, runs, now=now, last_error=_last_error(runs), unread=unread)
        self._put(self._key(SUMMARY_KEY), summary.to_json())
        return ReconcileReport(
            runs_examined=examined,
            published=outcome.published,
            deferred=outcome.deferred,
            failed=outcome.failed,
            skipped=outcome.skipped,
            summary=summary,
            interrupted=outcome.interrupted,
        )

    def _reconcile_run(
        self, ref: ManifestRef, state: RunSync | None, now: datetime, outcome: _Pass, *, force: bool, retry_failed: bool
    ) -> RunSync | None:
        manifest, rejected = self._read_manifest(ref)
        if rejected is not None:
            observed = state.observed_at if state else ref.modified
            run = RunSync(
                stage=self._stage,
                domain=ref.domain,
                product=ref.product,
                dagster_run_id=ref.dagster_run_id,
                observed_at=observed,
                manifest_etag=ref.etag,
                rejected=rejected,
            )
            self._put(self._state_key(ref), run.to_json())
            return run
        if manifest is None:
            return state
        run = self._merge(manifest, ref, state, now, reset=force, retry_failed=retry_failed)
        results = {r.identity: r for r in manifest.results}
        for identity, sync in list(run.results.items()):
            if not sync.due(now):
                continue
            result = results[identity]
            updated = self._publish(result, sync, now, outcome)
            run = _with_result(run, updated, now)
            if outcome.interrupted:
                break
        self._put(self._state_key(ref), run.to_json())
        return run

    def _merge(
        self, manifest: QualityManifest, ref: ManifestRef, state: RunSync | None, now: datetime, *, reset: bool,
        retry_failed: bool,
    ) -> RunSync:
        results: dict[str, ResultSync] = {}
        for result in manifest.results:
            identity = result.identity
            previous = state.results.get(identity) if state else None
            if om_status(result.outcome, warn_status=self._warn) is None:
                results[identity] = ResultSync(identity, ResultState.SKIPPED, reason="check did not run")
            elif previous is None or previous.state is ResultState.SKIPPED:
                results[identity] = ResultSync(identity)
            elif reset and previous.state is not ResultState.SYNCED:
                results[identity] = previous.reset()
            elif retry_failed and previous.state is ResultState.FAILED:
                results[identity] = previous.reset()
            else:
                # A synced result stays synced on a replay: the catalog already holds its timestamp.
                results[identity] = previous
        observed = min((r.observed_at for r in manifest.results), default=ref.modified)
        return RunSync(
            stage=self._stage,
            domain=manifest.domain,
            product=manifest.product,
            dagster_run_id=manifest.dagster_run_id,
            observed_at=observed,
            results=results,
            manifest_etag=ref.etag,
            last_success_at=state.last_success_at if state else None,
            last_failure_at=state.last_failure_at if state else None,
        )

    def _publish(self, result: QualityResult, sync: ResultSync, now: datetime, outcome: _Pass) -> ResultSync:
        status = om_status(result.outcome, warn_status=self._warn)
        if status is None:  # unreachable: _merge marks these skipped
            return ResultSync(sync.identity, ResultState.SKIPPED)
        target = self._target(result)
        if target is None:
            outcome.skipped += 1
            return ResultSync(sync.identity, ResultState.SKIPPED, reason="the check has no table identity")
        table_fqn, column = target
        try:
            columns = self._catalog.table_columns(table_fqn)
            if columns is None:
                outcome.deferred += 1
                return sync.retry(now, "table not registered in OpenMetadata")
            if column is not None and column not in columns:
                outcome.deferred += 1
                return sync.retry(now, "column not registered in OpenMetadata")
            name = case_name(result)
            timestamp = epoch_millis(result.observed_at)
            self._catalog.ensure_definition(definition_name(result), column=column is not None)
            case_fqn = self._catalog.upsert_case(
                name=name,
                definition=definition_name(result),
                table_fqn=table_fqn,
                column=column,
                description=f"{result.engine} check {result.check_id}, published from the run's quality manifest.",
            )
            if not self._catalog.has_result(case_fqn, timestamp, result.identity):
                self._catalog.add_result(case_fqn, result_body(result, status, timestamp))
        except OpenMetadataTransientError as exc:
            outcome.interrupted = True
            outcome.failed += 1
            return sync.retry(now, f"OpenMetadata unavailable ({_describe(exc)})")
        except OpenMetadataError as exc:
            outcome.failed += 1
            return sync.retry(now, f"OpenMetadata refused the request ({_describe(exc)})")
        outcome.published += 1
        return sync.synced()

    def _target(self, result: QualityResult) -> tuple[str, str | None] | None:
        table = result.table
        if not table:
            return None
        schema, _, name = table.rpartition(".")
        if not name or schema.count(".") or (not schema and (result.engine != "floe" or not result.domain)):
            return None
        schema = schema or f"{result.domain}_silver"
        fqn = self._schema_fqn(schema, result)
        return (f"{fqn}.{name}", result.column) if fqn else None

    def _schema_fqn(self, schema: str, result: QualityResult) -> str | None:
        """The registered schema FQN of a logical (`<domain>_silver`, `<product>_gold`) or physical schema name."""
        if not self._silver and not self._gold:
            return f"{self._database}.{schema}"
        known: dict[str, str] = {}
        for domain, fqn in self._silver.items():
            known[f"{domain}_silver"] = fqn
        for product, fqn in self._gold.items():
            known[f"{product}_gold"] = fqn
        known.update({fqn.rsplit(".", 1)[1]: fqn for fqn in (*self._silver.values(), *self._gold.values())})
        return known.get(schema)

    def _list_manifests(self) -> list[ManifestRef]:
        refs: list[ManifestRef] = []
        prefix = self._key(MANIFEST_PREFIX) + "/"
        for item in self._list(prefix):
            parts = item["Key"].removeprefix(prefix).split("/")
            if len(parts) != 4 or parts[3] != "manifest.json":
                continue
            refs.append(
                ManifestRef(
                    domain=parts[0],
                    product=parts[1],
                    dagster_run_id=parts[2],
                    etag=item.get("ETag"),
                    modified=item["LastModified"].astimezone(UTC),
                )
            )
        return refs

    def _list(self, prefix: str) -> Iterator[Mapping[str, Any]]:
        token: str | None = None
        while True:
            args: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                args["ContinuationToken"] = token
            try:
                page = self._store.list_objects_v2(**args)
            except Exception as exc:
                raise StoreUnavailableError(f"{prefix} could not be listed: {type(exc).__name__}") from exc
            yield from page.get("Contents") or []
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token:
                return

    def _read_manifest(self, ref: ManifestRef) -> tuple[QualityManifest | None, str | None]:
        """The manifest, or the reason it is rejected; neither when the object vanished since it was listed."""
        key = self._key(MANIFEST_PREFIX, ref.domain, ref.product, ref.dagster_run_id, "manifest.json")
        body = self._get(key)
        if body is None:
            return None, None
        try:
            manifest = QualityManifest.from_json(body)
        except QualityRecordError as exc:
            log.warning("quality manifest %s is not usable and is not published: %s", key, exc)
            return None, "the manifest is malformed or of an unsupported version"
        if (manifest.stage, manifest.domain, manifest.product, manifest.dagster_run_id) != (
            self._stage,
            ref.domain,
            ref.product,
            ref.dagster_run_id,
        ):
            log.warning("quality manifest %s describes another stage, product or run and is not published", key)
            return None, "the manifest describes another stage, product or run"
        return manifest, None

    def _read_state(self, ref: ManifestRef) -> RunSync | None:
        key = self._state_key(ref)
        body = self._get(key)
        if body is None:
            return None
        try:
            return RunSync.from_json(body)
        except QualityRecordError as exc:
            # A damaged state is rebuilt from the manifest: the catalog's own timestamp check keeps that safe.
            log.warning("quality sync state %s is unusable and is rebuilt: %s", key, exc)
            return None

    def _state_key(self, ref: ManifestRef) -> str:
        return self._key(STATE_PREFIX, ref.domain, ref.product, f"{ref.dagster_run_id}.json")

    def _key(self, *parts: str) -> str:
        return "/".join(p for p in (self._prefix, *parts) if p)

    def _get(self, key: str) -> str | None:
        try:
            return self._store.get_object(Bucket=self._bucket, Key=key)["Body"].read().decode()
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in _MISSING:
                return None
            raise StoreUnavailableError(f"{key} could not be read: {type(exc).__name__}") from exc

    def _put(self, key: str, body: str) -> None:
        full = key
        try:
            self._store.put_object(Bucket=self._bucket, Key=full, Body=body.encode(), ContentType="application/json")
        except Exception as exc:
            raise StoreUnavailableError(f"{full} could not be written: {type(exc).__name__}") from exc


def result_body(result: QualityResult, status: OmStatus, timestamp: int) -> dict[str, Any]:
    """The `CreateTestCaseResult` body. The original outcome rides in `result` and `testResultValue`, because the
    status cannot tell a WARN from an execution error. Counts and a pointer only: no row sample is ever sent."""
    values = [
        ("olf_outcome", result.outcome.value),
        ("engine_severity", result.engine_severity),
        ("pipeline_outcome", result.pipeline_outcome.value),
        ("dagster_run_id", result.dagster_run_id),
        (IDENTITY_VALUE, result.identity),
        *((f"metric.{name}", str(value)) for name, value in result.metrics.items()),
    ]
    if result.engine_run_id:
        values.append(("engine_run_id", result.engine_run_id))
    if result.artifact_uri:
        values.append(("report", result.artifact_uri))
    blocking = " and stopped the pipeline" if result.pipeline_outcome is PipelineOutcome.BLOCKED else ""
    return {
        "timestamp": timestamp,
        "testCaseStatus": status.value,
        "result": (
            f"{result.outcome.value.upper()} by {result.engine} policy {result.engine_severity}"
            f" in run {result.dagster_run_id} ({result.pipeline_outcome.value}{blocking})"
        ),
        "testResultValue": [{"name": name, "value": value} for name, value in values],
    }


def _needs_work(ref: ManifestRef, state: RunSync | None, now: datetime) -> bool:
    if state is None or state.manifest_etag != ref.etag:
        return True
    if state.rejected:
        return False
    return any(r.due(now) for r in state.results.values())


def _with_result(run: RunSync, update: ResultSync, now: datetime) -> RunSync:
    results = {**run.results, update.identity: update}
    if update.state is ResultState.SYNCED:
        return replace(run, results=results, last_success_at=now)
    if update.state is ResultState.SKIPPED:
        return replace(run, results=results)
    return replace(run, results=results, last_failure_at=now)


def _last_error(runs: list[RunSync]) -> str | None:
    epoch = datetime.min.replace(tzinfo=UTC)
    failing = sorted((r for r in runs if r.last_failure_at), key=lambda r: r.last_failure_at or epoch)
    for run in reversed(failing):
        for result in run.results.values():
            if result.reason and result.state in (ResultState.PENDING, ResultState.FAILED):
                return result.reason
    return None


def _describe(exc: OpenMetadataError | OpenMetadataTransientError) -> str:
    """The status, never the response text: the service's message can echo a request."""
    return f"HTTP {exc.status}" if exc.status else "no response"


def _path(fqn: str) -> str:
    return quote(fqn, safe="")


def _id(ref: ManifestRef) -> tuple[str, str, str]:
    return (ref.domain, ref.product, ref.dagster_run_id)


def _has_failed(state: RunSync | None) -> bool:
    return state is not None and state.count(ResultState.FAILED) > 0


def _scoped(schemas: Mapping[str, str] | None, database_fqn: str) -> dict[str, str]:
    """The mapping, refusing a schema that is not under this stage's database root."""
    scoped = dict(schemas or {})
    for fqn in scoped.values():
        if not fqn.startswith(f"{database_fqn}.") or fqn.count(".") != 2:
            raise ValueError(f"schema FQN {fqn!r} is not under {database_fqn!r}")
    return scoped
