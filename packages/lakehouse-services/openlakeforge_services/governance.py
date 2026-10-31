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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from openlakeforge_domain import (
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


def display_name_from_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("-", " ").replace("_", " ").split())


def domain_description(domain: dict) -> str:
    parts = []
    if domain.get("description"):
        parts.append(str(domain["description"]))
    if domain.get("status"):
        parts.append(f"Status: {domain['status']}")
    medallion = domain.get("medallion")
    if isinstance(medallion, dict) and medallion:
        parts.append("Medallion layers:")
        for layer, config in medallion.items():
            if isinstance(config, dict):
                owner = config.get("owner", "unknown")
                description = config.get("description", "")
                parts.append(f"- {layer}: {description} Owner: {owner}.")
    return "\n".join(parts)


def domain_payload(domain: dict) -> dict:
    name = domain.get("name")
    if not name:
        raise OpenMetadataError("OpenMetadata domain metadata is missing required field 'name'.")
    payload = {
        "name": name,
        "displayName": domain.get("displayName") or domain.get("display_name") or display_name_from_name(name),
        "domainType": domain.get("domainType") or domain.get("domain_type") or "Source-aligned",
        "description": domain_description(domain),
    }
    for optional in ["owners", "experts", "reviewers", "tags", "style", "extension"]:
        if domain.get(optional):
            payload[optional] = domain[optional]
    return payload


def product_payload(product: dict) -> dict:
    name = product.get("name")
    if not name:
        raise OpenMetadataError("OpenMetadata data-product metadata is missing required field 'name'.")
    domains = product.get("domains")
    if domains is None and product.get("domain"):
        domains = [product["domain"]]
    if not domains:
        raise OpenMetadataError(f"OpenMetadata data product '{name}' must define 'domain' or 'domains'.")
    payload = {
        "name": name,
        "displayName": product.get("displayName") or product.get("display_name") or display_name_from_name(name),
        "description": product.get("description") or "",
        "domains": domains,
    }
    for optional in ["owners", "experts", "reviewers", "tags", "style", "extension"]:
        if product.get(optional):
            payload[optional] = product[optional]
    return payload


def product_entries(domain: dict) -> Iterator[dict]:
    products = domain.get("data_products") or []
    if not isinstance(products, list):
        raise OpenMetadataError(f"Domain '{domain.get('name', '<unknown>')}' data_products must be a list.")
    for product in products:
        if not isinstance(product, dict):
            raise OpenMetadataError(f"Unsupported data product entry in domain '{domain.get('name')}': {product!r}")
        normalized = dict(product)
        if not normalized.get("name"):
            normalized["name"] = f"{domain['name']}_{normalized.get('id', '')}".rstrip("_")
        if not normalized.get("domain") and not normalized.get("domains"):
            normalized["domain"] = domain["name"]
        yield normalized


def _gold_models(lakehouse_root: Path, product_id: str) -> list[str]:
    """A product's Gold tables: one per dbt model, named by its file (no model sets an alias)."""
    models = lakehouse_root / "gold" / product_id / "dbt" / "models"
    return sorted({path.stem for path in models.rglob("*.sql")})


