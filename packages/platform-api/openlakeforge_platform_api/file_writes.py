"""The files explorer's write routes: upload, create folder, rename and delete, each run inside `audit.audited`.

The object store's `Write` action also allows deleting, so the API keeps the audit prefix read-only itself: a write
that touches it is refused with 403 before anything is attempted (`touches_audit_prefix`). The refusal is recorded.
Nothing here is atomic across keys; a folder rename copies everything first and deletes the originals last, and a
failed copy removes the copies already made.

The upload handler is async so the body is streamed; every call it makes to the audit log or the object store is
synchronous boto3, so each one runs on a worker thread and the event loop keeps serving other requests. The other
handlers are plain functions, which FastAPI already runs on a worker thread.
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
    check_prefix,
    files_store,
    require_bucket,
    s3_errors,
    touches_audit_prefix,
)
from openlakeforge_platform_api.models import (
    CreatedFolder,
    CreateFolderRequest,
    Deleted,
    DeleteError,
    DeleteRequest,
    Renamed,
    RenameRequest,
    UploadedObject,
)

log = logging.getLogger(__name__)

# S3 refuses a multipart part under 5 MiB (the last excepted); a body no larger than one part goes up in one call.
PART_BYTES = 8 * 1024 * 1024
MAX_KEYS = 1000
AUDIT_READ_ONLY = "the audit records are read-only"
_NOT_FOUND = ("NoSuchKey", "404", "NotFound")
_PRECONDITION_FAILED = ("PreconditionFailed", "412", "ConditionalRequestConflict")
_SHOWN_KEYS = 3
_MAX_TARGET_CHARS = 512


def _keys_text(keys: list[str]) -> str:
    """A compact description of `keys` for the audit target: the first few, then a count."""
    text = ", ".join(keys[:_SHOWN_KEYS])
    if len(keys) > _SHOWN_KEYS:
        text += f" (+{len(keys) - _SHOWN_KEYS} more)"
    return text[:_MAX_TARGET_CHARS]


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


def _any_under(store: FileStore, bucket: str, prefix: str) -> bool:
    page = store.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="", MaxKeys=1)
    return bool(page.get("Contents"))


def _keys_under(store: FileStore, bucket: str, prefix: str, *, cap: int | None = None) -> list[str]:
    """Every key under `prefix` (the folder's own marker included), 422 once there are more than `cap`."""
    keys: list[str] = []
    token: str | None = None
    while True:
        args: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "", "MaxKeys": MAX_KEYS}
        if token:
            args["ContinuationToken"] = token
        page = store.list_objects_v2(**args)
        keys.extend(entry["Key"] for entry in page.get("Contents", []))
        if cap is not None and len(keys) > cap:
            raise HTTPException(status_code=422, detail=f"{prefix!r} holds more than {cap} keys")
        token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if not token:
            return keys


def _delete_keys(store: FileStore, bucket: str, keys: list[str]) -> tuple[list[str], list[DeleteError]]:
    """delete_objects in batches of 1000; what S3 reports per key is returned, not raised."""
    deleted: list[str] = []
    errors: list[DeleteError] = []
    for start in range(0, len(keys), MAX_KEYS):
        batch = keys[start : start + MAX_KEYS]
        result = store.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch]})
        failed = {entry["Key"]: str(entry.get("Code") or "error") for entry in result.get("Errors", [])}
        errors.extend(DeleteError(key=key, detail=detail) for key, detail in failed.items())
        deleted.extend(key for key in batch if key not in failed)
    return deleted, errors


def _copy(store: FileStore, bucket: str, source: str, destination: str) -> None:
    store.copy_object(Bucket=bucket, Key=destination, CopySource={"Bucket": bucket, "Key": source})


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


def create_folder(request: Request, stage: str, bucket: str, body: CreateFolderRequest) -> CreatedFolder:
    """Write the zero-byte marker a folder is; 409 when anything is already at or under the prefix."""
    store = files_store(request)
    require_bucket(request, stage, bucket)
    prefix = check_prefix(body.prefix)
    if not prefix:
        raise HTTPException(status_code=422, detail="a folder prefix must not be empty")
    target = {"bucket": bucket, "prefix": prefix}
    _guard_audit_prefix(request, stage, bucket, [prefix], target)
    with audited(request, stage=stage, target=target):
        with s3_errors():
            if _any_under(store, bucket, prefix):
                raise HTTPException(status_code=409, detail=f"{prefix!r} already exists")
            store.put_object(Bucket=bucket, Key=prefix, Body=b"")
    return CreatedFolder(prefix=prefix)


