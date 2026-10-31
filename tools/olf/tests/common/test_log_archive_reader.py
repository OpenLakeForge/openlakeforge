"""The archive reader against what the archiver writes: the writer runs into an in-memory object store."""

from __future__ import annotations

import importlib
import io
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import ModuleType
from types import SimpleNamespace as NS
from typing import Any

import pytest
from openlakeforge_services.logs import ArchivedLogLine, LogArchive


class _ObjectStore:
    """put, list and get with S3's semantics: keys listed in order, `page_size` to a page."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.page_size = page_size

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:  # noqa: N803 - boto3's names
        self.objects[(Bucket, Key)] = Body

    def list_objects_v2(self, *, Bucket: str, Prefix: str, ContinuationToken: str = "") -> dict[str, Any]:  # noqa: N803
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": more,
            **({"NextContinuationToken": str(start + self.page_size)} if more else {}),
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class _Core:
    def __init__(self, logs: dict[tuple[str, str], str | Exception]) -> None:
        self._logs = logs

    def list_namespaced_pod(self, namespace: str) -> NS:
        pods: dict[str, list[str]] = {}
        for pod, container in self._logs:
            pods.setdefault(pod, []).append(container)
        return NS(
            items=[
                NS(metadata=NS(name=pod), status=NS(container_statuses=[NS(name=c) for c in containers]), spec=None)
                for pod, containers in pods.items()
            ]
        )

    def read_namespaced_pod_log(self, *, name: str, container: str, **_: object) -> str:
        value = self._logs[(name, container)]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # The archiver imports the Kubernetes client, which olf's environment,
    # where these tests run, does not install. Its calls are replaced below,
    # so a stand-in serves the import; monkeypatch removes it afterwards, and
    # test_k8s_log_archive.py's importorskip sees the same environment whatever
    # the order.
    if importlib.util.find_spec("kubernetes") is None:
        stand_in = ModuleType("kubernetes")
        stand_in.client = NS(CoreV1Api=None)  # type: ignore[attr-defined]
        stand_in.config = NS(load_incluster_config=None, load_kube_config=None, ConfigException=Exception)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kubernetes", stand_in)
    return importlib.import_module("libs.k8s_log_archive")


Archive = Callable[[datetime, dict[tuple[str, str], "str | Exception"], str], None]


@pytest.fixture
def store(writer: ModuleType, monkeypatch: pytest.MonkeyPatch) -> _ObjectStore:
    fake = _ObjectStore()
    monkeypatch.setattr(writer, "s3_client", lambda: fake)
    monkeypatch.setattr(writer.config, "load_incluster_config", lambda: None)
    monkeypatch.setenv("OPENLAKEFORGE_LOG_BASE_URI", "s3://ops/stages/dev/logs")
    return fake


@pytest.fixture
def archive(writer: ModuleType, store: _ObjectStore, monkeypatch: pytest.MonkeyPatch) -> Archive:
    """Run the archiver at `at` over pods whose (pod, container) logs are given, into `store`."""

    def run(at: datetime, logs: dict[tuple[str, str], str | Exception], namespace: str) -> None:
        class _Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return at

        monkeypatch.setattr(writer, "datetime", _Clock)
        monkeypatch.setattr(writer.client, "CoreV1Api", lambda: _Core(logs))
        monkeypatch.setenv("OPENLAKEFORGE_KUBE_NAMESPACE", namespace)
        writer.main()

    return run


DAY = date(2026, 10, 30)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 30, hour, minute, tzinfo=UTC)


def test_a_day_reads_every_archive_oldest_first(store: _ObjectStore, archive: Archive) -> None:
    archive(_at(14), {("dagster-0", "web"): "late"}, "olf-dev")
    archive(_at(9), {("dagster-0", "web"): "early\nsecond"}, "olf-dev")

    lines = LogArchive(store, "s3://ops/stages/dev/logs").read("olf-dev", DAY)

    assert [line.line for line in lines] == ["early", "second", "late"]
    assert lines[0] == ArchivedLogLine(
        archived_at=_at(9).isoformat(), namespace="olf-dev", pod="dagster-0", container="web", line="early"
    )


def test_an_hour_reads_only_its_partition(store: _ObjectStore, archive: Archive) -> None:
    archive(_at(9), {("p", "c"): "nine"}, "olf-dev")
    archive(_at(10), {("p", "c"): "ten"}, "olf-dev")

    assert [line.line for line in LogArchive(store, "s3://ops/stages/dev/logs").read("olf-dev", DAY, hour=10)] == [
        "ten"
    ]


def test_another_namespace_is_not_read(store: _ObjectStore, archive: Archive) -> None:
    archive(_at(9), {("p", "c"): "dev"}, "olf-dev")
    archive(_at(9, 30), {("p", "c"): "prod"}, "olf-prod")

    assert [line.line for line in LogArchive(store, "s3://ops/stages/dev/logs").read("olf-dev", DAY)] == ["dev"]


def test_pod_and_container_filter_and_limit_stops_early(store: _ObjectStore, archive: Archive) -> None:
    logs: dict[tuple[str, str], str | Exception] = {
        ("run-1", "main"): "a\nb\nc",
        ("run-1", "sidecar"): "x",
        ("run-2", "main"): "z",
    }
    archive(_at(9), logs, "olf-dev")
    archive = LogArchive(store, "s3://ops/stages/dev/logs")

    assert [line.line for line in archive.read("olf-dev", DAY, pod="run-1", container="main", limit=2)] == ["a", "b"]


def test_an_unreadable_container_reads_as_its_error(store: _ObjectStore, archive: Archive) -> None:
    archive(_at(9), {("p", "bad"): RuntimeError("container not started")}, "olf-dev")

    (line,) = LogArchive(store, "s3://ops/stages/dev/logs").read("olf-dev", DAY)

    assert (line.line, line.error) == (None, "container not started")


def test_a_run_with_no_pods_leaves_no_line(store: _ObjectStore, archive: Archive) -> None:
    archive(_at(9), {}, "olf-dev")

    assert LogArchive(store, "s3://ops/stages/dev/logs").read("olf-dev", DAY) == []


def test_listing_follows_the_continuation_token(store: _ObjectStore, archive: Archive) -> None:
    for minute in range(5):
        archive(_at(9, minute), {("p", "c"): f"run {minute}"}, "olf-dev")
    store.page_size = 2

    lines = LogArchive(store, "s3://ops/stages/dev/logs").read("olf-dev", DAY)

    assert [line.line for line in lines] == [f"run {minute}" for minute in range(5)]


@pytest.mark.parametrize(("namespace", "hour"), [("olf-dev/date=2026-10-30", None), ("", None), ("olf-dev", 24)])
def test_a_namespace_or_hour_that_would_leave_the_partition_is_refused(namespace: str, hour: int | None) -> None:
    with pytest.raises(ValueError):
        LogArchive(_ObjectStore(), "s3://ops/logs").read(namespace, DAY, hour=hour)


def test_a_base_uri_that_is_not_s3_is_refused() -> None:
    with pytest.raises(ValueError, match="s3://"):
        LogArchive(_ObjectStore(), "/var/log/olf")
