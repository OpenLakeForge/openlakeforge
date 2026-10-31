"""Audit records for operations that change state (ADR 0021, decision 3).

Every record is one JSON line on stdout. A record about an authenticated
person's state-changing request is also written to the object store, one
object per record, with a write-only key. That write is the durable one:

- before the API acts, it writes an `attempted` record, and if that write
  fails it answers 503 and does not act (fail closed);
- after it acts, it writes the outcome (`succeeded` or `failed`). The action
  has happened by then, so a failed outcome write is logged, not returned;
- a refusal of an authenticated person (403) is written too. A refusal of a
  caller with no valid token (401) goes to stdout only, so an anonymous caller
  cannot fill the bucket.

The request id in each object key is generated here, never taken from the
client. The key cannot read, list or delete what it wrote, but SeaweedFS's
`Write` action also allows overwriting and deleting an object, so the records
are tamper-evident against everyone except the API itself.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from openlakeforge_platform_api.tokens import Principal

log = logging.getLogger(__name__)

SCHEMA = "openlakeforge.platform_api_audit.v1"

Phase = Literal["attempted", "refused", "succeeded", "failed"]


class ObjectWriter(Protocol):
    """The one object-store call the audit sink makes; boto3's S3 client satisfies it."""

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> Any: ...  # noqa: N803


class AuditUnavailable(Exception):
    """The durable record could not be written; a state-changing request is answered 503."""


@dataclass(frozen=True)
class AuditRecord:
    request_id: str
    phase: Phase
    operation: str
    stage: str | None
    # Who: the validated token's subject, email and canonical role; all None when no valid token was presented.
    subject: str | None
    email: str | None
    role: str | None
    # What the operation acts on, e.g. {"job": "..."}, and what it produced, e.g. {"run_id": "..."}.
    target: Mapping[str, str] = field(default_factory=dict)
    result: Mapping[str, str] = field(default_factory=dict)
    status: int | None = None
    detail: str = ""
    time: str = ""

    def document(self) -> dict[str, Any]:
        return {"schema": SCHEMA, **asdict(self)}


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class AuditLog:
    """Writes records to stdout and, when `durable`, to `<base_uri>date=YYYY-MM-DD/<time>-<request id>-<phase>.json`."""

    def __init__(
        self,
        store: ObjectWriter | None,
        base_uri: str | None,
        *,
        emit: Callable[[str], None] = _stdout,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._emit = emit
        self._clock = clock
        self._bucket = self._prefix = ""
        if base_uri:
            parts = urlsplit(base_uri)
            self._bucket, self._prefix = parts.netloc, parts.path.lstrip("/")

    @property
    def durable(self) -> bool:
        """Whether a durable record can be attempted at all: an object store and a place in it."""
        return self._store is not None and bool(self._bucket)

    def write(self, record: AuditRecord, *, durable: bool) -> AuditRecord:
        """Emit `record` on stdout, then store it when `durable`; raises AuditUnavailable if that fails."""
        now = self._clock()
        record = AuditRecord(**{**asdict(record), "time": now.isoformat()})
        body = json.dumps(record.document(), sort_keys=True)
        self._emit(body)
        if not durable:
            return record
        if self._store is None or not self._bucket:
            raise AuditUnavailable("the platform API has no audit sink configured")
        key = f"{self._prefix}date={now:%Y-%m-%d}/{now:%H%M%S%f}-{record.request_id}-{record.phase}.json"
        try:
            self._store.put_object(Bucket=self._bucket, Key=key, Body=body.encode(), ContentType="application/json")
        except Exception as exc:  # any store failure, whatever its type, means there is no durable record
            # The store's message can name the endpoint or bucket; it goes to the log, not the caller.
            log.error("audit record %s/%s was not stored: %s", record.request_id, record.phase, exc)
            raise AuditUnavailable("the audit record could not be stored") from exc
        return record

    def write_best_effort(self, record: AuditRecord) -> None:
        """A durable write whose failure must not change the response: an outcome, or a refusal."""
        try:
            self.write(record, durable=True)
        except AuditUnavailable:
            # write() has already emitted the record on stdout and logged the failure.
            pass


@dataclass
class Outcome:
    """What the operation produced, filled in by its body for the `succeeded` record."""

    result: dict[str, str] = field(default_factory=dict)


def record(
    request: Request,
    phase: Phase,
    *,
    stage: str | None,
    target: dict[str, str],
    result: dict[str, str] | None = None,
    status: int | None = None,
    detail: str = "",
) -> AuditRecord:
    """A record of this request: its operation, the API's own request id, and the validated principal if any."""
    principal: Principal | None = getattr(request.state, "principal", None)
    route = request.scope.get("route")
    return AuditRecord(
        request_id=request.state.request_id,
        phase=phase,
        operation=getattr(route, "name", ""),
        stage=stage,
        subject=principal.subject if principal else None,
        email=principal.email if principal else None,
        role=principal.role if principal else None,
        target=target,
        result=result or {},
        status=status,
        detail=detail,
    )


@contextmanager
def audited(request: Request, *, stage: str | None, target: dict[str, str]) -> Iterator[Outcome]:
    """Run an operation's body on the record: the attempt is stored first, or nothing runs (503).

    The body sets `outcome.result` to what it produced. How it ended, succeeded
    or failed with the status it answered, is recorded after it.
    """
    audit: AuditLog = request.app.state.audit
    try:
        audit.write(record(request, "attempted", stage=stage, target=target), durable=True)
    except AuditUnavailable as exc:
        raise HTTPException(status_code=503, detail="the audit record could not be stored; nothing was done") from exc
    outcome = Outcome()
    try:
        yield outcome
    except HTTPException as exc:
        audit.write_best_effort(
            record(request, "failed", stage=stage, target=target, status=exc.status_code, detail=str(exc.detail))
        )
        raise
    except Exception as exc:
        audit.write_best_effort(
            record(request, "failed", stage=stage, target=target, status=500, detail=type(exc).__name__)
        )
        raise
    audit.write_best_effort(record(request, "succeeded", stage=stage, target=target, result=outcome.result))
