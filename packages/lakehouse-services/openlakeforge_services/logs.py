"""Read the Kubernetes log archive that `libs/k8s_log_archive.py` writes.

The archive is gzipped NDJSON under
`<base>/k8s/namespace=<ns>/date=<YYYY-MM-DD>/hour=<HH>/k8s-logs-<stamp>.ndjson.gz`,
one object per archiver run. The caller passes an S3 client: this package
holds no object-store credential and does not depend on boto3 (ADR 0021,
decision 4).
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlparse

# The writer's record schema; a record of another schema is skipped, not misread.
SCHEMA = "openlakeforge.k8s_log.v1"


class ObjectStore(Protocol):
    """The two calls of a boto3 S3 client the reader makes, with the arguments it passes."""

    def list_objects_v2(  # noqa: N803 - boto3's argument names
        self, *, Bucket: str, Prefix: str = ..., ContinuationToken: str = ...
    ) -> Mapping[str, Any]: ...

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...  # noqa: N803


@dataclass(frozen=True)
class ArchivedLogLine:
    archived_at: str
    namespace: str
    pod: str
    container: str
    # The log line with its Kubernetes timestamp, or why the archiver could not read the container.
    line: str | None = None
    error: str | None = None


class LogArchive:
    def __init__(self, store: ObjectStore, base_uri: str) -> None:
        parsed = urlparse(base_uri)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError(f"expected an s3:// log base URI, got {base_uri!r}")
        self._store = store
        self._bucket = parsed.netloc
        self._prefix = parsed.path.strip("/")

    def read(
        self,
        namespace: str,
        day: date,
        *,
        hour: int | None = None,
        pod: str = "",
        container: str = "",
        limit: int = 1000,
    ) -> list[ArchivedLogLine]:
        """Archived lines of one namespace for a day, or one hour of it, oldest archive first, at most `limit`."""
        # A slash would move the listing to another partition, such as another stage's namespace.
        if not namespace or "/" in namespace:
            raise ValueError(f"not a namespace: {namespace!r}")
        if hour is not None and not 0 <= hour <= 23:
            raise ValueError(f"not an hour of the day: {hour}")
        partition = [self._prefix, "k8s", f"namespace={namespace}", f"date={day.isoformat()}"]
        if hour is not None:
            partition.append(f"hour={hour:02d}")
        prefix = "/".join(part for part in partition if part) + "/"
        lines: list[ArchivedLogLine] = []
        # The object names carry the archive time to the second, so key order is archive order.
        for key in sorted(self._keys(prefix)):
            for line in self._lines(key):
                if (pod and line.pod != pod) or (container and line.container != container):
                    continue
                lines.append(line)
                if len(lines) >= limit:
                    return lines
        return lines

    def _keys(self, prefix: str) -> Iterator[str]:
        token = ""
        while True:
            page = (
                self._store.list_objects_v2(Bucket=self._bucket, Prefix=prefix, ContinuationToken=token)
                if token
                else self._store.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
            )
            for item in page.get("Contents", []):
                if item["Key"].endswith(".ndjson.gz"):
                    yield item["Key"]
            token = page.get("NextContinuationToken") or ""
            if not page.get("IsTruncated") or not token:
                return

    def _lines(self, key: str) -> Iterator[ArchivedLogLine]:
        body = self._store.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        for raw in gzip.decompress(body).decode("utf-8").splitlines():
            if not raw.strip():
                continue
            record = json.loads(raw)
            # A run with no pods leaves one marker record naming no pod; it is not a log line.
            if record.get("schema") != SCHEMA or "pod" not in record:
                continue
            yield ArchivedLogLine(
                archived_at=record["archived_at"],
                namespace=record["namespace"],
                pod=record["pod"],
                container=record["container"],
                line=record.get("line"),
                error=record.get("archive_error"),
            )
