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


def _etags_under(store: FileStore, bucket: str, prefix: str, *, cap: int | None = None) -> dict[str, str]:
    """Every key under `prefix` (the folder's own marker included) with its ETag, 422 past `cap` keys."""
    found: dict[str, str] = {}
    token: str | None = None
    while True:
        args: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "", "MaxKeys": MAX_KEYS}
        if token:
            args["ContinuationToken"] = token
        page = store.list_objects_v2(**args)
        found.update({entry["Key"]: str(entry.get("ETag", "")) for entry in page.get("Contents", [])})
        if cap is not None and len(found) > cap:
            raise HTTPException(status_code=422, detail=f"{prefix!r} holds more than {cap} keys")
        token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if not token:
            return found


def _delete_keys(
    store: FileStore, bucket: str, keys: list[str], etags: dict[str, str] | None = None
) -> tuple[list[str], list[DeleteError]]:
    """delete_objects in batches of 1000; what S3 reports per key is returned, not raised.

    A key with an ETag in `etags` is deleted only while it still has that ETag (`PreconditionFailed` otherwise).
    """
    pinned = etags or {}
    deleted: list[str] = []
    errors: list[DeleteError] = []
    for start in range(0, len(keys), MAX_KEYS):
        batch = keys[start : start + MAX_KEYS]
        objects = [{"Key": key, **({"ETag": pinned[key]} if pinned.get(key) else {})} for key in batch]
        result = store.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        failed = {entry["Key"]: str(entry.get("Code") or "error") for entry in result.get("Errors", [])}
        errors.extend(DeleteError(key=key, detail=detail) for key, detail in failed.items())
        deleted.extend(key for key in batch if key not in failed)
    return deleted, errors


# A move is pinned to what was looked at. The existence checks before it are a courtesy; the copy carries
# If-None-Match: * (the destination must still be absent) and x-amz-copy-source-if-match (the source must still be
# the object whose ETag was read), and the original is deleted only while it still has that ETag. SeaweedFS 4.48
# evaluates the copy's conditions under the destination's write lock (`weed/s3api/s3api_object_handlers_copy.go`) and
# a delete's If-Match, or a batch entry's ETag, under the source's (`s3api_object_handlers_delete.go`). What is left:
# the source can still be overwritten between the copy's check and its delete's check, which the delete then refuses
# (the move is rolled back, nothing is lost). A rollback deletes each copy only while it keeps the ETag the copy
# gave it, so a writer that replaced a copy keeps its write; the only window left is that delete's own check.
def _copy(store: FileStore, bucket: str, source: str, destination: str, source_etag: str) -> str:
    """Copy `source` (while it still has `source_etag`) to a new `destination`; the copy's own ETag."""
    try:
        result = store.copy_object(
            Bucket=bucket,
            Key=destination,
            CopySource={"Bucket": bucket, "Key": source},
            IfNoneMatch="*",
            CopySourceIfMatch=source_etag,
        )
    except ClientError as exc:
        if _error_code(exc) in _PRECONDITION_FAILED:
            raise HTTPException(
                status_code=409, detail=f"{destination!r} already exists or {source!r} changed; nothing was moved"
            ) from exc
        raise
    return str((result.get("CopyObjectResult") or {}).get("ETag", ""))


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
    except asyncio.CancelledError:
        # `audited` only closes the record on an Exception; a cancelled request would leave "attempted" alone. The
        # object is known to have landed when the final write marked the outcome, so say that, else say cancelled.
        landed = bool(outcome.result)
        closing = record(
            request,
            "succeeded" if landed else "failed",
            stage=stage,
            target=target,
            result=outcome.result if landed else None,
            status=None if landed else 499,
            detail="" if landed else "the request was cancelled",
        )
        audit: AuditLog = request.app.state.audit
        await _shielded(audit.write_best_effort, closing)
        raise
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
            await _stream_to_s3(request, store, bucket, key, length, outcome, overwrite=overwrite)
    return UploadedObject(key=key, size=length)


def _already_exists(key: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"{key!r} already exists; pass overwrite=true to replace it")


def _finished_by(outcome: Outcome, length: int, final_write: Callable[..., Any], **kwargs: Any) -> None:
    """Run the upload's last S3 call and, in the same worker thread, note that it landed.

    A request cancelled while that call is in flight cannot tell whether it returned; this marker can, so the
    cancelled upload's audit record says what happened to the object.
    """
    final_write(**kwargs)
    outcome.result["size"] = str(length)


