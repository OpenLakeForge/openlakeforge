"""The catalog half of the OpenMetadata bootstrap: OpenLineage, the lakehouse service, stage databases.

Ports the middle steps of the governance module's former `bootstrap.sh.tftpl` (#562). Every call is a
PUT upsert, so a rerun converges.
"""

from __future__ import annotations

from dataclasses import dataclass

from openlakeforge_services.openmetadata import OpenMetadataClient

_UPSERT = (200, 201)


@dataclass(frozen=True)
class CatalogSeed:
    """What the catalog steps write: one service, and a database (with the same schemas) per governed stage."""

    service: str
    display_name: str
    trino_namespace: str  # `trino://host:port`, which is also the service's connection address
    pipeline_service: str  # OpenLineage's default pipeline service for the canonical stage
    databases: tuple[str, ...] = ()
    schemas: tuple[str, ...] = ()


def enable_openlineage(client: OpenMetadataClient, seed: CatalogSeed) -> None:
    """Native OpenLineage ingestion for dbt-trino and Floe, both mapped onto the catalog service."""
    mapping = {seed.trino_namespace: seed.service, seed.service: seed.service}
    client.request(
        "PUT",
        "/api/v1/system/settings",
        ok_statuses=_UPSERT,
        json_body={
            "config_type": "openLineageSettings",
            "config_value": {
                "enabled": True,
                "autoCreateEntities": True,
                "eventTypeFilter": ["COMPLETE"],
                "defaultPipelineService": seed.pipeline_service,
                "namespaceToServiceMapping": mapping,
            },
        },
    )


def ensure_lakehouse_service(client: OpenMetadataClient, seed: CatalogSeed) -> None:
    """The database service crawls through Trino as the `openmetadata` user (1.13 removed the Iceberg connector)."""
    host_port = seed.trino_namespace.removeprefix("trino://")
    client.request(
        "PUT",
        "/api/v1/services/databaseServices",
        ok_statuses=_UPSERT,
        json_body={
            "name": seed.service,
            "displayName": seed.display_name,
            "serviceType": "Trino",
            "connection": {"config": {"type": "Trino", "hostPort": host_port, "username": "openmetadata"}},
        },
    )


def ensure_stage_databases(client: OpenMetadataClient, seed: CatalogSeed) -> None:
    """A `<service>.<database>` entity per governed stage, with its schemas.

    `deploy-metadata` seeds a stage's tables under that root and OpenMetadata rejects a
    schema whose database does not resolve (#211).
    """
    for database in seed.databases:
        client.request(
            "PUT",
            "/api/v1/databases",
            ok_statuses=_UPSERT,
            json_body={"name": database, "service": seed.service},
        )
        for schema in seed.schemas:
            client.request(
                "PUT",
                "/api/v1/databaseSchemas",
                ok_statuses=_UPSERT,
                json_body={"name": schema, "database": f"{seed.service}.{database}"},
            )


def bootstrap_catalog(client: OpenMetadataClient, seed: CatalogSeed) -> None:
    enable_openlineage(client, seed)
    ensure_lakehouse_service(client, seed)
    ensure_stage_databases(client, seed)
