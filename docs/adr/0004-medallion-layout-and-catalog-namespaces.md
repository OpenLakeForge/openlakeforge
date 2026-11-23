# ADR 0004: Medallion layout, ownership, and catalog namespaces

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS ✔ (suffixed bucket names, D11) · Azure ✔ |

## Context

User code was once organised by domain: `domains/<domain>/` held that domain's
Bronze, Silver, Gold and reports, and every namespace followed the product
(`{asset_prefix}_silver`, `{asset_prefix}_gold`). That made the product the
owner of all three medallion layers, which is not how the medallion
architecture divides ownership. Bronze is a raw copy of an external source
that several domains reuse, so nesting it under one domain ingested the same
feed twice. Silver holds a domain's validated entities, so keying it on the
product gave two products of one domain two Silver namespaces for the same
table. Only Gold is product-owned. Storage has the same shape problem: IAM,
lifecycle, retention and replication apply per bucket, not per path prefix.

## Decision

**D1. `lakehouse_code/` is laid out by medallion layer, and each layer names
its owner.** The domain-oriented layout is removed, not deprecated:
`domains/`, `domain.yaml` and `asset_prefix` do not exist. Enforced by:
`olf check contracts` (`_check_governance_conformance` in
`tools/olf/olf/common/contracts_check/_governance.py`), and
`_refuse_per_product_dbt_projects` in
`tools/olf/olf/common/commands/checks/_shared.py`.

```text
lakehouse_code/
├── bronze/<source>/          source-owned: source.yaml + dlt extract
├── silver/floe.yml, sources.yml   the Floe project and its named Bronze sources
├── silver/<domain>/          domain-owned: Floe domain defaults and one file per entity
├── gold/dbt_project.yml, packages.yml, macros/   the dbt project and what every product shares
├── gold/models/_sources/<domain>.yml   each domain's Silver tables, declared once
├── gold/models/<product>/    product-owned: dbt models and their schema.yml
├── dashboards/superset/<dashboard>/   consumption-owned: exported report bundles
├── pipelines/                user-maintained orchestration: definitions.py, products.py
└── governance.yml            the descriptor (ADR 0005)
```

**D2. A source owns raw Bronze files, a domain owns `<domain>_silver`, a
product owns `<product>_gold`.** Bronze holds raw files, not Iceberg tables,
so it has no catalog namespace; a source is ingested once and any domain
reads it. A domain validates its sources into one Silver namespace. A product
reads Silver and owns only its Gold namespace. Product `id` is unique across
the lakehouse, so `<product>_gold` needs no domain prefix. Which namespace a
table belongs to is ADR 0005 D2. Enforced by: `Domain.silver_namespace` and
`Product.gold_namespace` in `packages/core/openlakeforge_core/inventory.py`.

**D3. Gold is one dbt project at `lakehouse_code/gold/`, with one folder per
product under `models/`.** It mirrors Silver, one Floe project with a folder
per domain. The folder name is the product `id` from `governance.yml`.
`dbt_project.yml` (materialization, `on_table_exists`, `view_security`),
`packages.yml`, `macros/` and `models/_sources/` are shared by every product;
`models/<product>/*.sql` and its `schema.yml` belong to one product. Enforced
by: `olf check contracts`, which requires each folder to match a product.

**D4. A Gold dbt source is named after its domain.** `source('sales',
'orders')` reads `<prefix>sales_silver.orders`. Each Silver table is declared
once, in `models/_sources/<domain>.yml`, and no product redeclares it. Source
and table names are the domain and entity names, which are also the Dagster
asset key of the Silver table. Enforced by: `lakehouse_code/gold/models/_sources/`
and the asset-key mapping in `packages/runtime/openlakeforge_runtime/product_dagster.py`.

**D5. A Gold model's schema comes from its folder.** `generate_schema_name`
in `openlakeforge_dbt` names every model's schema `<prefix><product>_gold`,
where `<product>` is the model's folder under `models/`. dbt takes the macro
only from the root project, so the Gold project's
`macros/generate_schema_name.sql` delegates to the package's. A model outside
a product folder fails to parse, a `schema` config naming any other schema is
rejected (ADR 0005 D2), and the profile's target schema is never written to.
Enforced by:
`packages/runtime/openlakeforge_runtime/dbt/openlakeforge_dbt/macros/generate_schema_name.sql`.

**D6. A product is a dbt group, and its models are private by default.** Each
product's `schema.yml` declares a group named after the product, and
`dbt_project.yml` defaults every model to `access: private`, so dbt refuses a
`ref()` to another product's model unless that model is `access: public`.
Without a published mart a product reads only Silver. A `ref()` to a public
mart is a Dagster asset dependency; the reading product's job does not
rebuild it. Enforced by: `+access: private` in
`lakehouse_code/gold/dbt_project.yml`.

**D7. Dagster selects each product out of one manifest.** The image parses the
Gold project once, and each product's assets are
`dbt_assets(select="group:<product>")` over that manifest. A product's Silver
inputs are the sources its models read (none for a product that reads only
public marts), its job builds only its own models, and its run artifacts keep
their per-product prefix. Enforced by: `ProductDefinitionSpec` in
`packages/runtime/openlakeforge_runtime/product_dagster.py`.

**D8. Catalog namespaces are flat.** A namespace is a single-level name, not
a hierarchy: the shape Polaris and AWS Glue both implement natively, so the
same names work on either catalog without a translation layer. Namespaces are
derived from descriptors and reconciled in the artifacts phase by `olf infra
catalog sync-namespaces` (ADR 0002), never templated into Terraform. Enforced
by: `resolve_physical_names` in `packages/core/openlakeforge_core/inventory.py`.