async def _final_write(outcome: Outcome, length: int, call: Callable[..., Any], **kwargs: Any) -> None:
    """`_finished_by` on a worker thread; a cancelled request waits for the call to end before it unwinds."""
    task = asyncio.ensure_future(run_in_threadpool(_finished_by, outcome, length, call, **kwargs))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


async def _stream_to_s3(
    request: Request, store: FileStore, bucket: str, key: str, length: int, outcome: Outcome, *, overwrite: bool
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
                await _final_write(
                    outcome,
                    length,
                    store.complete_multipart_upload,
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    **conditions,
                )
            else:
                await _final_write(
                    outcome,
                    length,
                    store.put_object,
                    Bucket=bucket,
                    Key=key,
                    Body=bytes(buffer),
                    **conditions,
                )
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
    try:
        etag = str(store.head_object(Bucket=bucket, Key=source)["ETag"])
    except ClientError as exc:
        if _error_code(exc) in _NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"{source!r} does not exist") from exc
        raise
    if _exists(store, bucket, destination):
        raise HTTPException(status_code=409, detail=f"{destination!r} already exists")
    copy_etag = _copy(store, bucket, source, destination, etag)
    try:
        store.delete_object(Bucket=bucket, Key=source, IfMatch=etag)
    except ClientError as exc:
        if _error_code(exc) not in _PRECONDITION_FAILED:
            raise
        # Replaced after the copy: the original is the newer file, so it stays and the stale copy goes, unless
        # someone has replaced that copy too, in which case it is theirs and stays.
        _remove_copies(store, bucket, {destination: copy_etag}, destination)
        raise HTTPException(status_code=409, detail=f"{source!r} changed during the move; nothing was moved") from exc
    return 1


def _remove_copies(store: FileStore, bucket: str, copies: dict[str, str], where: str) -> list[str]:
    """Delete the copies a failed move made, each only while it still has the ETag the copy gave it.

    A copy that has changed since belongs to another writer now: it is left alone, and returned.
    """
    try:
        _deleted, errors = _delete_keys(store, bucket, list(copies), copies)
    except Exception as exc:
        log.warning("the copies under %s/%s were not removed after a failed move: %s", bucket, where, exc)
        return []
    left = [error.key for error in errors if error.detail in _PRECONDITION_FAILED]
    if left:
        log.warning("%d keys under %s/%s were replaced by someone else and left in place", len(left), bucket, where)
    return left


def _move_folder(store: FileStore, bucket: str, source: str, destination: str) -> int:
    etags = _etags_under(store, bucket, source, cap=MAX_KEYS)
    if not etags:
        raise HTTPException(status_code=404, detail=f"{source!r} does not exist")
    if _any_under(store, bucket, destination):
        raise HTTPException(status_code=409, detail=f"{destination!r} already exists")
    copied: dict[str, str] = {}
    try:
        for key, etag in etags.items():
            new_key = destination + key[len(source) :]
            copied[new_key] = _copy(store, bucket, key, new_key, etag)
    except Exception:
        # The originals are untouched; remove the copies so the destination is not left half-made.
        _remove_copies(store, bucket, copied, destination)
        raise
    _deleted, errors = _delete_keys(store, bucket, list(etags), etags)
    changed = [error.key for error in errors if error.detail in _PRECONDITION_FAILED]
    if changed:
        # Replaced after the copy: those originals are newer and stay, with their stale copies removed.
        stale = {destination + key[len(source) :]: copied[destination + key[len(source) :]] for key in changed}
        _remove_copies(store, bucket, stale, destination)
        raise HTTPException(
            status_code=409,
            detail=f"{len(changed)} keys changed during the move and stay in {source!r}; the rest moved",
        )
    if errors:
        log.warning("%d originals under %s/%s were not deleted after a move", len(errors), bucket, source)
        raise HTTPException(
            status_code=502, detail=f"moved, but {len(errors)} original keys could not be deleted; remove them by hand"
        )
    return len(etags)


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
                    for found in _etags_under(store, bucket, key, cap=MAX_KEYS):
                        expanded[found] = None
                else:
                    expanded[key] = None
                if len(expanded) > MAX_KEYS:
                    raise HTTPException(status_code=422, detail=f"the delete covers more than {MAX_KEYS} keys")
            deleted, errors = _delete_keys(store, bucket, list(expanded))
        outcome.result["deleted"] = str(len(deleted))
        outcome.result["errors"] = str(len(errors))
    return Deleted(deleted=deleted, errors=errors)
