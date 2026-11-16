"""The files explorer's read routes: a stage's lakehouse buckets, listed and downloaded with the API's own key.

The object store cannot take a person's token, so the permission matrix rows for these routes are the only control
on who reads what. The bucket and the key are validated here, once, for these routes and for the write routes that
follow (`require_bucket`, `check_key`, `touches_audit_prefix`, `s3_errors`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from openlakeforge_platform_api.models import (
    Bucket,
    Buckets,
    FileCapabilities,
    FileObject,
    FileStageCapability,
    Folder,
    Objects,
)
from openlakeforge_platform_api.permissions import PermissionMatrix
from openlakeforge_platform_api.settings import Settings

log = logging.getLogger(__name__)

# The operations a client may be offered, in display order. A name with no matrix row is never reported.
FILE_OPERATIONS = (
    "list_buckets",
    "list_objects",
    "download_object",
    "upload_object",
    "create_folder",
    "rename_object",
    "delete_objects",
)
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
MAX_KEY_BYTES = 1024
CHUNK_BYTES = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class FileStore(Protocol):
    """The S3 calls the files routes make; boto3's S3 client satisfies it."""

    def list_buckets(self) -> Any: ...
    def list_objects_v2(  # noqa: N803 - boto3's names
        self, *, Bucket: str, Prefix: str, Delimiter: str, MaxKeys: int, ContinuationToken: str = ...
    ) -> Any: ...

    def get_object(self, *, Bucket: str, Key: str) -> Any: ...  # noqa: N803

    # The upload route's calls.
    def head_object(self, *, Bucket: str, Key: str) -> Any: ...  # noqa: N803
    def put_object(  # noqa: N803
        self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str = ...
    ) -> Any: ...
    def create_multipart_upload(self, *, Bucket: str, Key: str) -> Any: ...  # noqa: N803
    def upload_part(  # noqa: N803
        self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes
    ) -> Any: ...
    def complete_multipart_upload(  # noqa: N803
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, Any],
        IfNoneMatch: str = ...,
    ) -> Any: ...
    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> Any: ...  # noqa: N803

    # The folder, rename and delete routes' calls.
    def copy_object(  # noqa: N803
        self,
        *,
        Bucket: str,
        Key: str,
        CopySource: dict[str, str],
        IfNoneMatch: str = ...,
        CopySourceIfMatch: str = ...,
    ) -> Any: ...
    def delete_object(self, *, Bucket: str, Key: str, IfMatch: str = ...) -> Any: ...  # noqa: N803
    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> Any: ...  # noqa: N803


def files_store(request: Request) -> FileStore:
    store: FileStore | None = request.app.state.files_store
    if store is None:
        raise HTTPException(status_code=503, detail="the files store is not configured")
    return store


def _role(request: Request) -> str | None:
    role: str | None = request.state.principal.role
    return role


def require_bucket(request: Request, stage: str, bucket: str) -> None:
    """404 unless `bucket` is one of `stage`'s buckets, or the ops bucket and the caller is the top role."""
    settings: Settings = request.app.state.settings
    matrix: PermissionMatrix = request.app.state.permissions
    if bucket in settings.file_buckets.get(stage, ()):
        return
    if bucket == settings.ops_bucket and _role(request) == matrix.precedence[0]:
        return
    raise HTTPException(status_code=404, detail=f"bucket {bucket!r} is not in stage {stage!r}")


def check_key(key: str) -> str:
    """`key` when it is a usable, canonical object key; otherwise 422. A single trailing `/` (a folder) is allowed.

    The object store normalises a key with a backslash, an empty segment or a `.`/`..` segment into a different key
    (SeaweedFS turns `audit//x` into `audit/x`), so such a key is refused rather than checked as written: every
    check here and in `touches_audit_prefix` then speaks about the key the store will act on.
    """
    if not key:
        raise HTTPException(status_code=422, detail="the key is empty")
    if key.startswith("/"):
        raise HTTPException(status_code=422, detail="the key must not start with '/'")
    if "\\" in key:
        raise HTTPException(status_code=422, detail="the key must not contain a backslash")
    segments = (key[:-1] if key.endswith("/") else key).split("/")
    if "" in segments:
        raise HTTPException(status_code=422, detail="the key must not contain an empty segment ('//')")
    if "." in segments or ".." in segments:
        raise HTTPException(status_code=422, detail="the key must not contain a '.' or '..' segment")
    if _CONTROL.search(key):
        raise HTTPException(status_code=422, detail="the key must not contain control characters")
    try:
        size = len(key.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=422, detail="the key is not valid UTF-8") from exc
    if size > MAX_KEY_BYTES:
        raise HTTPException(status_code=422, detail=f"the key is longer than {MAX_KEY_BYTES} bytes")
    return key


def check_prefix(prefix: str) -> str:
    """`prefix` when it is empty or a valid key ending in `/`; otherwise 422."""
    if prefix == "":
        return prefix
    if not prefix.endswith("/"):
        raise HTTPException(status_code=422, detail="a prefix must be empty or end with '/'")
    return check_key(prefix)


