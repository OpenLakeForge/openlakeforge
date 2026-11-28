"""A read-only comparison of what Git declares with what OpenMetadata holds, for one stage.

It issues GET requests only. Each finding names the entity and the stage it
belongs to; domains and data products are shared by every stage and report as
`shared`. It covers the entities a deploy manages today, and reports what a
person must repair: nothing here is ever written or deleted (ownership guide).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openlakeforge_core import gold_role_tag

from .governance import DomainSpec, MetadataConfig, MetadataReconciler, domain_payload, product_payload
from .openmetadata import fqn_part
from .ownership import marker_of, patch_operations, tag_operations
from .product_graph import consumes_from_drift

MISSING = "missing"
STALE = "stale"
CHANGED = "changed"
UNRESOLVED = "unresolved"
SHARED = "shared"
_PAGE = 1000


@dataclass(frozen=True)
class DriftItem:
    kind: str
    entity: str
    fqn: str
    stage: str
    detail: str = ""

    def line(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.kind:<10} {self.stage:<7} {self.entity} {self.fqn}{suffix}"


@dataclass(frozen=True)
class _Declared:
    """One entity Git declares: where it lives and the fields a deploy keeps in step."""

    entity: str
    collection: str
    fqn: str
    stage: str
    owned: Mapping[str, Any]
    fields: str = "extension"
    tags: tuple[str, ...] = ()


def stage_of(config: MetadataConfig) -> str:
    return config.catalog_database.removeprefix("lakehouse_")


def _in_stage_buckets(config: MetadataConfig, fqn: str) -> bool:
    """Whether a container FQN sits under one of this stage's buckets. Every stage shares one storage service, so
    another stage's marked containers are not stale here."""
    buckets = {config.storage_bronze_bucket, config.storage_silver_bucket, config.storage_gold_bucket} - {""}
    return any(
        fqn == root or fqn.startswith(f"{root}.")
        for root in (f"{fqn_part(config.storage_service)}.{fqn_part(b)}" for b in buckets)
    )


