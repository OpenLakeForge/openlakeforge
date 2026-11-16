"""The files explorer's contract with the object store: the S3 calls its routes make, behind one client."""

from __future__ import annotations

from typing import Any, Protocol


class FileStore(Protocol):
    """The S3 calls the files routes make; boto3's S3 client satisfies it."""

    def list_buckets(self) -> Any: ...
    def list_objects_v2(  # noqa: N803 - boto3's names
        self, *, Bucket: str, Prefix: str, Delimiter: str, MaxKeys: int, ContinuationToken: str = ...
    ) -> Any: ...

    def get_object(self, *, Bucket: str, Key: str) -> Any: ...  # noqa: N803
