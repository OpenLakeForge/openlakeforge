"""OpenMetadata governance metadata: storage containers, domains, data products.

`MetadataDeployer` seeds one stage's entities from the governance and Bronze
source descriptors, through `MetadataReconciler`'s idempotent upserts. Callers
pass an `OpenMetadataClient` holding whatever credential they resolved; the
deployer logs in with `MetadataConfig`'s admin credentials only when the
client carries no token.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from openlakeforge_core import (
    LakehouseDescriptorError,
    load_governance_lakehouse_document,
    load_lakehouse_inventory_from_descriptors,
    load_source_descriptor,
)

from .openmetadata import OpenMetadataClient, OpenMetadataError
from .progress import SILENT, Progress


@dataclass
class MetadataConfig:
    """Where one stage's metadata goes: the catalog root and the storage it describes."""

    base_url: str
    admin_email: str
    admin_password: str
    allow_missing_assets: bool
    catalog_service: str
    catalog_database: str
    catalog_database_fqn: str
    catalog_silver_schema_fqns: dict
    catalog_gold_schema_fqns: dict
    storage_service: str
    storage_display_name: str
    storage_endpoint: str
    storage_region: str
    storage_bronze_bucket: str
    storage_silver_bucket: str
    storage_gold_bucket: str


@dataclass(frozen=True)
class BronzeContainer:
    """One Bronze resource, landed at `<bucket>/<source>/<resource>`, to register under the storage service."""

    source: str
    resource: str
    description: str


@dataclass(frozen=True)
class ProductSpec:
    """One data product of `governance.yml`, with the Gold tables its dbt models build."""

    id: str
    display_name: str
    description: str
    domain: str
    gold_tables: tuple[str, ...]


@dataclass(frozen=True)
class DomainSpec:
    """One domain of `governance.yml`, its Silver tables and the Bronze resources they read."""

    name: str
    display_name: str
    description: str
    status: str
    silver_tables: tuple[str, ...]
    bronze: tuple[BronzeContainer, ...]
    products: tuple[ProductSpec, ...]


def domain_payload(domain: DomainSpec) -> dict:
    description = "\n".join(part for part in (domain.description, domain.status and f"Status: {domain.status}") if part)
    return {
        "name": domain.name,
        "displayName": domain.display_name,
        "domainType": "Source-aligned",
        "description": description,
    }


def product_payload(product: ProductSpec) -> dict:
    return {
        "name": product.id,
        "displayName": product.display_name,
        "description": product.description,
        "domains": [product.domain],
    }


def _gold_models(lakehouse_root: Path, product_id: str) -> list[str]:
    """A product's Gold tables: one per dbt model, named by its file (no model sets an alias)."""
    models = lakehouse_root / "gold" / product_id / "dbt" / "models"
    return sorted({path.stem for path in models.rglob("*.sql")})


class MetadataReconciler:
    """Idempotent create/update primitives for one OpenMetadata deployment."""

    def __init__(self, config: MetadataConfig, client: OpenMetadataClient, *, progress: Progress = SILENT):
        self.config = config
        self.client = client
        self.progress = progress
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
        root the config derives from the selected catalog database,
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
        self.progress.info(f"Upserted OpenMetadata storage service: {self.config.storage_service}")

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
        self.progress.info(f"Upserted OpenMetadata container: {full_path}")

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
        self.progress.info(f"Upserted OpenMetadata database schema: {schema_fqn}")

    def ensure_table(self, schema_fqn: str, name: str) -> None:
        """Register a table the stage's pipelines write, before their lineage arrives.

        OpenMetadata 1.13.6 attaches an OpenLineage edge only to tables it
        already holds: its auto-create path inserts the entity without
        updatedAt, Postgres refuses the row, and the edge is dropped. The Trino
        ingestion adopts the entity by FQN and fills in its columns. POST, not
        PUT: an upsert with no columns would delete the ones the ingestion
        found, so an existing table (409) is left alone.
        """
        self.require_stage_scoped(schema_fqn, "table")
        payload = {"name": name, "databaseSchema": schema_fqn, "columns": []}
        self.client.request("POST", "/api/v1/tables", json_body=payload, ok_statuses=(200, 201, 409))
        self.progress.info(f"Registered OpenMetadata table: {schema_fqn}.{name}")


