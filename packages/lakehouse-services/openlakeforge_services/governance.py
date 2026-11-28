"""OpenMetadata governance metadata: storage containers, domains, data products.

`MetadataDeployer` seeds one stage's entities from the governance and Bronze
source descriptors, through `MetadataReconciler`'s idempotent upserts. Callers
pass an `OpenMetadataClient` holding whatever credential they resolved; the
deployer logs in with `MetadataConfig`'s admin credentials only when the
client carries no token.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openlakeforge_core import (
    LakehouseDescriptorError,
    ProductSla,
    load_governance_lakehouse_document,
    load_lakehouse_inventory_from_descriptors,
    load_source_descriptor,
    parse_sla,
)

from .openmetadata import OpenMetadataClient, OpenMetadataError, OpenMetadataTransientError, fqn_part
from .ownership import (
    MARKED_ENTITY_TYPES,
    MARKER_DESCRIPTION,
    MARKER_PROPERTY,
    marker_value,
    patch_operations,
    tag_operations,
)
from .progress import SILENT, Progress

if TYPE_CHECKING:
    from .drift import DriftItem

# `lifecycleStage` is ignored on write by OpenMetadata 1.13.6 (200, value unchanged), so the lifecycle and the
# terms of use live in custom properties on the data product. See docs/testing/openmetadata-governance-findings.md.
LIFECYCLE_PROPERTY = "olfLifecycle"
TERMS_PROPERTY = "olfTermsOfUse"
FRESHNESS_POLICY_PROPERTY = "olfFreshnessPolicy"
PRODUCT_PROPERTIES = (
    (LIFECYCLE_PROPERTY, "Lifecycle stage declared in governance.yml; the native lifecycleStage is not writable."),
    (TERMS_PROPERTY, "Terms of use declared in governance.yml, as JSON with `allowed` and `prohibited` lists."),
    (
        FRESHNESS_POLICY_PROPERTY,
        "SLA fields OpenMetadata's DataProduct.sla has no place for (readyBy, timezone, schedule, gracePeriod), "
        "declared in governance.yml, as JSON.",
    ),
)
SENSITIVITY_CLASSIFICATION = "DataSensitivity"
SENSITIVITY_DESCRIPTION = (
    "Data sensitivity declared in governance.yml. Metadata only: it does not grant or revoke access."
)


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
    owner: Mapping[str, str] | None = None
    experts: tuple[Mapping[str, str], ...] = ()
    reviewers: tuple[Mapping[str, str], ...] = ()
    product_type: str | None = None
    tier: str | None = None
    lifecycle: str | None = None
    terms_of_use: Mapping[str, Any] | None = None
    classification: str | None = None
    sla: ProductSla | None = None


@dataclass(frozen=True)
class DomainSpec:
    """One domain of `governance.yml`, its Silver tables and the Bronze resources they read."""

    name: str
    display_name: str
    description: str
    silver_tables: tuple[str, ...]
    bronze: tuple[BronzeContainer, ...]
    products: tuple[ProductSpec, ...]
    owner: Mapping[str, str] | None = None
    experts: tuple[Mapping[str, str], ...] = ()
    domain_type: str | None = None
    classification: str | None = None


def domain_payload(domain: DomainSpec) -> dict:
    return {
        "name": domain.name,
        "displayName": domain.display_name,
        "domainType": domain.domain_type or "Source-aligned",
        "description": domain.description,
    }


def tag_fqns(tier: str | None, classification: str | None) -> tuple[str, ...]:
    return tuple(
        fqn
        for fqn in (
            f"Tier.{tier}" if tier else "",
            f"{SENSITIVITY_CLASSIFICATION}.{classification}" if classification else "",
        )
        if fqn
    )


def _people(
    resolved: Mapping[tuple[str, str], dict],
    owner: Mapping[str, str] | None,
    experts: Sequence[Mapping[str, str]],
    reviewers: Sequence[Mapping[str, str]] = (),
) -> dict[str, list[dict]]:
    """The owner, experts and reviewers a descriptor names, as the references OpenMetadata stores; none declared
    means none written, so a person's own assignment survives."""
    named = {"owners": [owner] if owner else [], "experts": experts, "reviewers": reviewers}
    return {field: [resolved[(ref["type"], ref["ref"])] for ref in refs] for field, refs in named.items() if refs}