def product_contract_key(product: dict) -> str:
    return product.get("asset_prefix") or product.get("name") or product.get("id") or ""


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

    def validate_deployment_inputs(self, domain_specs: list[tuple[Path, dict]]) -> None:
        """Check every schema FQN and its coverage before metadata writes."""
        for schema_fqn in (
            *self.config.catalog_silver_schema_fqns.values(),
            *self.config.catalog_gold_schema_fqns.values(),
        ):
            self._reconciler.require_stage_scoped(schema_fqn, "database schema")
        self.validate_bronze_entries(domain_specs)
        for _, domain in domain_specs:
            if domain["name"] not in self.config.catalog_silver_schema_fqns:
                raise OpenMetadataError(
                    f"Domain {domain['name']!r} Silver schema is not covered by the provider contract schema FQNs."
                )
            for product in product_entries(domain):
                if product_contract_key(product) not in self.config.catalog_gold_schema_fqns:
                    raise OpenMetadataError(
                        f"Data product '{product_contract_key(product)}' Gold schema is not covered "
                        "by the provider contract schema FQNs."
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

    def bronze_container_specs(self, domain_specs: list[tuple[Path, dict]]):
        """Build provider-specific Bronze container hierarchies from domain contracts."""
        bucket = self.config.storage_bronze_bucket
        root_fqn = f"{self.config.storage_service}.{bucket}"
        seen = set()

        for _, domain in domain_specs:
            for product in product_entries(domain):
                for source in product.get("bronze") or []:
                    source_path = source["path"]
                    parsed = urllib.parse.urlparse(source_path)
                    if parsed.scheme != "s3":
                        raise OpenMetadataError(
                            f"Data product '{product['name']}' Bronze path must use s3://: {source_path}"
                        )

                    segments = [segment for segment in parsed.path.split("/") if segment]
                    parent_fqn = root_fqn
                    full_path = f"s3://{bucket}"
                    for index, segment in enumerate(segments):
                        full_path = f"{full_path}/{segment}"
                        current_fqn = f"{parent_fqn}.{segment}"
                        if full_path not in seen:
                            is_source = index == len(segments) - 1
                            description = (
                                source.get("description", "") if is_source else f"Bronze source prefix for {full_path}."
                            )
                            yield {
                                "name": segment,
                                "parent_fqn": parent_fqn,
                                "path": full_path,
                                "description": description,
                            }
                            seen.add(full_path)
                        parent_fqn = current_fqn

    # --- source discovery -------------------------------------------------

    def domain_files(self) -> list[Path]:
        return self._descriptor_paths

    def _domain_specs(self) -> list[tuple[Path, dict]]:
        """Transform the canonical lakehouse descriptor into deployer domain specs.

        The v1alpha3 Lakehouse descriptor nests domains with products whose
        bronze references are provider-neutral ``{source, resources}`` pairs.
        The deployer machinery consumes v1alpha2-shaped dicts (``data_products``
        with explicit bronze container entries), so this maps between the two:
        bronze entries are derived from the source name plus each resource,
        and physical s3 paths are resolved from the provider contract at deploy
        time -- never stored in the descriptor.

        The two files are also loaded independently below (as raw dicts, to
        keep every cosmetic ``description``/``status`` field the deployer
        payloads need). ``load_lakehouse_inventory_from_descriptors`` runs
        first purely for its cross-descriptor validation: it is the same
        check every other consumer of these descriptors relies on to catch a
        Silver table naming a source/resource no Source descriptor declares.
        Without it, a bad reference would still build an
        ``s3://.../<source>/<resource>`` Bronze path below and publish a
        phantom asset to OpenMetadata instead of failing before any writes.
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
        lakehouse_document = load_governance_lakehouse_document(lakehouse_path)
        resource_descriptions: dict[str, dict[str, str]] = {}
        for source_path in source_paths:
            source_document = load_source_descriptor(source_path)
            resource_descriptions[source_document["name"]] = {
                resource["name"]: resource.get("description", "")
                for resource in source_document["resources"]
                if isinstance(resource, dict) and "name" in resource
            }
        bronze_bucket = self.config.storage_bronze_bucket
        domain_specs: list[tuple[Path, dict]] = []
        root = inventory.lakehouse_root
        for domain in lakehouse_document["domains"]:
            products = []
            for product in domain["products"]:
                bronze_entries = [
                    {
                        "name": entity.resource,
                        "path": f"s3://{bronze_bucket}/{entity.source}/{entity.resource}",
                        "description": resource_descriptions.get(entity.source, {}).get(entity.resource, ""),
                    }
                    for entity in entities[domain["name"]]
                ]
                bronze_entries = list({entry["path"]: entry for entry in bronze_entries}.values())
                products.append(
                    {
                        "id": product["id"],
                        "name": product["id"],
                        "displayName": product["displayName"],
                        "description": product.get("description", ""),
                        "status": product.get("status", ""),
                        "domain": domain["name"],
                        "bronze": bronze_entries,
                        "gold_tables": _gold_models(root, product["id"]),
                    }
                )
            domain_specs.append(
                (
                    lakehouse_path,
                    {
                        "name": domain["name"],
                        "displayName": domain["displayName"],
                        "description": domain.get("description", ""),
                        "status": domain.get("status", ""),
                        "silver_tables": [entity.name for entity in entities[domain["name"]]],
                        "data_products": products,
                    },
                )
            )
        return domain_specs

    def validate_bronze_entries(self, domain_specs) -> None:
        for _, domain in domain_specs:
            for product in product_entries(domain):
                bronze_entries = product.get("bronze")
                if bronze_entries is None:
                    continue
                if not isinstance(bronze_entries, list):
                    raise OpenMetadataError(
                        f"Data product '{product['name']}' Bronze entries must be an array."
                    )
                for index, container in enumerate(bronze_entries):
                    if not isinstance(container, dict):
                        raise OpenMetadataError(
                            f"Data product '{product['name']}' Bronze entry at index {index} must be an object."
                        )
                    if not isinstance(container.get("name"), str) or not container["name"]:
                        raise OpenMetadataError(
                            f"Data product '{product['name']}' Bronze entry at index {index} "
                            "is missing required 'name'."
                        )
                    if not isinstance(container.get("path"), str) or not container["path"]:
                        raise OpenMetadataError(
                            f"Data product '{product['name']}' Bronze entry at index {index} "
                            "is missing required 'path'."
                        )

    def deploy(self) -> None:
        self.wait_for_openmetadata()
        self.login()

        domain_specs = self._domain_specs()
        self.validate_deployment_inputs(domain_specs)

        # Phase A+B: Object Store service and medallion bucket containers.
        self.ensure_storage_service()
        for container in self.storage_bucket_specs():
            self.ensure_container(container["name"], None, container["path"], container["description"])
        for container in self.bronze_container_specs(domain_specs):
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
        for _, domain in domain_specs:
            domain_body = domain_payload(domain)
            self.client.request("PUT", "/api/v1/domains", json_body=domain_body, ok_statuses=(200, 201))
            self.progress.info(f"Upserted OpenMetadata domain: {domain_body['name']}")
            silver_fqn = self.config.catalog_silver_schema_fqns[domain["name"]]
            self.ensure_database_schema(silver_fqn, domain["name"])
            # Every table a pipeline writes is registered before it runs, so
            # its lineage resolves (ensure_table); the domain comes from the
            # schema.
            for table in domain.get("silver_tables", []):
                self.ensure_table(silver_fqn, table)

            for product in product_entries(domain):
                product_body = product_payload(product)
                self.client.request("PUT", "/api/v1/dataProducts", json_body=product_body, ok_statuses=(200, 201))
                self.progress.info(f"Upserted OpenMetadata data product: {product_body['name']}")
                schema_fqn = self.config.catalog_gold_schema_fqns[product_contract_key(product)]
                self.ensure_database_schema(schema_fqn, domain["name"])
                for table in product.get("gold_tables", []):
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
                self.progress.info(f"Attached {len(refs)} OpenMetadata asset(s) to data product: {product_body['name']}")

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