def detect_drift(
    reconciler: MetadataReconciler,
    config: MetadataConfig,
    domains: Sequence[DomainSpec],
    containers: Sequence[Mapping[str, Any]],
) -> list[DriftItem]:
    """`containers` are the deployer's bucket and Bronze container specs (`name`, `parent_fqn`, `description`)."""
    stage = stage_of(config)
    items: list[DriftItem] = []
    declared: list[_Declared] = []
    domain_ids: dict[str, str] = {}

    for domain in domains:
        body = domain_payload(domain)
        entity = reconciler.lookup("domains", domain.name)
        if entity:
            domain_ids[domain.name] = entity["id"]
        declared.append(
            _Declared(
                "domain",
                "domains",
                domain.name,
                SHARED,
                {k: body[k] for k in ("displayName", "description", "domainType")},
            )
        )
        for product in domain.products:
            pbody = product_payload(product)
            owned = {k: pbody[k] for k in ("displayName", "description")}
            if entity:
                # A deploy sets a product's domain only when it creates it, so a product moved to another domain is
                # reported here for a person to repair rather than overwritten.
                owned["domains"] = [{"id": entity["id"], "type": "domain"}]
            declared.append(_Declared("data product", "dataProducts", product.id, SHARED, owned, "domains,extension"))
            if not entity:
                items.append(
                    DriftItem(UNRESOLVED, "data product", product.id, SHARED, f"domain {domain.name!r} not found")
                )
    for domain in domains:
        schemas: list[tuple[str, Sequence[str], Mapping[str, str]]] = [
            (config.catalog_silver_schema_fqns[domain.name], domain.silver_tables, {})
        ]
        schemas += [(config.catalog_gold_schema_fqns[p.id], p.gold_tables, p.gold_roles) for p in domain.products]
        for schema_fqn, tables, roles in schemas:
            owned = (
                {"domains": [{"id": domain_ids[domain.name], "type": "domain"}]} if domain.name in domain_ids else {}
            )
            declared.append(
                _Declared("database schema", "databaseSchemas", schema_fqn, stage, owned, "domains,extension")
            )
            declared += [
                _Declared(
                    "table",
                    "tables",
                    f"{schema_fqn}.{t}",
                    stage,
                    {},
                    "extension,tags" if t in roles else "extension",
                    (gold_role_tag(roles[t]),) if t in roles else (),
                )
                for t in tables
            ]
            database_fqn = schema_fqn.rpartition(".")[0]
            if reconciler.lookup("databases", database_fqn, "") is None:
                items.append(
                    DriftItem(UNRESOLVED, "database schema", schema_fqn, stage, f"database {database_fqn!r} not found")
                )
            elif domain.name not in domain_ids:
                items.append(
                    DriftItem(UNRESOLVED, "database schema", schema_fqn, stage, f"domain {domain.name!r} not found")
                )
    declared += [
        _Declared(
            "container",
            "containers",
            f"{c['parent_fqn'] or fqn_part(config.storage_service)}.{fqn_part(c['name'])}",
            stage,
            {"description": c["description"]},
        )
        for c in containers
    ]

    for want in declared:
        current = reconciler.lookup(want.collection, want.fqn, want.fields)
        if current is None:
            items.append(DriftItem(MISSING, want.entity, want.fqn, want.stage))
            continue
        fields = [op["path"].lstrip("/") for op in patch_operations(current, want.owned)]
        # Only a missing tag is drift: a person's other tags, and a role OLF no longer derives, are theirs to keep.
        held_tags = {tag["tagFQN"] for tag in current.get("tags") or []}
        if any(op["op"] == "add" for op in tag_operations(current, want.tags)):
            fields.append(f"tags (missing {', '.join(sorted(set(want.tags) - held_tags))})")
        if marker_of(current) is None:
            fields.append("olfManaged")
        if fields:
            items.append(DriftItem(CHANGED, want.entity, want.fqn, want.stage, ", ".join(fields)))

    for domain in domains:
        for product in domain.products:
            items += [
                DriftItem(UNRESOLVED, "gold model", problem, stage, "no resolvable role")
                for problem in product.role_problems
            ]
            items += [
                DriftItem(CHANGED, "data product", product.id, SHARED, detail)
                for detail in consumes_from_drift(reconciler, product.id, product.consumes_from)
            ]
            # The tables a deploy would attach: those under the product's Gold schema that it does not hold yet.
            held_assets = reconciler.held_asset_ids(product.id)
            if held_assets is None:
                # Never read as clean what could not be read: a repair needs to know the check did not run.
                detail = "assets: the product's asset listing could not be read"
                items.append(DriftItem(UNRESOLVED, "data product", product.id, SHARED, detail))
                continue
            unattached = [
                ref
                for ref in reconciler.list_schema_tables(config.catalog_gold_schema_fqns[product.id])
                if ref["id"] not in held_assets
            ]
            if unattached:
                detail = f"assets: {len(unattached)} table(s) of its Gold schema are not attached"
                items.append(DriftItem(CHANGED, "data product", product.id, SHARED, detail))

    held = {(want.collection, want.fqn) for want in declared}

    def stale_entities(collection: str, label: str, params: dict[str, str], item_stage: str) -> list[str]:
        found_fqns: list[str] = []
        for found in reconciler.client.list_entities(f"/api/v1/{collection}", {"fields": "extension", **params}, _PAGE):
            fqn = found.get("fullyQualifiedName") or found["name"]
            if collection == "containers" and not _in_stage_buckets(config, fqn):
                continue
            if marker_of(found) and (collection, fqn) not in held:
                found_fqns.append(fqn)
                items.append(
                    DriftItem(STALE, label, fqn, item_stage, "marked as OpenLakeForge-managed, absent from Git")
                )
        return found_fqns

    stale_entities("domains", "domain", {}, SHARED)
    stale_entities("dataProducts", "data product", {}, SHARED)
    stale_schemas = stale_entities(
        "databaseSchemas", "database schema", {"database": config.catalog_database_fqn}, stage
    )
    stale_entities("containers", "container", {"service": config.storage_service}, stage)
    # A schema removed from Git still holds the tables a deploy registered in it, so those are searched too.
    declared_schemas = [w.fqn for w in declared if w.collection == "databaseSchemas"]
    for schema_fqn in [*declared_schemas, *stale_schemas]:
        stale_entities("tables", "table", {"databaseSchema": schema_fqn}, stage)
    return sorted(items, key=lambda item: (item.stage, item.entity, item.fqn, item.kind))
