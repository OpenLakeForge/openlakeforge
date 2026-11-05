"""Removing OpenMetadata services that no longer report to governance, for the bootstrap (#562).

Ports `remove_service` from the governance module's `bootstrap.sh.tftpl`. Look up by name,
delete by id, the pipeline before the service that owns it -- so the common path issues no
delete at all, and a rerun after a partial removal finishes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.common import log

# The unqualified services every deployment registered before #131: (kind, service, pipeline).
LEGACY_SERVICES = (
    ("pipelineServices", "dagster", "dagster-metadata-ingestion"),
    ("dashboardServices", "superset", "superset-metadata-ingestion"),
)


def _by_name(client: OpenMetadataClient, path: str) -> Mapping[str, Any] | None:
    try:
        return client.request("GET", path)
    except OpenMetadataError as exc:
        if exc.status == 404:
            return None
        raise


def remove_service(client: OpenMetadataClient, kind: str, service: str, pipeline: str) -> None:
    """Hard-delete `<service>.<pipeline>` and then the service, each only if it exists."""
    stale_pipeline = _by_name(client, f"/api/v1/services/ingestionPipelines/name/{service}.{pipeline}")
    if stale_pipeline is not None:
        client.request(
            "DELETE",
            f"/api/v1/services/ingestionPipelines/{stale_pipeline['id']}",
            params={"hardDelete": "true"},
            ok_statuses=(200, 404),
        )
    stale_service = _by_name(client, f"/api/v1/services/{kind}/name/{service}")
    if stale_service is not None:
        client.request(
            "DELETE",
            f"/api/v1/services/{kind}/{stale_service['id']}",
            params={"hardDelete": "true", "recursive": "true"},
            ok_statuses=(200, 404),
        )
        log.step(f"Removed stale OpenMetadata service '{service}'.")


def remove_legacy_services(client: OpenMetadataClient) -> None:
    for kind, service, pipeline in LEGACY_SERVICES:
        remove_service(client, kind, service, pipeline)
