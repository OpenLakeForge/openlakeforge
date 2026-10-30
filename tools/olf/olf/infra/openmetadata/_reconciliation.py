"""REST upsert/resolve primitives against the OpenMetadata API.

`OpenMetadataReconciler` owns the idempotent PUT/PATCH/GET calls that create
or update one OpenMetadata entity at a time. `OpenMetadataDeployer`
(`olf.infra.openmetadata._orchestration`) composes a reconciler and sequences these
calls; this module has no opinion on ordering.
"""

from __future__ import annotations

import time
import urllib.parse

from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra.openmetadata._config import OpenMetadataConfig


class OpenMetadataReconciler:
    """Idempotent create/update primitives for one OpenMetadata deployment."""

    def __init__(self, config: OpenMetadataConfig, client: OpenMetadataClient):
        self.config = config
        self.client = client
        self._ensured_schema_fqns: set[str] = set()

    def login(self) -> None:
        if self.client.token:  # the automation bot's, set by the caller (SSO: basic login is off)
            return
        self.client.login(self.config.admin_email, self.config.admin_password)

    def wait_for_openmetadata(self) -> None:
        last_error = None
        for _ in range(120):
            try:
                self.client.request("GET", "/api/v1/system/config/jwks")
                return
            except OpenMetadataError as exc:
                last_error = exc
                time.sleep(2)
        raise OpenMetadataError(f"OpenMetadata did not become reachable: {last_error}")

    def require_stage_scoped(self, fqn: str, entity: str) -> None:
        """Reject an entity FQN that lies outside this stage's database root.

        One OpenMetadata deployment represents every governed stage, and the
        only thing separating their catalog entities is the
        `<service>.lakehouse_<stage>` database root (#131). The schemas and
        tables a deploy seeds arrive on their own environment channel
        (OPENLAKEFORGE_CATALOG_{SILVER,GOLD}_SCHEMA_FQNS_JSON), which
        `build_contract_env` leaves alone once it is set; one left behind by
        an earlier stage would otherwise seed that stage's catalog while this
        deploy reports the stage it was asked for. Check them against the
        root `OpenMetadataConfig` derives from the selected catalog database,
        so reconciling one stage never writes over another stage's entities.
        """
        root = self.config.catalog_database_fqn
        if fqn == root or fqn.startswith(f"{root}."):
            return
        raise OpenMetadataError(
            f"Refusing to reconcile {entity} {fqn!r}: it belongs to another stage. This deploy is scoped to the "
            f"database root {root!r}; check that OPENMETADATA_CATALOG_DATABASE and "
            "OPENLAKEFORGE_CATALOG_{SILVER,GOLD}_SCHEMA_FQNS_JSON come from the same stage's contract "
            "environment."
        )

    def list_schema_tables(self, schema_fqn: str) -> list[dict]:
        """Asset references for the tables a schema holds so far."""
        self.require_stage_scoped(schema_fqn, "database schema")
        query = f"databaseSchema={urllib.parse.quote(schema_fqn, safe='')}&limit=1000"
        refs: list[dict] = []
        after = None
        while True:
            suffix = f"&after={urllib.parse.quote(after, safe='')}" if after else ""
            page = self.client.request("GET", f"/api/v1/tables?{query}{suffix}")
            refs += [{"id": table["id"], "type": "table"} for table in page.get("data", [])]
            after = (page.get("paging") or {}).get("after")
            if not after:
                return refs

    def ensure_storage_service(self) -> None:
        aws_config = {"awsRegion": self.config.storage_region}
        if self.config.storage_endpoint:
            aws_config["endPointURL"] = self.config.storage_endpoint
        payload = {
            "name": self.config.storage_service,
            "displayName": self.config.storage_display_name,
            "serviceType": "S3",
            "connection": {"config": {"type": "S3", "awsConfig": aws_config}},
        }
        self.client.request("PUT", "/api/v1/services/storageServices", json_body=payload, ok_statuses=(200, 201))
        print(f"Upserted OpenMetadata storage service: {self.config.storage_service}")

    def ensure_container(self, name, parent_fqn, full_path, description) -> None:
        payload = {
            "name": name,
            "service": self.config.storage_service,
            "fullPath": full_path,
            "description": description,
        }
        if parent_fqn:
            encoded = urllib.parse.quote(parent_fqn, safe="")
            parent = self.client.request("GET", f"/api/v1/containers/name/{encoded}")
            parent_id = parent.get("id")
            if not parent_id:
                raise OpenMetadataError(
                    f"OpenMetadata container lookup for '{parent_fqn}' did not return an id: {parent}"
                )
            payload["parent"] = {"id": parent_id, "type": "container"}
        self.client.request("PUT", "/api/v1/containers", json_body=payload, ok_statuses=(200, 201))
        print(f"Upserted OpenMetadata container: {full_path}")

    def ensure_database_schema(self, schema_fqn: str, domain: str | None = None) -> None:
        """Create the databaseSchema and set its domain ahead of the Trino ingestion.

        The schema is where ownership lives: tables the ingestion discovers
        under it inherit the domain. The ingestion adopts this entity by FQN.
        """
        database_fqn, _, name = schema_fqn.rpartition(".")
        if not database_fqn or not name:
            raise OpenMetadataError(f"Malformed schema FQN {schema_fqn!r}: expected '<service>.<database>.<schema>'")
        self.require_stage_scoped(schema_fqn, "database schema")
        if schema_fqn in self._ensured_schema_fqns:
            return
        payload: dict = {"name": name, "database": database_fqn}
        if domain:
            payload["domains"] = [domain]
        self.client.request("PUT", "/api/v1/databaseSchemas", json_body=payload, ok_statuses=(200, 201))
        self._ensured_schema_fqns.add(schema_fqn)
        print(f"Upserted OpenMetadata database schema: {schema_fqn}")
