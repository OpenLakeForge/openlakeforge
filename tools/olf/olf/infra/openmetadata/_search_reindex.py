"""Reindex OpenMetadata search when OpenSearch has come back empty (#909).

OpenSearch runs without a volume, so a pod restart, an upgrade or a restore empties the index
while the entities survive in the OpenMetadata database. The bootstrap probes search for a
database it knows exists through REST and, on no hit, runs the SearchIndexingApplication.

The endpoints, the trigger keys and the run statuses match OpenMetadata 1.13.6's source
(`AppResource` `/trigger/{name}` and `/name/{name}/runs/latest`, `searchIndexingAppConfig.json`,
`appRunRecord.json`, `SearchResource` `/query`); they have not yet been exercised against a live
stack, so they live in the constants below.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.common import log
from olf.infra.openmetadata._catalog_seed import CatalogSeed

SEARCH_PATH = "/api/v1/search/query"
SEARCH_INDEX = "database_search_index"
TRIGGER_PATH = "/api/v1/apps/trigger/SearchIndexingApplication"
LATEST_RUN_PATH = "/api/v1/apps/name/SearchIndexingApplication/runs/latest"
TRIGGER_BODY = {"recreateIndex": True, "entities": ["all"]}
_RUNNING = frozenset({"pending", "started", "running", "active", "stopinprogress"})
_FAILED = frozenset({"failed", "activeerror", "stopped"})
TIMEOUT_SECONDS = 600.0
POLL_SECONDS = 5.0


def _hits(client: OpenMetadataClient, fqn: str) -> int:
    try:
        result = client.request(
            "GET", SEARCH_PATH, params={"q": f'fullyQualifiedName:"{fqn}"', "index": SEARCH_INDEX, "size": 1}
        )
    except OpenMetadataError as exc:
        # A wiped OpenSearch has no index to query, which OpenMetadata reports as an error status.
        # An authentication or authorization failure is not an empty index: let it surface.
        if exc.status is not None and exc.status >= 400 and exc.status not in (401, 403):
            return 0
        raise
    return int(result.get("hits", {}).get("total", {}).get("value", 0))


def _latest_run(client: OpenMetadataClient) -> Mapping[str, Any]:
    try:
        return client.request("GET", LATEST_RUN_PATH)
    except OpenMetadataError as exc:
        if exc.status == 404:  # the application has never run
            return {}
        raise


def ensure_search_indexed(
    client: OpenMetadataClient,
    seed: CatalogSeed,
    *,
    timeout: float = TIMEOUT_SECONDS,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Do nothing when search finds the first stage database; otherwise reindex and wait for it."""
    if not seed.databases:
        return
    fqn = f"{seed.service}.{seed.databases[0]}"
    if _hits(client, fqn):
        return
    log.step("OpenMetadata search is empty (OpenSearch has no volume); reindexing...")
    before = _latest_run(client).get("timestamp")
    client.request("POST", TRIGGER_PATH, json_body=TRIGGER_BODY, ok_statuses=(200, 202))
    deadline = clock() + timeout
    while clock() < deadline:
        sleep(poll)
        run = _latest_run(client)
        status = str(run.get("status", "")).lower()
        # The previous run still being "latest" means the new one has not registered yet.
        if run.get("timestamp") == before or status in _RUNNING or not status:
            continue
        if status in _FAILED:
            raise OpenMetadataError(f"OpenMetadata search reindex ended as {status!r}; see the openmetadata pod logs.")
        if _hits(client, fqn):
            log.info("OpenMetadata search reindexed.")
            return
    raise OpenMetadataError(
        f"OpenMetadata search is still empty {timeout:.0f}s after triggering a reindex. Run the "
        "SearchIndexingApplication from OpenMetadata Settings > Applications, then rerun `olf infra deploy`."
    )