def touches_audit_prefix(settings: Settings, bucket: str, key: str) -> bool:
    """Whether a write to `key` (or, for a key ending in `/`, a prefix) in `bucket` reaches the audit records.

    The files key can write and delete anywhere in the ops bucket, so the API itself keeps the audit prefix
    read-only: a key under it, or a folder that contains it, is refused by the write routes (403). Reads are fine.
    """
    if not settings.audit_base_uri:
        return False
    parts = urlsplit(settings.audit_base_uri)
    audit_bucket, audit_prefix = parts.netloc, _canonical(parts.path, folder=True)[0]
    if bucket != audit_bucket:
        return False
    path, folder = _canonical(key)
    return path.startswith(audit_prefix) or (folder and audit_prefix.startswith(path))


def _canonical(key: str, *, folder: bool = False) -> tuple[str, bool]:
    """The key as the store resolves it, and whether it names a folder: `\\` is `/`, empty and `.` segments vanish."""
    segments = key.replace("\\", "/").split("/")
    folder = folder or key.endswith(("/", "\\")) or segments[-1] in (".", "..")
    resolved: list[str] = []
    for segment in segments:
        if segment in ("", "."):
            continue
        if segment == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(segment)
    path = "/".join(resolved)
    return (path + "/" if path and folder else path), folder


@contextmanager
def s3_errors() -> Iterator[None]:
    """Maps what boto3 raises to the status a client can act on. The store's own message goes to the log only."""
    try:
        yield
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        log.warning("the files store answered %s: %s", code or "an error", exc)
        if code in ("NoSuchKey", "404", "NotFound"):
            raise HTTPException(status_code=404, detail="the object does not exist") from exc
        if code == "NoSuchBucket":
            raise HTTPException(status_code=404, detail="the bucket does not exist") from exc
        if code in ("AccessDenied", "403"):
            raise HTTPException(status_code=502, detail="the object store denied the platform API's files key") from exc
        raise HTTPException(status_code=502, detail="the object store answered with an error") from exc
    except BotoCoreError as exc:
        log.warning("the files store is unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="the object store is unreachable") from exc


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def file_capabilities(request: Request) -> FileCapabilities:
    settings: Settings = request.app.state.settings
    matrix: PermissionMatrix = request.app.state.permissions
    role = _role(request)
    stages = [
        FileStageCapability(
            stage=stage,
            operations=[name for name in FILE_OPERATIONS if matrix.allows(name, role, stage)],
        )
        for stage in settings.stages
        if stage in settings.file_buckets and matrix.allows("list_buckets", role, stage)
    ]
    return FileCapabilities(stages=stages, max_upload_bytes=settings.files_max_upload_bytes)


def list_buckets(request: Request, stage: str) -> Buckets:
    """The stage's buckets, plus the ops bucket for a platform-admin."""
    settings: Settings = request.app.state.settings
    matrix: PermissionMatrix = request.app.state.permissions
    store = files_store(request)
    names = list(settings.file_buckets.get(stage, ()))
    if settings.ops_bucket and _role(request) == matrix.precedence[0] and settings.ops_bucket not in names:
        names.append(settings.ops_bucket)
    created: dict[str, str | None] = {}
    try:
        with s3_errors():
            for entry in store.list_buckets().get("Buckets", []):
                created[str(entry.get("Name"))] = _iso(entry.get("CreationDate"))
    except HTTPException as exc:
        # Listing every bucket may be denied to the files key; the names are known without it.
        if exc.status_code == 503:
            raise
    return Buckets(buckets=[Bucket(name=name, created_at=created.get(name)) for name in names])


def list_objects(
    request: Request,
    stage: str,
    bucket: str,
    prefix: str = "",
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),  # noqa: B008 - FastAPI's dependency API
) -> Objects:
    """One page of the folders and files directly under `prefix`."""
    store = files_store(request)
    require_bucket(request, stage, bucket)
    check_prefix(prefix)
    args: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": limit}
    if cursor:
        args["ContinuationToken"] = cursor
    with s3_errors():
        page = store.list_objects_v2(**args)
    folders = [Folder(prefix=entry["Prefix"]) for entry in page.get("CommonPrefixes", [])]
    # The zero-byte marker a created folder leaves is the folder itself, not a file in it.
    files = [
        FileObject(key=entry["Key"], size=int(entry["Size"]), last_modified=_iso(entry["LastModified"]) or "")
        for entry in page.get("Contents", [])
        if entry["Key"] != prefix
    ]
    return Objects(
        prefix=prefix,
        folders=folders,
        files=files,
        next_cursor=page.get("NextContinuationToken") if page.get("IsTruncated") else None,
    )


def _content_disposition(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    fallback = "".join(char if 32 <= ord(char) < 127 and char not in '"\\;%' else "_" for char in name) or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _chunks(body: Any) -> Iterator[bytes]:
    try:
        yield from body.iter_chunks(CHUNK_BYTES)
    finally:
        body.close()


def download_object(request: Request, stage: str, bucket: str, key: str = Query()) -> StreamingResponse:  # noqa: B008
    """The object's bytes, streamed in chunks, never read whole."""
    store = files_store(request)
    require_bucket(request, stage, bucket)
    check_key(key)
    with s3_errors():
        result = store.get_object(Bucket=bucket, Key=key)
    headers = {"Content-Disposition": _content_disposition(key)}
    length = result.get("ContentLength")
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(
        _chunks(result["Body"]),
        media_type=result.get("ContentType") or "application/octet-stream",
        headers=headers,
    )
