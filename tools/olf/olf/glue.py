"""AWS Glue adapter for ownership-safe catalog reconciliation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from openlakeforge_domain import CatalogNamespace

from olf.catalog import CATALOG_KEY, MANAGED_BY_KEY, MANAGED_BY_VALUE, NamespaceState

LOCATION_PROPERTY = "LocationUri"


class GlueError(RuntimeError):
    """Raised when Glue rejects a catalog operation."""


@dataclass(frozen=True)
class GlueConfig:
    catalog_id: str
    region: str
    catalog_name: str = "lakehouse_dev"


@dataclass
class GlueClient:
    config: GlueConfig
    _client: Any = field(default=None, repr=False)
    _databases: dict[str, dict] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self._client is None:
            from olf.auth import aws_session

            self._client = aws_session(os.environ, region=self.config.region or None).client(
                "glue", region_name=self.config.region or None
            )

    def list_namespaces(self) -> dict[str, NamespaceState]:
        states: dict[str, NamespaceState] = {}
        self._databases = {}
        for page in self._client.get_paginator("get_databases").paginate(CatalogId=self.config.catalog_id):
            for database in page.get("DatabaseList", []):
                name = database["Name"]
                self._databases[name] = database
                states[name] = NamespaceState(database.get(LOCATION_PROPERTY, ""), database.get("Parameters") or {})
        return states

    def _database_input(self, namespace: CatalogNamespace, existing: dict | None = None) -> dict:
        existing = existing or {}
        parameters = dict(existing.get("Parameters") or {})
        parameters.update({MANAGED_BY_KEY: MANAGED_BY_VALUE, CATALOG_KEY: self.config.catalog_name})
        value = {
            "Name": namespace.name,
            LOCATION_PROPERTY: namespace.location,
            "Description": existing.get("Description")
            or f"OpenLakeForge {self.config.catalog_id} {namespace.name} Iceberg namespace",
            "Parameters": parameters,
        }
        for key in ("CreateTableDefaultPermissions", "TargetDatabase", "FederatedDatabase"):
            if existing.get(key) is not None:
                value[key] = existing[key]
        return value

    def create_namespace(self, namespace: CatalogNamespace) -> None:
        try:
            self._client.create_database(
                CatalogId=self.config.catalog_id, DatabaseInput=self._database_input(namespace)
            )
        except self._client.exceptions.AlreadyExistsException:
            pass

    def adopt_namespace(self, namespace: CatalogNamespace) -> None:
        self.update_namespace_location(namespace)

    def update_namespace_location(self, namespace: CatalogNamespace) -> None:
        existing = self._databases.get(namespace.name)
        if existing is None:
            existing = self._client.get_database(
                CatalogId=self.config.catalog_id, Name=namespace.name
            )["Database"]
        self._client.update_database(
            CatalogId=self.config.catalog_id,
            Name=namespace.name,
            DatabaseInput=self._database_input(namespace, existing),
        )

    def drop_namespace(self, name: str) -> None:
        """Remove Glue metadata only; S3 table files are deliberately retained."""
        paginator = self._client.get_paginator("get_tables")
        for page in paginator.paginate(CatalogId=self.config.catalog_id, DatabaseName=name, MaxResults=1):
            for table in page.get("TableList", []):
                self._client.delete_table(CatalogId=self.config.catalog_id, DatabaseName=name, Name=table["Name"])
        try:
            self._client.delete_database(CatalogId=self.config.catalog_id, Name=name)
        except self._client.exceptions.EntityNotFoundException:
            pass
