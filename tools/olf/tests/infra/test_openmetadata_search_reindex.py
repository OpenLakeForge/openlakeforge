"""The OpenMetadata search reindex: triggered only when search misses a known database."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError, OpenMetadataTransientError

from olf.infra.openmetadata import _search_reindex as reindex
from olf.infra.openmetadata._catalog_seed import CatalogSeed

SEED = CatalogSeed(
    service="polaris",
    display_name="x",
    trino_namespace="trino://t:8080",
    pipeline_service="p",
    databases=("lakehouse_dev",),
)


class FakeClient:
    def __init__(self, *, hits: int, runs: list[Mapping[str, Any]], search_error: int | None = None) -> None:
        self.hits = hits
        self.runs = runs  # successive answers of runs/latest; the last one repeats
        self.search_error = search_error
        self.calls: list[tuple[str, str]] = []
        self.queries: list[Mapping[str, Any]] = []
        self.triggered = False

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, **_kw: Any) -> Any:
        self.calls.append((method, path))
        if path == reindex.SEARCH_PATH:
            self.queries.append(params or {})
            if self.search_error:
                # As the real client does: a 5xx is a transient error, anything else an OpenMetadataError.
                error = OpenMetadataTransientError if self.search_error >= 500 else OpenMetadataError
                raise error("no index", status=self.search_error)
            return {"hits": {"total": {"value": self.hits}}}
        if path == reindex.TRIGGER_PATH:
            self.triggered = True
            return {}
        run = self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
        if self.triggered and run.get("status") == "success":
            self.hits = 1
        return run


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run(client: FakeClient, clock: Clock, timeout: float = 60) -> None:
    reindex.ensure_search_indexed(
        cast(OpenMetadataClient, client), SEED, timeout=timeout, poll=5, clock=clock, sleep=clock.sleep
    )


def test_empty_search_triggers_reindex_and_waits() -> None:
    client = FakeClient(
        hits=0,
        runs=[
            {"timestamp": 1, "status": "success"},  # before the trigger
            {"timestamp": 1, "status": "success"},  # new run not registered yet
            {"timestamp": 2, "status": "running"},
            {"timestamp": 2, "status": "success"},
        ],
    )
    _run(client, Clock())
    assert ("POST", reindex.TRIGGER_PATH) in client.calls
    assert client.queries[0]["q"] == 'fullyQualifiedName:"polaris.lakehouse_dev"'
    assert client.hits == 1


def test_missing_index_error_counts_as_empty_and_failed_run_is_an_error() -> None:
    client = FakeClient(hits=0, search_error=500, runs=[{}, {"timestamp": 2, "status": "failed"}])
    with pytest.raises(OpenMetadataError, match="ended as 'failed'"):
        _run(client, Clock())
    assert client.triggered


def test_hits_do_not_trigger() -> None:
    client = FakeClient(hits=1, runs=[{}])
    _run(client, Clock())
    assert not client.triggered


def test_reindex_that_never_completes_times_out() -> None:
    client = FakeClient(hits=0, runs=[{"timestamp": 1, "status": "success"}, {"timestamp": 2, "status": "running"}])
    clock = Clock()
    with pytest.raises(OpenMetadataError, match="still empty 60s after"):
        _run(client, clock)
    assert clock.now >= 60


def test_an_authentication_failure_surfaces_instead_of_reindexing() -> None:
    client = FakeClient(hits=0, search_error=401, runs=[{}])
    with pytest.raises(OpenMetadataError, match="no index"):
        _run(client, Clock())
    assert not client.triggered