**D9. Each medallion layer has its own bucket per stage, named
`<prefix>-<stage>-<layer>`.** Buckets are the unit IAM, lifecycle, retention
and replication apply to, so one bucket per layer lets BI read Gold without
raw Bronze and gives raw files their own retention. `<prefix>` is the
Deployment Profile's `metadata.name`, validated as a bucket component (at most
51 characters, so `<prefix>-prod-bronze` fits in 63); there is no separate
prefix override. The ops bucket is `<prefix>-ops`. The names are derived once
by `DeploymentTopology.bucket_names()` and passed to each root as
`bucket_names`, never rebuilt in a root, and a provider contract whose bucket
names differ is rejected. Table paths carry no `bronze/`, `silver/` or `gold/`
prefix, because the bucket implies the layer. Enforced by:
`tools/olf/olf/common/profile.py` and the bucket check in
`tools/olf/olf/common/provider_contracts/_v3.py`.

**D10. A deployed bucket is never renamed implicitly.** A rename would make
Terraform create an empty bucket while Polaris keeps the old locations, so
`olf infra deploy` refuses it; the opt-in is `--allow-stage-removal`. Enforced
by: `require_no_bucket_rename` in
`tools/olf/olf/infra/deployment/local/platform.py`.

**D11. AWS keeps a random suffix on its bucket names.** S3 names are global
across accounts, and replacing a bucket on AWS deletes its objects. AWS stage
buckets stay `<profile name>-<stage>-<layer>-<suffix>` with the suffix the
`aws-s3` module appends, and the ops bucket stays
`<bucket_name_prefix>-ops-<suffix>` (a root variable, not `metadata.name`).
Existing AWS names do not change. Enforced by:
`infra/terraform/modules/storage/aws-s3/tests/bucket_names.tftest.hcl`.

**D12. Stage catalogs allow drop with purge, bounded to the stage's own
buckets.** Each `lakehouse_<stage>` catalog carries
`polaris.config.drop-with-purge.enabled=true`, set per catalog rather than
realm-wide, so it covers only stage catalogs. Trino drops tables with purge
and Polaris drops a view only with purge; without the property every dbt
incremental model fails on its second run (#741). Purge stays in one stage: a
stage catalog's `allowedLocations` are its own three buckets. Who may drop is
unchanged: it still needs the stage principals' catalog grants. Enforced by:
`DROP_WITH_PURGE` in `tools/olf/olf/infra/polaris_bootstrap.py`, which also
sets it on a catalog that already exists.

**D13. The ops bucket sits outside the medallion split and uses stable
prefixes.** Floe run reports are keyed by domain (`floe/reports/<domain>/`),
matching domain-owned Silver; dbt run artifacts by domain and product
(`run-artifacts/dbt/<domain>/<product>/`); logs go under `logs/`. Once a stage
is activated they land under its own prefix, `activations/<stage>/`. The full
layout is in [Provider contracts](../architecture/provider-contracts.md#ops-bucket-layout).
Enforced by: `artifact_prefixes` and `dbt_artifact_prefix` in
`packages/core/openlakeforge_core/inventory.py`.

## Consequences

- Adding a product that reuses an existing source touches
  `gold/models/<product>/` and `governance.yml`; its Dagster job is derived
  from them, and `pipelines/products.py` gains an entry only to change its
  cadence or inputs. No Bronze or Silver work and no dbt configuration is
  duplicated.
- One Gold project costs isolation: a model that fails to parse fails `olf
  check dbt` and the image's manifest bake for every product, and each run
  parses the whole Gold project.
- Migrating from the `domains/` layout is a manual rewrite, with no parser for
  the old descriptor shape (ADR 0005 D7).

## Boundaries

| Not decided here | Where |
| --- | --- |
| Which namespace a table belongs to, and the descriptors | ADR 0005 |
| How products become Dagster jobs and code locations | ADR 0006 |
| AWS's prefixed Glue databases in one shared catalog | ADR 0010 D2 |
| Pod log collection, metrics and their stores | ADR 0022, ADR 0024 |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Domain-oriented `domains/<domain>/` with product-keyed namespaces | Bronze ingested twice per domain; two Silver namespaces for one domain's table |
| One bucket with a path prefix per layer | Cannot grant Gold without Bronze, or retain raw files differently from validated tables |
| One dbt project per product (`gold/<product>/dbt/`) | Each repeated the same configuration and redeclared shared Silver tables |
| A Dagster module per product (`pipelines/dagster/<product>.py`) | The modules differed only in names `governance.yml` already declares |
| Realm-wide `DROP_WITH_PURGE_ENABLED` | Would cover catalogs the bootstrap does not create for stages |

## History

- Undated: New record, merging the former ADRs 0013 (medallion bucket split),
  0014 (ops bucket and prefixes) and 0026 (medallion ownership and flat
  namespaces, which superseded ADR 0021's product-owned Silver).
- 2026-10-29 (#545): D2 changed: `governance.yml` names domains and products
  only; the schema decides which one a table belongs to.
- 2026-11-01 (#741): D12 added: stage catalogs allow drop with purge.
- 2026-11-07 (#993): D9 changed: local and Azure stage buckets are
  `<prefix>-<stage>-<layer>`, derived by the resolver (were
  `lakehouse-<stage>-<layer>`); D10 and D11 added.
- 2026-11-12 (#1099): D3 to D7 added: Gold is one dbt project with a folder per
  product, the schema comes from the folder, and products are private dbt
  groups.
- 2026-11-20 (#1339): D1 changed: `pipelines/products.py` replaces the per-product
  Dagster modules.
- 2026-11-23: Rewritten in the standard form; no decision changed.
