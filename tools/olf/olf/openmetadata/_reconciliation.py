"""REST upsert/resolve primitives against the OpenMetadata API.

`OpenMetadataReconciler` owns the idempotent PUT/PATCH/GET calls that create
or update one OpenMetadata entity at a time. `OpenMetadataDeployer`
(`olf.openmetadata._orchestration`) composes a reconciler and sequences these
calls; this module has no opinion on ordering.
"""

from __future__ import annotations

import time
import urllib.parse

from olf.clients.openmetadata import OpenMetadataClient, OpenMetadataError
from olf.openmetadata._config import OpenMetadataConfig


class OpenMetadataReconciler:
    """Idempotent create/update primitives for one OpenMetadata deployment."""

    def __init__(self, config: OpenMetadataConfig, client: OpenMetadataClient):
        self.config = config
        self.client = client
        self._ensured_schema_fqns: set[str] = set()

    def login(self) -> None:
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

    def resolve_table_asset(self, asset):
        if isinstance(asset, str):
            asset_type = "table"
            fqn = asset
        elif isinstance(asset, dict):
            asset_type = asset.get("type", "table")
            fqn = asset.get("fqn") or asset.get("fullyQualifiedName")
        else:
            raise OpenMetadataError(f"Unsupported asset entry: {asset!r}")

        if asset_type != "table":
            raise OpenMetadataError(
                f"Unsupported OpenMetadata data-product asset type '{asset_type}'. Only 'table' is supported."
            )
        if not fqn:
            raise OpenMetadataError(f"OpenMetadata data-product asset is missing 'fqn': {asset!r}")
        self.require_stage_scoped(fqn, "table")

        encoded_fqn = urllib.parse.quote(fqn, safe="")
        try:
            table = self.client.request("GET", f"/api/v1/tables/name/{encoded_fqn}?fields=domains")
        except OpenMetadataError as exc:
            if "HTTP 404" in str(exc):
                return None, fqn
            raise

        table_id = table.get("id")
        if not table_id:
            raise OpenMetadataError(f"OpenMetadata table lookup for '{fqn}' did not return an id: {table}")
        return {
            "id": table_id,
            "type": "table",
            "name": table.get("name"),
            "fullyQualifiedName": table.get("fullyQualifiedName", fqn),
            "displayName": table.get("displayName"),
            "domains": table.get("domains") or [],
        }, None

    def resolve_domain_ref(self, domain_name):
        encoded_name = urllib.parse.quote(domain_name, safe="")
        domain = self.client.request("GET", f"/api/v1/domains/name/{encoded_name}")
        domain_id = domain.get("id")
        if not domain_id:
            raise OpenMetadataError(f"OpenMetadata domain lookup for '{domain_name}' did not return an id: {domain}")
        return {
            "id": domain_id,
            "type": "domain",
            "name": domain.get("name", domain_name),
            "fullyQualifiedName": domain.get("fullyQualifiedName", domain_name),
            "displayName": domain.get("displayName"),
        }

    @staticmethod
    def _domain_matches(existing, expected) -> bool:
        return (
            existing.get("id") == expected.get("id")
            or existing.get("fullyQualifiedName") == expected.get("fullyQualifiedName")
            or existing.get("name") == expected.get("name")
        )

    def ensure_table_domains(self, table_refs, domain_refs) -> None:
        for table_ref in table_refs:
            existing_domains = table_ref.get("domains") or []
            missing_domains = [
                domain_ref
                for domain_ref in domain_refs
                if not any(self._domain_matches(existing, domain_ref) for existing in existing_domains)
            ]
            if not missing_domains:
                continue
            domains = existing_domains + missing_domains
            self.client.request(
                "PATCH",
                f"/api/v1/tables/{table_ref['id']}",
                json_body=[{"op": "add", "path": "/domains", "value": domains}],
                content_type="application/json-patch+json",
            )
            table_ref["domains"] = domains
            print(
                "Assigned OpenMetadata domain(s) "
                f"{', '.join(domain['fullyQualifiedName'] for domain in missing_domains)} "
                f"to table: {table_ref['fullyQualifiedName']}"
            )

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

    def ensure_database_schema(self, schema_fqn: str) -> None:
        """Create the databaseSchema a table stub is about to reference.

        Phase 1 used to pre-create these alongside the Polaris namespaces it
        owned. Now that namespaces are reconciled in Phase 2 (ADR 0002), the
        schema entity has to be created here instead -- OpenMetadata rejects a
        table whose `databaseSchema` does not resolve, and the Polaris crawler
        that would otherwise discover it has not run yet at seeding time.
        """
        database_fqn, _, name = schema_fqn.rpartition(".")
        if not database_fqn or not name:
            raise OpenMetadataError(f"Malformed schema FQN {schema_fqn!r}: expected '<service>.<database>.<schema>'")
        self.require_stage_scoped(schema_fqn, "database schema")
        if schema_fqn in self._ensured_schema_fqns:
            return
        self.client.request(
            "PUT",
            "/api/v1/databaseSchemas",
            json_body={"name": name, "database": database_fqn},
            ok_statuses=(200, 201),
        )
        self._ensured_schema_fqns.add(schema_fqn)
        print(f"Upserted OpenMetadata database schema: {schema_fqn}")

    def ensure_table_stub(self, schema_fqn, name, description) -> None:
        self.require_stage_scoped(schema_fqn, "table")
        payload = {"name": name, "databaseSchema": schema_fqn, "columns": []}
        if description:
            payload["description"] = description
        self.client.request("PUT", "/api/v1/tables", json_body=payload, ok_statuses=(200, 201))
        print(f"Upserted OpenMetadata table stub: {schema_fqn}.{name}")