def sla_payload(sla: ProductSla | None) -> dict[str, Any]:
    """The native `DataProduct.sla` fields OLF owns. `availability` and `responseTime` are not declared, so they
    are never written and a person's values stay."""
    if sla is None:
        return {}
    native: dict[str, Any] = {"tier": sla.tier}
    if sla.maximum_age_minutes is not None:
        native["dataFreshness"] = sla.maximum_age_minutes
    if sla.minimum_quality_score is not None:
        native["dataQuality"] = sla.minimum_quality_score
    return {"sla": native}


def freshness_policy(sla: ProductSla | None) -> str | None:
    """The declared SLA fields that have no native OpenMetadata home, as stable JSON; None when none are declared."""
    if sla is None:
        return None
    policy = {
        key: value
        for key, value in (
            ("readyBy", sla.ready_by),
            ("timezone", sla.timezone),
            ("schedule", sla.schedule),
            ("gracePeriod", sla.grace_period),
        )
        if value
    }
    return json.dumps(policy, sort_keys=True) if policy else None


def _product_sla(domain: str, product: Mapping[str, Any]) -> ProductSla | None:
    if "sla" not in product:
        return None
    return parse_sla(product["sla"], where=f"governance.yml: domains[{domain}].products[{product['id']}]")


def _product_extension(product: ProductSpec) -> dict[str, str]:
    extension = {}
    policy = freshness_policy(product.sla)
    if policy:
        extension[FRESHNESS_POLICY_PROPERTY] = policy
    if product.lifecycle:
        extension[LIFECYCLE_PROPERTY] = product.lifecycle
    if product.terms_of_use:
        extension[TERMS_PROPERTY] = json.dumps(product.terms_of_use, sort_keys=True)
    return extension


def product_payload(product: ProductSpec) -> dict:
    return {
        "name": product.id,
        "displayName": product.display_name,
        "description": product.description,
        "domains": [product.domain],
    }


def _gold_models(lakehouse_root: Path, product_id: str) -> list[str]:
    """A product's Gold tables: one per dbt model, named by its file (no model sets an alias)."""
    models = lakehouse_root / "gold" / "models" / product_id
    return sorted({path.stem for path in models.rglob("*.sql")})