def rename_object(request: Request, stage: str, bucket: str, body: RenameRequest) -> Renamed:
    """Move a file, or every key under a folder, by copying and then deleting; never over something existing."""
    store = files_store(request)
    require_bucket(request, stage, bucket)
    source, destination = check_key(body.source), check_key(body.target)
    folder = source.endswith("/")
    if folder != destination.endswith("/"):
        raise HTTPException(status_code=422, detail="rename a file to a file or a folder to a folder")
    if source == destination:
        raise HTTPException(status_code=422, detail="the source and the destination are the same")
    if folder and destination.startswith(source):
        raise HTTPException(status_code=422, detail="a folder cannot be moved into itself")
    target = {"bucket": bucket, "from": source, "to": destination}
    _guard_audit_prefix(request, stage, bucket, [source, destination], target)
    with audited(request, stage=stage, target=target) as outcome:
        with s3_errors():
            moved = (
                _move_folder(store, bucket, source, destination)
                if folder
                else _move_file(store, bucket, source, destination)
            )
        outcome.result["moved"] = str(moved)
    return Renamed(moved=moved)


def _move_file(store: FileStore, bucket: str, source: str, destination: str) -> int:
    if not _exists(store, bucket, source):
        raise HTTPException(status_code=404, detail=f"{source!r} does not exist")
    if _exists(store, bucket, destination):
        raise HTTPException(status_code=409, detail=f"{destination!r} already exists")
    _copy(store, bucket, source, destination)
    store.delete_object(Bucket=bucket, Key=source)
    return 1


def _move_folder(store: FileStore, bucket: str, source: str, destination: str) -> int:
    keys = _keys_under(store, bucket, source, cap=MAX_KEYS)
    if not keys:
        raise HTTPException(status_code=404, detail=f"{source!r} does not exist")
    if _any_under(store, bucket, destination):
        raise HTTPException(status_code=409, detail=f"{destination!r} already exists")
    copied: list[str] = []
    try:
        for key in keys:
            new_key = destination + key[len(source) :]
            _copy(store, bucket, key, new_key)
            copied.append(new_key)
    except Exception:
        # The originals are untouched; remove the copies so the destination is not left half-made.
        try:
            _delete_keys(store, bucket, copied)
        except Exception as exc:
            log.warning("the copies under %s/%s were not removed after a failed move: %s", bucket, destination, exc)
        raise
    _deleted, errors = _delete_keys(store, bucket, keys)
    if errors:
        log.warning("%d originals under %s/%s were not deleted after a move", len(errors), bucket, source)
        raise HTTPException(
            status_code=502, detail=f"moved, but {len(errors)} original keys could not be deleted; remove them by hand"
        )
    return len(keys)


def delete_objects(request: Request, stage: str, bucket: str, body: DeleteRequest) -> Deleted:
    """Delete the keys; one ending in `/` takes everything under it. Per-key failures are reported, not raised."""
    store = files_store(request)
    require_bucket(request, stage, bucket)
    keys = [check_key(key) for key in body.keys]
    target = {"bucket": bucket, "keys": _keys_text(keys)}
    _guard_audit_prefix(request, stage, bucket, keys, target)
    with audited(request, stage=stage, target=target) as outcome:
        with s3_errors():
            expanded: dict[str, None] = {}
            for key in keys:
                if key.endswith("/"):
                    expanded[key] = None
                    for found in _keys_under(store, bucket, key, cap=MAX_KEYS):
                        expanded[found] = None
                else:
                    expanded[key] = None
                if len(expanded) > MAX_KEYS:
                    raise HTTPException(status_code=422, detail=f"the delete covers more than {MAX_KEYS} keys")
            deleted, errors = _delete_keys(store, bucket, list(expanded))
        outcome.result["deleted"] = str(len(deleted))
        outcome.result["errors"] = str(len(errors))
    return Deleted(deleted=deleted, errors=errors)