class MetadataDeployer:
    """Seeds one stage's storage containers, domains and data products.

    `descriptor_paths` is the governance descriptor followed by every Bronze
    source descriptor it references; `domain_files()` returns them, and a
    caller that resolves them later overrides it. `rerun_hint` finishes the
    advice given when Gold schemas have no tables yet.
    """

    def __init__(
        self,
        config: MetadataConfig,
        client: OpenMetadataClient,
        *,
        descriptor_paths: Sequence[Path] = (),
        rerun_hint: str = "then rerun the metadata deployment.",
        progress: Progress = SILENT,
    ):
        self.config = config
        self.client = client
        self.progress = progress
        self._descriptor_paths = list(descriptor_paths)
        self._rerun_hint = rerun_hint
        self._reconciler = MetadataReconciler(config, client, progress=progress)

    # --- reconciliation delegators -----------------------------------------

    def login(self) -> None:
        self._reconciler.login()

    def wait_for_openmetadata(self) -> None:
        self._reconciler.wait_for_openmetadata()

    def ensure_storage_service(self) -> None:
        self._reconciler.ensure_storage_service()

    def ensure_container(self, name, parent_fqn, full_path, description) -> None:
        self._reconciler.ensure_container(name, parent_fqn, full_path, description)

    def ensure_database_schema(self, schema_fqn: str, domain: str | None = None) -> None:
        self._reconciler.ensure_database_schema(schema_fqn, domain)

    def ensure_table(self, schema_fqn: str, name: str) -> None:
        self._reconciler.ensure_table(schema_fqn, name)

    def list_schema_tables(self, schema_fqn: str) -> list[dict]:
        return self._reconciler.list_schema_tables(schema_fqn)

    # --- schema coverage ---------------------------------------------------

    def validate_deployment_inputs(self, domains: Sequence[DomainSpec]) -> None:
        """Check every schema FQN and its coverage before metadata writes."""
        for schema_fqn in (
            *self.config.catalog_silver_schema_fqns.values(),
            *self.config.catalog_gold_schema_fqns.values(),
        ):
            self._reconciler.require_stage_scoped(schema_fqn, "database schema")
        for domain in domains:
            if domain.name not in self.config.catalog_silver_schema_fqns:
                raise OpenMetadataError(
                    f"Domain {domain.name!r} Silver schema is not covered by the provider contract schema FQNs."
                )
            for product in domain.products:
                if product.id not in self.config.catalog_gold_schema_fqns:
                    raise OpenMetadataError(
                        f"Data product {product.id!r} Gold schema is not covered by the provider contract schema FQNs."
                    )

    def storage_bucket_specs(self):
        specs = [
            (self.config.storage_bronze_bucket, "Bronze landing bucket for raw immutable source files."),
            (
                self.config.storage_silver_bucket,
                "Silver bucket for Floe-validated Iceberg tables and validation reports.",
            ),
            (self.config.storage_gold_bucket, "Gold bucket for dbt-owned business marts."),
        ]
        seen = set()
        for name, description in specs:
            if not name or name in seen:
                continue
            seen.add(name)
            yield {"name": name, "path": f"s3://{name}", "description": description}

    def bronze_container_specs(self, domains: Sequence[DomainSpec]):
        """The Bronze container hierarchy: one container per source prefix, then one per resource under it."""
        bucket = self.config.storage_bronze_bucket
        root_fqn = f"{self.config.storage_service}.{bucket}"
        seen = set()

        for domain in domains:
            for container in domain.bronze:
                source_path = f"s3://{bucket}/{container.source}"
                if source_path not in seen:
                    seen.add(source_path)
                    yield {
                        "name": container.source,
                        "parent_fqn": root_fqn,
                        "path": source_path,
                        "description": f"Bronze source prefix for {source_path}.",
                    }
                resource_path = f"{source_path}/{container.resource}"
                if resource_path not in seen:
                    seen.add(resource_path)
                    yield {
                        "name": container.resource,
                        "parent_fqn": f"{root_fqn}.{container.source}",
                        "path": resource_path,
                        "description": container.description,
                    }

    # --- source discovery -------------------------------------------------

    def domain_files(self) -> list[Path]:
        return self._descriptor_paths

    def _domain_specs(self) -> list[DomainSpec]:
        """The domains and products of the governance descriptor, with what they cover.

        `load_lakehouse_inventory_from_descriptors` runs first for its
        cross-descriptor validation: a Silver table naming a source or resource
        no Source descriptor declares fails here, before any write, instead of
        publishing a phantom Bronze container. The governance and source
        documents are read again for the `description`/`status` text the
        inventory does not keep.
        """
        descriptor_paths = self.domain_files()
        if not descriptor_paths:
            raise OpenMetadataError("No OpenMetadata lakehouse descriptor to deploy.")
        lakehouse_path = descriptor_paths[0]
        source_paths = descriptor_paths[1:]
        try:
            inventory = load_lakehouse_inventory_from_descriptors(lakehouse_path, source_paths)
            entities = {domain.name: inventory.floe_entities(domain) for domain in inventory.domains}
        except LakehouseDescriptorError as exc:
            raise OpenMetadataError(str(exc)) from exc
        resource_descriptions: dict[tuple[str, str], str] = {}
        for source_path in source_paths:
            source_document = load_source_descriptor(source_path)
            for resource in source_document["resources"]:
                resource_descriptions[(source_document["name"], resource["name"])] = resource.get("description", "")
        root = inventory.lakehouse_root
        domains: list[DomainSpec] = []
        for domain in load_governance_lakehouse_document(lakehouse_path)["domains"]:
            domain_entities = entities[domain["name"]]
            bronze = {
                (entity.source, entity.resource): BronzeContainer(
                    entity.source, entity.resource, resource_descriptions.get((entity.source, entity.resource), "")
                )
                for entity in domain_entities
            }
            products = tuple(
                ProductSpec(
                    id=product["id"],
                    display_name=product["displayName"],
                    description=product.get("description", ""),
                    domain=domain["name"],
                    gold_tables=tuple(_gold_models(root, product["id"])),
                )
                for product in domain["products"]
            )
            domains.append(
                DomainSpec(
                    name=domain["name"],
                    display_name=domain["displayName"],
                    description=domain.get("description", ""),
                    status=domain.get("status", ""),
                    silver_tables=tuple(entity.name for entity in domain_entities),
                    bronze=tuple(bronze.values()),
                    products=products,
                )
            )
        return domains

    def deploy(self) -> None:
        self.wait_for_openmetadata()
        self.login()

        domains = self._domain_specs()
        self.validate_deployment_inputs(domains)

        # Phase A+B: Object Store service and medallion bucket containers.
        self.ensure_storage_service()
        for container in self.storage_bucket_specs():
            self.ensure_container(container["name"], None, container["path"], container["description"])
        for container in self.bronze_container_specs(domains):
            self.ensure_container(
                container["name"],
                container["parent_fqn"],
                container["path"],
                container["description"],
            )

        # Phase C: Upsert domains and data products from governance YAML. A
        # domain is set on the databaseSchema entity rather than on each table:
        # OpenMetadata resolves a table's domain from its schema when the table
        # has none of its own (TableRepository.setInheritedFields, 1.13.6), so
        # tables the Trino ingestion finds later need no per-table write.
        missing_assets = []
        for domain in domains:
            domain_body = domain_payload(domain)
            self.client.request("PUT", "/api/v1/domains", json_body=domain_body, ok_statuses=(200, 201))
            self.progress.info(f"Upserted OpenMetadata domain: {domain_body['name']}")
            silver_fqn = self.config.catalog_silver_schema_fqns[domain.name]
            self.ensure_database_schema(silver_fqn, domain.name)
            # Every table a pipeline writes is registered before it runs, so
            # its lineage resolves (ensure_table); the domain comes from the
            # schema.
            for table in domain.silver_tables:
                self.ensure_table(silver_fqn, table)

            for product in domain.products:
                product_body = product_payload(product)
                self.client.request("PUT", "/api/v1/dataProducts", json_body=product_body, ok_statuses=(200, 201))
                self.progress.info(f"Upserted OpenMetadata data product: {product_body['name']}")
                schema_fqn = self.config.catalog_gold_schema_fqns[product.id]
                self.ensure_database_schema(schema_fqn, domain.name)
                for table in product.gold_tables:
                    self.ensure_table(schema_fqn, table)

                # A schema is a valid data-product asset, but a table does not
                # inherit `dataProducts` from its schema (no inheritDataProducts
                # call in TableRepository, 1.13.6), so attach the tables found
                # under the Gold schema. Silver inputs reach the product through
                # lineage, not membership.
                refs = self.list_schema_tables(schema_fqn)
                if not refs:
                    missing_assets.append(schema_fqn)
                    continue
                product_name = urllib.parse.quote(product_body["name"], safe="")
                self.client.request(
                    "PUT",
                    f"/api/v1/dataProducts/{product_name}/assets/add",
                    json_body={"assets": refs, "dryRun": False},
                    ok_statuses=(200, 201),
                )
                self.progress.info(
                    f"Attached {len(refs)} OpenMetadata asset(s) to data product: {product_body['name']}"
                )

        if missing_assets:
            message = "\n".join(f"  - {fqn}" for fqn in sorted(set(missing_assets)))
            guidance = (
                "OpenMetadata Gold schemas have no tables yet:\n"
                f"{message}\n"
                "Run the product ETL jobs in Dagster, wait for the catalog metadata ingestion to crawl the catalog, "
                f"{self._rerun_hint}"
            )
            if self.config.allow_missing_assets:
                self.progress.warn(guidance)
            else:
                raise OpenMetadataError(guidance)
