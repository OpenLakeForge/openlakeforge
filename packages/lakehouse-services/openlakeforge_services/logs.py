"""Read the pod log archive in both layouts (ADR 0022, decision 5).

v1, written by the polling archiver (`openlakeforge_runtime/k8s_log_archive.py`):
`<base>/k8s/namespace=<ns>/date=<YYYY-MM-DD>/hour=<HH>/k8s-logs-<stamp>.ndjson.gz`.
v2, written by the node collector:
`<base>/v2/namespace=<ns>/date=<YYYY-MM-DD>/<stage>-<uuid>.ndjson.gz`.
Both are gzipped NDJSON. The caller passes an S3 client: this package holds no
object-store credential and does not depend on boto3 (ADR 0021, decision 4).
"""

from __future__ import annotations

import gzip
import heapq
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import chain
from typing import Any, Protocol
from urllib.parse import urlparse

# The writer's record schema; a record of another schema is skipped, not misread.
SCHEMA = "openlakeforge.k8s_log.v1"
SCHEMA_V2 = "openlakeforge.k8s_log.v2"

# Only a name Kubernetes could give a namespace becomes a path segment: no slash, `=` or dot.
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
# The collector keeps no severity field, so it is read from the level token the line starts its message with.
SEVERITIES = ("debug", "info", "warning", "error", "critical")
_LEVEL = re.compile(r"\b(DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\b", re.IGNORECASE)
_LEVEL_NAME = {"warn": "warning", "fatal": "critical"}


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
    # Set for v2 records (the v1 archiver never wrote them), `source` for both.
    stage: str | None = None
    component: str | None = None
    domain: str | None = None
    product: str | None = None
    run_id: str | None = None
    stream: str | None = None
    missing: tuple[str, ...] | None = None
    source: str | None = None


def _severity(text: str | None) -> int:
    """Rank of the first level token in the line, or -1 when it names none."""
    match = _LEVEL.search(text or "")
    if match is None:
        return -1
    name = match.group(1).lower()
    return SEVERITIES.index(_LEVEL_NAME.get(name, name))


def _instant(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
        component: str = "",
        product: str = "",
        run_id: str = "",
        severity: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[ArchivedLogLine]:
        """Lines of one namespace for a UTC day, or one hour of it, oldest first, at most `limit`.

        Every filter selects within the one namespace's partitions; none of them is a path segment, so a
        filter cannot move the listing to another stage. `severity` is a floor (`warning` also returns
        errors); `since` and `until` bound the record time. `component`, `product` and `run_id` exist only
        in v2, so setting one leaves v1 out.
        """
        # A slash would move the listing to another partition, such as another stage's namespace.
        if not _NAMESPACE.fullmatch(namespace):
            raise ValueError(f"not a namespace: {namespace!r}")
        if hour is not None and not 0 <= hour <= 23:
            raise ValueError(f"not an hour of the day: {hour}")
        if severity and severity not in SEVERITIES:
            raise ValueError(f"not a severity: {severity!r}")
        floor = SEVERITIES.index(severity) if severity else -1
        since, until = (
            None if bound is None else bound if bound.tzinfo else bound.replace(tzinfo=UTC) for bound in (since, until)
        )

        def wanted(line: ArchivedLogLine, at: datetime | None) -> bool:
            return (
                (not pod or line.pod == pod)
                and (not container or line.container == container)
                and (not component or line.component == component)
                and (not product or line.product == product)
                and (not run_id or line.run_id == run_id)
                and (floor < 0 or _severity(line.line) >= floor)
                and (since is None or (at is not None and at >= since))
                and (until is None or (at is not None and at < until))
            )

        v2 = heapq.nsmallest(limit, self._v2(namespace, day, hour, wanted), key=lambda item: item[0])
        # ADR 0022, decision 5: a v1 line is dropped only when v2 holds the same line of the same container.
        overlap = {(line.pod, line.container, line.archived_at, line.line) for _, line in v2}
        v1 = () if component or product or run_id else self._v1(namespace, day, hour, wanted, overlap)
        merged = heapq.nsmallest(limit, chain(v2, v1), key=lambda item: item[0])
        return [line for _, line in merged]

    def _v2(
        self, namespace: str, day: date, hour: int | None, wanted: Callable[[ArchivedLogLine, datetime | None], bool]
    ) -> Iterator[tuple[tuple[datetime, str], ArchivedLogLine]]:
        seen: set[str] = set()
        for key in sorted(self._keys(self._partition("v2", namespace, day, None))):
            for record in self._records(key):
                if record.get("schema") != SCHEMA_V2 or record["namespace"] != namespace:
                    continue
                stamp = record["time"]
                if hour is not None and stamp[11:13] != f"{hour:02d}":
                    continue
                missing = record.get("missing")
                line = ArchivedLogLine(
                    archived_at=stamp,
                    namespace=namespace,
                    pod=record["pod"],
                    container=record["container"],
                    line=record["message"],
                    stage=record.get("stage"),
                    component=record.get("component"),
                    domain=record.get("domain"),
                    product=record.get("product"),
                    run_id=record.get("run_id"),
                    stream=record.get("stream"),
                    missing=tuple(missing) if isinstance(missing, list) else (),
                    source="v2",
                )
                at = _instant(stamp)
                if not wanted(line, at):
                    continue
                # A replay after a collector restart writes the same record again.
                dedup = record.get("dedup_key")
                if dedup is not None:
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                yield (at or datetime.min.replace(tzinfo=UTC), stamp), line

    def _v1(
        self,
        namespace: str,
        day: date,
        hour: int | None,
        wanted: Callable[[ArchivedLogLine, datetime | None], bool],
        overlap: set[tuple[str, str, str, str | None]],
    ) -> Iterator[tuple[tuple[datetime, str], ArchivedLogLine]]:
        # The object names carry the archive time to the second, so key order is archive order.
        for key in sorted(self._keys(self._partition("k8s", namespace, day, hour))):
            for record in self._records(key):
                # A run with no pods leaves one marker record naming no pod; it is not a log line.
                if record.get("schema") != SCHEMA or "pod" not in record:
                    continue
                text = record.get("line")
                line = ArchivedLogLine(
                    archived_at=record["archived_at"],
                    namespace=record["namespace"],
                    pod=record["pod"],
                    container=record["container"],
                    line=text,
                    error=record.get("archive_error"),
                    source="v1",
                )
                # The line starts with the kubelet's timestamp, which is when it was written; the
                # archive time is only when it was copied.
                stamp, _, rest = (text or "").partition(" ")
                at = _instant(stamp) or _instant(line.archived_at)
                if (line.pod, line.container, stamp, rest) in overlap:
                    continue
                if wanted(line, at):
                    yield (at or datetime.min.replace(tzinfo=UTC), line.archived_at), line

    def _partition(self, layout: str, namespace: str, day: date, hour: int | None) -> str:
        parts = [self._prefix, layout, f"namespace={namespace}", f"date={day.isoformat()}"]
        if hour is not None:
            parts.append(f"hour={hour:02d}")
        return "/".join(part for part in parts if part) + "/"

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

    def _records(self, key: str) -> Iterator[dict[str, Any]]:
        body = self._store.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        for raw in gzip.decompress(body).decode("utf-8").splitlines():
            if not raw.strip():
                continue
            record = json.loads(raw)
            if isinstance(record, dict):
                yield record
