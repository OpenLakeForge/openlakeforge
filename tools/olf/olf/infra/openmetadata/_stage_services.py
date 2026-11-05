"""The per-stage pipeline and dashboard services of the OpenMetadata bootstrap (#562).

Ports the stage loop of the governance module's `bootstrap.sh.tftpl`. Each governed stage gets a
`dagster_<stage>` pipeline service, and each with analytics a `superset_<stage>` dashboard service,
named as the contract requires (provider_contracts rejects any other). Each service gets one
metadata ingestion pipeline, created paused and deployed without a run: the Dagster connector still
queries the pre-1.x `PipelineRuns` type, and Superset reports only exist after the artifact phase
imports them (ADR 0007).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.common import log
from olf.infra import k8s, superset
from olf.infra.openmetadata._stale_services import _by_name, remove_service

# The closed set the profile schema allows, so iterating it also finds the services of a stage
# that is no longer governed.
STAGES = ("dev", "uat", "prod")
DAGSTER_PIPELINE = "dagster-metadata-ingestion"
SUPERSET_PIPELINE = "superset-metadata-ingestion"
# The governance module's defaults, which no environment overrides.
SUPERSET_USERNAME = "admin"
SUPERSET_AUTH_PROVIDER = "db"
SUPERSET_VERIFY_SSL = "no-ssl"
_PIPELINES = "/api/v1/services/ingestionPipelines"
_UPSERT = (200, 201)


@dataclass(frozen=True)
class StageServices:
    """One governed stage's endpoints; no Superset URL means the stage has no analytics."""

    stage: str
    dagster_url: str
    superset_url: str = ""
    superset_namespace: str = ""  # holds the Superset admin Secret


def _register(
    client: OpenMetadataClient,
    kind: str,
    service: Mapping[str, Any],
    pipeline: str,
    pipeline_display: str,
    source_config: Mapping[str, Any],
    service_ref_type: str,
) -> None:
    """Upsert the service, create its pipeline only if missing, and deploy it without a run."""
    name = service["name"]
    service_id = client.request("PUT", f"/api/v1/services/{kind}", ok_statuses=_UPSERT, json_body=service).get("id")
    if not service_id:
        raise OpenMetadataError(f"OpenMetadata returned no id for service '{name}'")
    existing = _by_name(client, f"{_PIPELINES}/name/{name}.{pipeline}")
    if existing is None:
        existing = client.request(
            "POST",
            _PIPELINES,
            ok_statuses=_UPSERT,
            json_body={
                "name": pipeline,
                "displayName": pipeline_display,
                "pipelineType": "metadata",
                "sourceConfig": {"config": source_config},
                "airflowConfig": {"startDate": "2025-01-01T00:00:00.000Z", "retries": 1, "pausePipeline": True},
                "service": {"id": service_id, "type": service_ref_type},
            },
        )
    try:  # best effort, as in the template: the pipeline exists and stays paused either way
        client.request("POST", f"{_PIPELINES}/deploy/{existing['id']}", ok_statuses=_UPSERT)
    except ServiceClientError as exc:
        log.warn(f"Could not deploy ingestion pipeline '{name}.{pipeline}': {exc}")
    log.step(f"OpenMetadata service '{name}' registered (ingestion deferred, see ADR 0007).")


def _register_dagster(client: OpenMetadataClient, stage: StageServices) -> None:
    _register(
        client,
        "pipelineServices",
        {
            "name": f"dagster_{stage.stage}",
            "displayName": f"Dagster Orchestrator ({stage.stage})",
            "serviceType": "Dagster",
            "connection": {"config": {"type": "Dagster", "host": stage.dagster_url, "timeout": 1000}},
        },
        DAGSTER_PIPELINE,
        "Dagster Pipeline Metadata",
        {"type": "PipelineMetadata", "includeLineage": True},
        "pipelineService",
    )


def _register_superset(client: OpenMetadataClient, stage: StageServices, environ: Mapping[str, str] | None) -> None:
    password = k8s.secret_value(superset.ADMIN_SECRET, "admin-password", stage.superset_namespace, environ=environ)
    _register(
        client,
        "dashboardServices",
        {
            "name": f"superset_{stage.stage}",
            "displayName": f"Superset Reporting ({stage.stage})",
            "serviceType": "Superset",
            "connection": {
                "config": {
                    "type": "Superset",
                    "hostPort": stage.superset_url,
                    "connection": {
                        "provider": SUPERSET_AUTH_PROVIDER,
                        "username": SUPERSET_USERNAME,
                        "password": password,
                        "verifySSL": SUPERSET_VERIFY_SSL,
                    },
                }
            },
        },
        SUPERSET_PIPELINE,
        "Superset Dashboard Metadata",
        {"type": "DashboardMetadata", "includeDataModels": True},
        "dashboardService",
    )


def bootstrap_stage_services(
    client: OpenMetadataClient, stages: Sequence[StageServices], environ: Mapping[str, str] | None = None
) -> None:
    """Register the given stages' services and remove those of every other stage.

    A stage that lost governance or analytics keeps its namespace, but its service would point
    governance at a runtime that no longer reports to it.
    """
    governed = {stage.stage: stage for stage in stages}
    for name in STAGES:
        stage = governed.get(name)
        if stage:
            _register_dagster(client, stage)
        else:
            remove_service(client, "pipelineServices", f"dagster_{name}", DAGSTER_PIPELINE)
        if stage and stage.superset_url:
            _register_superset(client, stage, environ)
        else:
            remove_service(client, "dashboardServices", f"superset_{name}", SUPERSET_PIPELINE)
