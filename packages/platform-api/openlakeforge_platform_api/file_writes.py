"""The files explorer's write routes, each run inside `audit.audited`.

The object store's `Write` action also allows deleting, so the API keeps the audit prefix read-only itself: a write
that touches it is refused with 403 before anything is attempted (`touches_audit_prefix`). The refusal is recorded.

The upload handler is async so the body is streamed; every call it makes to the audit log or the object store is
synchronous boto3, so each one runs on a worker thread and the event loop keeps serving other requests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from botocore.exceptions import ClientError
from fastapi import HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from openlakeforge_platform_api.audit import AuditLog, Outcome, audited, record
from openlakeforge_platform_api.files import (
    FileStore,
    check_key,
    files_store,
    require_bucket,
    s3_errors,
    touches_audit_prefix,
)
from openlakeforge_platform_api.models import UploadedObject

log = logging.getLogger(__name__)

# S3 refuses a multipart part under 5 MiB (the last excepted); a body no larger than one part goes up in one call.
PART_BYTES = 8 * 1024 * 1024
AUDIT_READ_ONLY = "the audit records are read-only"
_NOT_FOUND = ("NoSuchKey", "404", "NotFound")
_PRECONDITION_FAILED = ("PreconditionFailed", "412", "ConditionalRequestConflict")


def _guard_audit_prefix(request: Request, stage: str, bucket: str, keys: list[str], target: dict[str, str]) -> None:
    """403, and a stored `refused` record, when any key reaches the audit records."""
    if any(touches_audit_prefix(request.app.state.settings, bucket, key) for key in keys):
        audit: AuditLog = request.app.state.audit
        audit.write_best_effort(
            record(request, "refused", stage=stage, target=target, status=403, detail=AUDIT_READ_ONLY)
        )
        raise HTTPException(status_code=403, detail=AUDIT_READ_ONLY)


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _exists(store: FileStore, bucket: str, key: str) -> bool:
    try:
        store.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _error_code(exc) in _NOT_FOUND:
            return False
        raise
    return True


async def _shielded(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """`function` on a worker thread that a cancelled request still lets finish: cleanup must not be skipped."""
    return await asyncio.shield(asyncio.ensure_future(run_in_threadpool(function, *args, **kwargs)))


@asynccontextmanager
async def _audited_off_the_loop(request: Request, *, stage: str, target: dict[str, str]) -> AsyncIterator[Outcome]:
    """`audited`, whose record writes are synchronous S3 calls, with each of them on a worker thread."""
    manager = audited(request, stage=stage, target=target)
    outcome = await run_in_threadpool(manager.__enter__)
    try:
        yield outcome
    except BaseException as exc:
        if not await _shielded(manager.__exit__, type(exc), exc, exc.__traceback__):
            raise
    else:
        await _shielded(manager.__exit__, None, None, None)


async def upload_object(
    request: Request,
    stage: str,
    bucket: str,
    key: str = Query(),  # noqa: B008 - FastAPI's dependency API
    overwrite: bool = Query(default=False),  # noqa: B008
) -> UploadedObject:
    """Store the request body as `key`, streamed to S3 (multipart above one part) and never held whole."""
    store = files_store(request)
    require_bucket(request, stage, bucket)
    check_key(key)
    if key.endswith("/"):
        raise HTTPException(status_code=422, detail="an object key must not end with '/'; create a folder instead")
    target = {"bucket": bucket, "key": key}
    await run_in_threadpool(_guard_audit_prefix, request, stage, bucket, [key], target)
    declared = request.headers.get("content-length")
    if declared is None:
        raise HTTPException(status_code=411, detail="the upload needs a Content-Length")
    if not declared.isdigit():
        raise HTTPException(status_code=400, detail="the Content-Length is not a number")
    length = int(declared)
    maximum: int = request.app.state.settings.files_max_upload_bytes
    if length > maximum:
        raise HTTPException(status_code=413, detail=f"an upload is at most {maximum} bytes")
    async with _audited_off_the_loop(request, stage=stage, target=target) as outcome:
        with s3_errors():
            if not overwrite and await run_in_threadpool(_exists, store, bucket, key):
                raise _already_exists(key)
            await _stream_to_s3(request, store, bucket, key, length, overwrite=overwrite)
        outcome.result["size"] = str(length)
    return UploadedObject(key=key, size=length)


def _already_exists(key: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"{key!r} already exists; pass overwrite=true to replace it")


async def _stream_to_s3(
    request: Request, store: FileStore, bucket: str, key: str, length: int, *, overwrite: bool
) -> None:
    # The check above is only a courtesy: another upload can land between it and the final write. Without
    # overwrite, the final write itself (PutObject, or CompleteMultipartUpload) carries If-None-Match: *, which
    # SeaweedFS evaluates under the object's write lock, so exactly one of two racing uploads wins.
    conditions = {} if overwrite else {"IfNoneMatch": "*"}
    received = 0
    buffer = bytearray()
    multipart = length > PART_BYTES
    upload_id: str | None = None
    parts: list[dict[str, Any]] = []

    async def send_part() -> None:
        assert upload_id is not None
        number = len(parts) + 1
        body = bytes(buffer[:PART_BYTES])
        del buffer[:PART_BYTES]
        result = await run_in_threadpool(
            store.upload_part, Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=body
        )
        parts.append({"ETag": result["ETag"], "PartNumber": number})

    try:
        if multipart:
            created = await run_in_threadpool(store.create_multipart_upload, Bucket=bucket, Key=key)
            upload_id = created["UploadId"]
        async for chunk in request.stream():
            received += len(chunk)
            if received > length:
                raise HTTPException(status_code=400, detail="the body is longer than its Content-Length")
            buffer += chunk
            while multipart and len(buffer) >= PART_BYTES:
                await send_part()
        if received != length:
            raise HTTPException(status_code=400, detail="the body is shorter than its Content-Length")
        try:
            if upload_id is not None:
                if buffer:
                    await send_part()
                await run_in_threadpool(
                    store.complete_multipart_upload,
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    **conditions,
                )
            else:
                await run_in_threadpool(store.put_object, Bucket=bucket, Key=key, Body=bytes(buffer), **conditions)
        except ClientError as exc:
            if _error_code(exc) in _PRECONDITION_FAILED:
                raise _already_exists(key) from exc
            raise
    except BaseException:
        # Any failure, a client that went away included, leaves no half-uploaded parts behind.
        if upload_id is not None:
            try:
                await _shielded(store.abort_multipart_upload, Bucket=bucket, Key=key, UploadId=upload_id)
            except Exception as exc:
                log.warning("multipart upload %s of %s/%s was not aborted: %s", upload_id, bucket, key, exc)
        raise