class MetadataReconciler:
    """Idempotent create/update primitives for one OpenMetadata deployment."""

    def __init__(
        self, config: MetadataConfig, client: OpenMetadataClient, *, revision: str = "", progress: Progress = SILENT
    ):
        self.config = config
        self.client = client
        self.progress = progress
        self._marker = {MARKER_PROPERTY: marker_value(revision)}
        self._ensured_schema_fqns: set[str] = set()
        self._string_type_id = ""

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

    def lookup(self, collection: str, fqn: str, fields: str = "extension") -> Mapping[str, Any] | None:
        """The entity at `fqn`, or None when OpenMetadata does not hold it. A read: never writes."""
        path = f"/api/v1/{collection}/name/{urllib.parse.quote(fqn, safe='')}"
        entity = self.client.request("GET", path, params={"fields": fields}, ok_statuses=(200, 404))
        return entity if entity.get("id") else None

    def _ensure_property(self, entity_type: str, name: str, description: str) -> None:
        definition = self.client.request(
            "GET", f"/api/v1/metadata/types/name/{entity_type}", params={"fields": "customProperties"}
        )
        if any(prop.get("name") == name for prop in definition.get("customProperties") or []):
            return
        if not self._string_type_id:
            self._string_type_id = self.client.request("GET", "/api/v1/metadata/types/name/string")["id"]
        self.client.request(
            "PUT",
            f"/api/v1/metadata/types/{definition['id']}",
            json_body={
                "name": name,
                "description": description,
                "propertyType": {"id": self._string_type_id, "type": "type"},
            },
            ok_statuses=(200, 201),
        )
        self.progress.info(f"Defined the {name} custom property on {entity_type}")

    def ensure_marker_properties(self) -> None:
        """Define the `olfManaged` custom property on each entity type OLF stamps; writes only what is missing."""
        for entity_type in MARKED_ENTITY_TYPES:
            self._ensure_property(entity_type, MARKER_PROPERTY, MARKER_DESCRIPTION)

    def ensure_product_properties(self) -> None:
        """Define the lifecycle and terms-of-use custom properties on data products."""
        for name, description in PRODUCT_PROPERTIES:
            self._ensure_property("dataProduct", name, description)

    def ensure_tags(self, tag_fqns: Collection[str]) -> None:
        """Check the tags a deploy will write exist. `Tier` ships with OpenMetadata; the DataSensitivity
        classification is OLF's own and is provisioned here, so any other missing tag is an error."""
        for fqn in sorted(tag_fqns):
            if self.lookup("tags", fqn, "") is not None:
                continue
            classification, _, name = fqn.partition(".")
            if classification != SENSITIVITY_CLASSIFICATION:
                raise OpenMetadataError(
                    f"OpenMetadata has no tag {fqn!r}. The Tier classification ships with OpenMetadata "
                    "(Tier1 to Tier5); check that it was not deleted."
                )
            if self.lookup("classifications", classification, "") is None:
                self.client.request(
                    "POST",
                    "/api/v1/classifications",
                    json_body={
                        "name": classification,
                        "description": SENSITIVITY_DESCRIPTION,
                        "mutuallyExclusive": True,
                    },
                    ok_statuses=(200, 201, 409),
                )
            self.client.request(
                "POST",
                "/api/v1/tags",
                json_body={
                    "name": name,
                    "description": f"{name}: {SENSITIVITY_DESCRIPTION}",
                    "classification": classification,
                },
                ok_statuses=(200, 201, 409),
            )
            self.progress.info(f"Provisioned OpenMetadata tag {fqn}")

    def resolve_reference(self, ref: Mapping[str, str], *, where: str, owner: bool = False) -> dict[str, str]:
        """The OpenMetadata user or team a descriptor reference names. Never creates one."""
        kind, name = ref["type"], ref["ref"]
        entity = self.lookup("teams" if kind == "team" else "users", name, "")
        if entity is None:
            raise OpenMetadataError(
                f"{where}: {kind} {name!r} does not exist in OpenMetadata, and OpenLakeForge never creates users or "
                "teams. Create it in OpenMetadata (Settings, Teams & Users) or correct the reference in governance.yml."
            )
        if owner and kind == "team" and entity.get("teamType") != "Group":
            raise OpenMetadataError(
                f"{where}: team {name!r} has teamType {entity.get('teamType')!r}, and OpenMetadata only lets a team "
                "of type Group own entities (the Organization team is refused). Reference a Group team."
            )
        return {"id": entity["id"], "type": kind}

    def reconcile(
        self,
        collection: str,
        fqn: str,
        create: Mapping[str, Any],
        owned: Mapping[str, Any],
        *,
        fields: str = "extension",
        extension: Mapping[str, Any] | None = None,
        tags: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        """Create the entity with its marker, then PATCH what differs; nothing when equal.

        `create` is the full creation body; `owned` is what OLF keeps in step afterwards, in the shape a read
        returns, `extension` the custom properties beside the marker, and `tags` the tag FQNs the entity must
        hold (tags of other classifications stay). A 409 on create means the crawler or a concurrent deploy got
        there first, so it is patched. After a write the entity is read back: OpenMetadata answers 200 to a
        field it ignores (`lifecycleStage`), so a 200 alone is not taken as success.
        """
        current = self.lookup(collection, fqn, fields)
        if current is None:
            self.client.request(
                "POST",
                f"/api/v1/{collection}",
                json_body={**create, "extension": self._marker},
                ok_statuses=(200, 201, 409),
            )
            current = self.lookup(collection, fqn, fields)
            if current is None:
                raise OpenMetadataError(f"OpenMetadata refused to create {collection} {fqn!r} and does not hold it.")
        desired = {**owned, "extension": {**self._marker, **(extension or {})}}
        operations = patch_operations(current, desired) + tag_operations(current, tags)
        if not operations:
            return current
        self.client.request(
            "PATCH",
            f"/api/v1/{collection}/{current['id']}",
            json_body=operations,
            content_type="application/json-patch+json",
        )
        stored = self.lookup(collection, fqn, fields) or {}
        ignored = sorted({op["path"] for op in patch_operations(stored, desired) + tag_operations(stored, tags)})
        if ignored:
            raise OpenMetadataError(
                f"OpenMetadata accepted the update of {collection} {fqn!r} but reads back without {ignored}; "
                "the server ignored it."
            )
        return current

    def ensure_storage_service(self) -> None:
        aws_config = {"awsRegion": self.config.storage_region}
        if self.config.storage_endpoint:
            aws_config["endPointURL"] = self.config.storage_endpoint
        name = self.config.storage_service
        current = self.lookup("services/storageServices", name, "")
        if current is None:
            payload = {
                "name": name,
                "displayName": self.config.storage_display_name,
                "serviceType": "S3",
                "connection": {"config": {"type": "S3", "awsConfig": aws_config}},
            }
            self.client.request(
                "POST", "/api/v1/services/storageServices", json_body=payload, ok_statuses=(200, 201, 409)
            )
            self.progress.info(f"Created OpenMetadata storage service: {name}")
            return
        operations = patch_operations(
            {"displayName": current.get("displayName")}, {"displayName": self.config.storage_display_name}
        )
        operations += self._storage_connection_operations(current, aws_config)
        if operations:
            self.client.request(
                "PATCH",
                f"/api/v1/services/storageServices/{current['id']}",
                json_body=operations,
                content_type="application/json-patch+json",
            )

    @staticmethod
    def _storage_connection_operations(current: Mapping[str, Any], aws_config: Mapping[str, str]) -> list[dict]:
        """JSON Patch for the S3 connection: leaf by leaf where the structure exists, so unrelated connection
        fields survive, and the missing parent object added whole where it does not (a service can have no
        connection, and JSON Patch cannot add below an absent object)."""
        connection = current.get("connection") or {}
        config = connection.get("config") or {}
        have = config.get("awsConfig") or {}
        if not connection:
            return [{"op": "add", "path": "/connection", "value": {"config": {"type": "S3", "awsConfig": aws_config}}}]
        if not config:
            return [{"op": "add", "path": "/connection/config", "value": {"type": "S3", "awsConfig": aws_config}}]
        if not have:
            return [{"op": "add", "path": "/connection/config/awsConfig", "value": dict(aws_config)}]
        operations: list[dict] = [
            {
                "op": "add" if key not in have else "replace",
                "path": f"/connection/config/awsConfig/{key}",
                "value": value,
            }
            for key, value in aws_config.items()
            if have.get(key) != value
        ]
        # An empty endpoint means native AWS S3, so a leftover custom endpoint is removed, as the old PUT did.
        if "endPointURL" in have and "endPointURL" not in aws_config:
            operations.append({"op": "remove", "path": "/connection/config/awsConfig/endPointURL"})
        return operations

    def ensure_container(self, name, parent_fqn, full_path, description) -> None:
        fqn = f"{parent_fqn or fqn_part(self.config.storage_service)}.{fqn_part(name)}"
        payload: dict = {
            "name": name,
            "service": self.config.storage_service,
            "fullPath": full_path,
            "description": description,
        }
        if parent_fqn and self.lookup("containers", fqn) is None:
            parent = self.client.request("GET", f"/api/v1/containers/name/{urllib.parse.quote(parent_fqn, safe='')}")
            if not parent.get("id"):
                raise OpenMetadataError(
                    f"OpenMetadata container lookup for '{parent_fqn}' did not return an id: {parent}"
                )
            payload["parent"] = {"id": parent["id"], "type": "container"}
        self.reconcile("containers", fqn, payload, {"description": description})

    def ensure_database_schema(self, schema_fqn: str, domain: Mapping[str, Any] | None = None) -> None:
        """Create the databaseSchema and set its domain ahead of the Trino ingestion.

        The schema is where ownership lives: tables the ingestion discovers
        under it inherit the domain. The ingestion adopts this entity by FQN.
        `domain` is the reconciled domain entity.
        """
        database_fqn, _, name = schema_fqn.rpartition(".")
        if not database_fqn or not name:
            raise OpenMetadataError(f"Malformed schema FQN {schema_fqn!r}: expected '<service>.<database>.<schema>'")
        self.require_stage_scoped(schema_fqn, "database schema")
        if schema_fqn in self._ensured_schema_fqns:
            return
        payload: dict = {"name": name, "database": database_fqn}
        owned: dict = {}
        if domain:
            payload["domains"] = [domain["fullyQualifiedName"]]
            owned["domains"] = [{"id": domain["id"], "type": "domain"}]
        self.reconcile("databaseSchemas", schema_fqn, payload, owned, fields="domains,extension")
        self._ensured_schema_fqns.add(schema_fqn)

    def ensure_table(self, schema_fqn: str, name: str, tags: Sequence[str] = ()) -> None:
        """Register a table the stage's pipelines write, before their lineage arrives.

        OpenMetadata 1.13.6 attaches an OpenLineage edge only to tables it
        already holds: its auto-create path inserts the entity without
        updatedAt, Postgres refuses the row, and the edge is dropped. The Trino
        ingestion adopts the entity by FQN and fills in its columns. POST, not
        PUT: an upsert with no columns would delete the ones the ingestion
        found, so an existing table (409) is left alone, and is only read first
        so a deploy that changes nothing writes nothing. The marker and the
        product's `tags` are the only fields patched onto a table this deploy
        did not create. The schema is scoped to the current stage, so the tags
        never reach another stage's tables.
        """
        self.require_stage_scoped(schema_fqn, "table")
        self.reconcile(
            "tables",
            f"{schema_fqn}.{name}",
            {"name": name, "databaseSchema": schema_fqn, "columns": []},
            {},
            fields="extension,tags" if tags else "extension",
            tags=tags,
        )

    def held_asset_ids(self, product_name: str) -> set[str] | None:
        """The ids of the assets a data product holds, or None when OpenMetadata does not answer with a list
        (an error answer included).

        `GET /dataProducts/name/{name}?fields=assets` returns no assets on 1.13.6 (a live run re-sent every
        attach), so the by-name assets listing (`getDataProductAssetsByName`, a ResultList paged by limit and
        offset, at most 1000 a page) is read instead.
        """
        held: set[str] = set()
        offset = 0
        while True:
            try:
                page = self.client.request(
                    "GET",
                    f"/api/v1/dataProducts/name/{urllib.parse.quote(product_name, safe='')}/assets",
                    params={"limit": 1000, "offset": offset},
                    ok_statuses=(200, 404),
                )
            # The attach below is the call that must succeed; it raises on its own. A 5xx or timeout is the
            # transient type, which is not an OpenMetadataError.
            except (OpenMetadataError, OpenMetadataTransientError):
                return None
            if "data" not in page:
                return None
            held |= {asset["id"] for asset in page["data"]}
            offset += len(page["data"])
            if not page["data"] or offset >= (page.get("paging") or {}).get("total", offset):
                return held

    def attach_assets(self, product_name: str, refs: list[dict]) -> int:
        """Add the tables the product does not hold yet; returns how many were added.

        When the held assets cannot be read, every table is sent: the attach is an idempotent upsert, so this
        costs one write and never loses an asset.
        """
        held = self.held_asset_ids(product_name)
        if held is None:
            self.progress.warn(f"Could not read the assets of data product {product_name}; attaching every table.")
            held = set()
        missing = [ref for ref in refs if ref["id"] not in held]
        if missing:
            self.client.request(
                "PUT",
                f"/api/v1/dataProducts/{urllib.parse.quote(product_name, safe='')}/assets/add",
                json_body={"assets": missing, "dryRun": False},
                ok_statuses=(200, 201),
            )
        return len(missing)


class MetadataDeployer:
    """Seeds one stage's storage containers, domains and data products.

    `descriptor_paths` is the governance descriptor followed by every Bronze
    source descriptor it references; `domain_files()` returns them, and a
    caller that resolves them later overrides it. `rerun_hint` finishes the
    advice given when Gold schemas have no tables yet. `revision` is the Git
    revision stamped into every entity's ownership marker.
    """

    def __init__(
        self,
        config: MetadataConfig,
        client: OpenMetadataClient,
        *,
        descriptor_paths: Sequence[Path] = (),
        rerun_hint: str = "then rerun the metadata deployment.",
        revision: str = "",
        progress: Progress = SILENT,
    ):
        self.config = config
        self.client = client
        self.progress = progress
        self._descriptor_paths = list(descriptor_paths)
        self._rerun_hint = rerun_hint
        self._reconciler = MetadataReconciler(config, client, revision=revision, progress=progress)

    # --- reconciliation delegators -----------------------------------------

    def login(self) -> None:
        self._reconciler.login()

    def wait_for_openmetadata(self) -> None:
        self._reconciler.wait_for_openmetadata()

    def ensure_storage_service(self) -> None:
        self._reconciler.ensure_storage_service()

    def ensure_container(self, name, parent_fqn, full_path, description) -> None:
        self._reconciler.ensure_container(name, parent_fqn, full_path, description)

    def ensure_database_schema(self, schema_fqn: str, domain: Mapping[str, Any] | None = None) -> None:
        self._reconciler.ensure_database_schema(schema_fqn, domain)

    def ensure_table(self, schema_fqn: str, name: str, tags: Sequence[str] = ()) -> None:
        self._reconciler.ensure_table(schema_fqn, name, tags)

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
        root_fqn = f"{fqn_part(self.config.storage_service)}.{fqn_part(bucket)}"
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
                        "parent_fqn": f"{root_fqn}.{fqn_part(container.source)}",
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
        documents are read again for the `description` text the
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
                    owner=product.get("owner"),
                    experts=tuple(product.get("experts", ())),
                    reviewers=tuple(product.get("reviewers", ())),
                    product_type=product.get("type"),
                    tier=product.get("tier"),
                    lifecycle=product.get("lifecycle"),
                    terms_of_use=product.get("termsOfUse"),
                    classification=product.get("classification"),
                    sla=parse_sla(
                        product["sla"], where=f"governance.yml: domains[{domain['name']}].products[{product['id']}]"
                    )
                    if "sla" in product
                    else None,
                )
                for product in domain["products"]
            )
            domains.append(
                DomainSpec(
                    name=domain["name"],
                    display_name=domain["displayName"],
                    description=domain.get("description", ""),
                    silver_tables=tuple(entity.name for entity in domain_entities),
                    bronze=tuple(bronze.values()),
                    products=products,
                    owner=domain.get("owner"),
                    experts=tuple(domain.get("experts", ())),
                    domain_type=domain.get("domainType"),
                    classification=domain.get("classification"),
                )
            )
        return domains

    def _resolve_people(self, domains: Sequence[DomainSpec]) -> dict[tuple[str, str], dict]:
        resolved: dict[tuple[str, str], dict] = {}
        for domain in domains:
            entities = [(f"domains[{domain.name}]", domain.owner, domain.experts, ())] + [
                (f"domains[{domain.name}].products[{p.id}]", p.owner, p.experts, p.reviewers) for p in domain.products
            ]
            for where, owner, experts, reviewers in entities:
                for field, refs in (
                    ("owner", [owner] if owner else []),
                    ("experts", experts),
                    ("reviewers", reviewers),
                ):
                    for ref in refs:
                        path = f"governance.yml {where}.{field}"
                        # OpenMetadata types `experts` as users, so a team would fail the PATCH after earlier writes.
                        if field == "experts" and ref["type"] != "user":
                            raise OpenMetadataError(
                                f"{path}: {ref['type']} {ref['ref']!r} is not allowed; OpenMetadata experts must be "
                                "users. Reference a user, or list the team under owner or reviewers."
                            )
                        resolved[(ref["type"], ref["ref"])] = self._reconciler.resolve_reference(
                            ref, where=path, owner=field == "owner"
                        )
        return resolved

    def drift(self) -> list[DriftItem]:
        """What differs between Git and OpenMetadata for this stage; reads only (see `drift.detect_drift`)."""
        from .drift import detect_drift

        self.login()
        domains = self._domain_specs()
        self.validate_deployment_inputs(domains)
        containers = [{**c, "parent_fqn": None} for c in self.storage_bucket_specs()] + list(
            self.bronze_container_specs(domains)
        )
        return detect_drift(self._reconciler, self.config, domains, containers)

    def deploy(self) -> None:
        self.wait_for_openmetadata()
        self.login()

        domains = self._domain_specs()
        self.validate_deployment_inputs(domains)
        # Resolved before the first write: an owner that does not exist must fail the deploy with nothing changed.
        people = self._resolve_people(domains)

        # Every write below reads first and changes only what differs, so a
        # second deploy of the same revision writes nothing. Nothing is deleted.
        self._reconciler.ensure_marker_properties()
        self._reconciler.ensure_product_properties()
        self._reconciler.ensure_tags(
            {
                fqn
                for domain in domains
                for fqn in (
                    *tag_fqns(None, domain.classification),
                    *(f for product in domain.products for f in tag_fqns(product.tier, product.classification)),
                )
            }
        )

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

        # Phase C: Reconcile domains and data products from governance YAML. A
        # domain is set on the databaseSchema entity rather than on each table:
        # OpenMetadata resolves a table's domain from its schema when the table
        # has none of its own (TableRepository.setInheritedFields, 1.13.6), so
        # tables the Trino ingestion finds later need no per-table write.
        missing_assets = []
        for domain in domains:
            domain_body = domain_payload(domain)
            domain_entity = self._reconciler.reconcile(
                "domains",
                domain_body["name"],
                domain_body,
                {
                    **{key: domain_body[key] for key in ("displayName", "description")},
                    **({"domainType": domain.domain_type} if domain.domain_type else {}),
                    **_people(people, domain.owner, domain.experts),
                },
                fields="owners,experts,tags,extension",
                tags=tag_fqns(None, domain.classification),
            )
            self.progress.info(f"Reconciled OpenMetadata domain: {domain_body['name']}")
            silver_fqn = self.config.catalog_silver_schema_fqns[domain.name]
            self.ensure_database_schema(silver_fqn, domain_entity)
            # Every table a pipeline writes is registered before it runs, so
            # its lineage resolves (ensure_table); the domain comes from the
            # schema.
            for table in domain.silver_tables:
                self.ensure_table(silver_fqn, table)

            for product in domain.products:
                product_body = product_payload(product)
                product_tags = tag_fqns(product.tier, product.classification)
                # A product's domain is set when it is created and not moved
                # afterwards: reassigning it is a human decision the drift
                # report surfaces, not a write a deploy makes.
                self._reconciler.reconcile(
                    "dataProducts",
                    product_body["name"],
                    product_body,
                    {
                        **{key: product_body[key] for key in ("displayName", "description")},
                        **({"dataProductType": product.product_type} if product.product_type else {}),
                        **_people(people, product.owner, product.experts, product.reviewers),
                        **sla_payload(product.sla),
                    },
                    fields="owners,experts,reviewers,tags,extension",
                    extension=_product_extension(product),
                    tags=product_tags,
                )
                self.progress.info(f"Reconciled OpenMetadata data product: {product_body['name']}")
                schema_fqn = self.config.catalog_gold_schema_fqns[product.id]
                self.ensure_database_schema(schema_fqn, domain_entity)
                for table in product.gold_tables:
                    self.ensure_table(schema_fqn, table, product_tags)

                # A schema is a valid data-product asset, but a table does not
                # inherit `dataProducts` from its schema (no inheritDataProducts
                # call in TableRepository, 1.13.6), so attach the tables found
                # under the Gold schema. Silver inputs reach the product through
                # lineage, not membership.
                refs = self.list_schema_tables(schema_fqn)
                if not refs:
                    missing_assets.append(schema_fqn)
                    continue
                added = self._reconciler.attach_assets(product_body["name"], refs)
                self.progress.info(
                    f"OpenMetadata data product {product_body['name']} holds {len(refs)} asset(s); attached {added}"
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
