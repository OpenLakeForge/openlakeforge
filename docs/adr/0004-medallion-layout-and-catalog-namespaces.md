# ADR 0004: Medallion layout, ownership, and catalog namespaces

## Status

Binding.

## Context

The platform originally organised user code by domain: `domains/<domain>/`
contained that domain's Bronze, Silver, Gold, reports, and a `domain.yaml`
describing all of it. Every downstream namespace followed the product —
`{asset_prefix}_silver` and `{asset_prefix}_gold` — which made the *product* the
owner of all three medallion layers.

That does not match how the medallion architecture actually divides ownership,
and the mismatch had costs:

- **Bronze is not domain-owned.** It is a raw representation of an external
  source, reused by whatever domains need it. Nesting it under one domain meant
  ingesting the same CRM feed twice when two domains wanted different slices,
  duplicating both the extract and the storage.
- **Silver is not product-owned.** Validated entities are a domain asset. Keying
  the Silver namespace on `asset_prefix` gave two products in the same domain two
  separate Silver namespaces for the same conceptual table.
- **Only Gold is genuinely product-owned.**

## Decision

### `lakehouse_code/` replaces `domains/`

User code is organised by medallion layer, and each layer names its owner:

```text
lakehouse_code/
├── bronze/<source>/          source-owned: source.yaml + dlt extract
├── silver/floe.yml, sources.yml   the Floe project and its named Bronze sources
├── silver/<domain>/          domain-owned: Floe domain defaults and one file per entity
├── gold/dbt_project.yml, packages.yml, macros/   the dbt project and what every product shares
├── gold/models/_sources/<domain>.yml   each domain's Silver tables, declared once
├── gold/models/<product>/    product-owned: dbt models and their schema.yml
├── dashboards/superset/<dashboard>/   consumption-owned: exported report bundles
├── pipelines/dagster/        user-maintained orchestration
└── governance.yml            the descriptor (ADR 0005)
```

The domain-oriented layout is **removed**, not deprecated. `domains/`,
`domain.yaml`, and `asset_prefix` do not exist.

### Ownership rule

```text
Source   ->  raw files in the Bronze bucket (no catalog namespace)
Domain   ->  <domain>_silver
Product  ->  <product>_gold
```

Bronze holds raw source files, not Iceberg tables, so it has no catalog
namespace. A Bronze source is ingested once and referenced by any domain. A domain
validates its sources into one Silver namespace. A product consumes Silver
tables from its domain and owns only its Gold namespace. Product `id` is
unique across the whole lakehouse, so `<product>_gold` needs no domain prefix.

### Gold is one dbt project; a product is a folder in it

Gold has the same shape as Silver: one project for the whole lakehouse, and
an owner's folder inside it. Silver is one Floe project with a folder per
domain. Gold is one dbt project at `lakehouse_code/gold/` with a folder per
product under `models/`. The folder name is the product `id` from
`governance.yml`, and `olf check contracts` requires each one to match a
product.

| Shared by every product | Owned by one product |
| --- | --- |
| `dbt_project.yml`: materialization, `on_table_exists`, `view_security` | `models/<product>/*.sql` |
| `packages.yml`: `openlakeforge_dbt` and any other dbt package | `models/<product>/schema.yml`: descriptions, tests, the product's dbt `group` |
| `macros/`: the `generate_schema_name` wrapper, and project macros any product calls | |
| `models/_sources/<domain>.yml`: one dbt source per domain | |

**A source is named after its domain.** `source('sales', 'orders')` reads
`<prefix>sales_silver.orders`. Each Silver table is declared once, and a
product does not redeclare it. The source's name and table name are the
domain and entity names, which are also the Dagster asset key of the Silver
table.

**The schema comes from the folder.** `generate_schema_name` in
`openlakeforge_dbt` names every model's schema
`<prefix><product>_gold`, where `<product>` is the model's folder under
`models/`. dbt takes `generate_schema_name` only from the root project, so
the Gold project's `macros/generate_schema_name.sql` delegates to the
package's. A model outside a product folder fails to parse. A `schema` config
that names any other schema is still rejected by `olf check contracts`
(ADR 0005). The profile's target schema is never written to.

**A product is a dbt group, and its models are private by default.** Each
product's `schema.yml` declares a dbt `group` named after the product, and
`dbt_project.yml` defaults every model to `access: private`. dbt then refuses
a `ref()` from one product to another product's model unless that model is
marked `access: public`. Unless a product publishes a mart this way, it still
reads only Silver. A `ref()` to a public mart is an asset dependency in
Dagster. The reading product's job does not rebuild it.

**Dagster selects each product out of one manifest.** The image parses the
project once. Each product's assets are
`dbt_assets(select="group:<product>")` over that manifest. Its Silver
inputs are the sources its models read, not every source in the project, and
are empty for a product that reads only public marts. Its
job still builds only its own models. Run artifacts keep their per-product
prefix.

The namespace contract is **flat** — a single-level name, not a hierarchy. This
is deliberate: it is the shape both Polaris and AWS Glue implement natively, so
the identical namespace names work on either catalog with no translation layer.

### Per-layer buckets

Each medallion layer gets its own bucket rather than a path prefix inside one:

| Bucket (DEV) | Layer | Written by |
| --- | --- | --- |
| `<prefix>-dev-bronze` | Bronze | dlt |
| `<prefix>-dev-silver` | Silver | Floe |
| `<prefix>-dev-gold` | Gold | dbt-trino |
| `<prefix>-ops` | operational artifacts | the artifacts phase |

The naming rule is `<prefix>-<stage>-<layer>` on the self-hosted providers (local and Azure), where
`<prefix>` is the Deployment Profile's `metadata.name`. Bucket names are global per endpoint or account and
renaming one means migrating its data, so the name is derived once by the resolver
(`DeploymentTopology.bucket_names()`), passed to each Terraform root as the `bucket_names` variable, and never
rebuilt in a root. `metadata.name` is therefore validated as a bucket component: at most 51 characters, so
`<prefix>-prod-bronze` stays within 63. There is no separate prefix override in the profile; to share an object
store with other tenants, choose a distinctive `metadata.name`. The ops bucket is `<prefix>-ops`.

`olf infra deploy` refuses to rename a bucket that is already deployed (Terraform would create an empty one and
Polaris would keep the old locations); the opt-in is `--allow-stage-removal`. A provider contract whose bucket
names differ from the derived ones is rejected.

**AWS keeps a suffix.** S3 bucket names are global across accounts, so `<prefix>-<stage>-<layer>` can collide with a
bucket in another account, and replacing a bucket on AWS deletes its objects. On AWS the stage buckets stay
`<profile name>-<stage>-<layer>-<suffix>`, with the random suffix the `aws-s3` module appends, and the ops bucket
stays `<bucket_name_prefix>-ops-<suffix>` (a root variable, not `metadata.name`). DEV keeps the names of the v0.2
POC. Existing AWS names do not change; a Terraform test on the `aws-s3` module pins them.

Buckets are the unit at which IAM policies, lifecycle rules, retention, and
replication apply. A single bucket cannot grant BI consumers read access to Gold
without also exposing raw Bronze, and cannot give raw CSVs a different retention
policy from validated tables. Path prefixes inside one bucket cannot express
either.

Because the layer is implied by the bucket, table paths carry no `bronze/`,
`silver/`, or `gold/` prefix.

### Stage catalogs allow drop with purge

Each stage catalog (`lakehouse_<stage>`) is created with the property
`polaris.config.drop-with-purge.enabled=true`, and `olf`'s Polaris bootstrap
(`tools/olf/olf/infra/polaris_bootstrap.py`) sets it
on a catalog that already exists. Trino drops tables with purge, and Polaris
drops a view only with purge; without the property Polaris answers both with
HTTP 403. Every dbt incremental run after the first drops its
`<model>__dbt_tmp` view, so with purge off every incremental Gold model failed
on its second run (#741).

The property is set per catalog rather than as the realm-wide
`DROP_WITH_PURGE_ENABLED` feature, so it covers only the catalogs the bootstrap
creates for stages. Purge deletes the dropped table's files. That stays inside
one stage: a stage catalog's `allowedLocations` are that stage's own three
buckets, and Polaris refuses a table or view located outside them, so a purge
in `lakehouse_dev` cannot reach `<prefix>-prod-*`. Who may drop is unchanged:
dropping still needs the catalog grants the stage's principals hold.

### Operational artifact prefixes

`<prefix>-ops` is outside the medallion split and uses stable prefixes:

```text
floe/reports/<domain>/
logs/
run-artifacts/dbt/<domain>/<product>/
```

Floe run reports are keyed by domain, matching domain-owned Silver, and land
under the stage's own artifact prefix (`activations/<stage>/`) once a stage is
activated.

The local observability adapter is `observability.object_log_archive`: metrics
and tracing are disabled, and logs, Floe reports, and dbt run artifacts are
archived to object storage. A Loki/Grafana/Prometheus stack is a later
observability adapter, not a replacement for this baseline.

## Consequences

Adding a product that reuses an existing source touches
`lakehouse_code/gold/models/<product>/`, `governance.yml`, and one Dagster module at
`lakehouse_code/pipelines/dagster/<product>.py`; no Bronze or Silver work is
duplicated, and no dbt project configuration is copied.

A single Gold project costs some isolation. A model that fails to parse fails
`olf check dbt` and the image's manifest bake for every product, not just its
own. The code location already failed as a whole before this change, because
it baked every product's manifest at import, so a deployed stack is no worse
off. Each run also parses the whole Gold project, not just one product's
models.

Catalog namespaces are derived from descriptors, so they are reconciled in the
artifacts phase by `olf infra catalog sync-namespaces` (ADR 0002), never templated into
Terraform.

Migrating an installation from the `domains/` layout is a manual rewrite. There
is no automated path and no parser for the old descriptor shape (ADR 0005).

## History

Merges the decisions previously recorded as ADR 0013 (medallion bucket split),
0014 (ops artifact bucket and prefixes), and 0026 (medallion ownership and the
flat catalog namespace contract, which superseded ADR 0021's product-owned
Silver).

Bucket names follow `lakehouse-<stage>-<layer>` for every stage, matching the
`lakehouse_<stage>` catalogs. DEV previously kept the v0.2 names
(`lakehouse-bronze`, `-silver`, `-gold`) and the other stages used
`<profile>-<stage>-<layer>`; the local and Azure environments now use the single scheme,
and the AWS environment is still on the old one.

2026-10-28 (#533): Floe 0.8 builds its manifests in memory in the code
location, so `floe/manifests/` is no longer written; the Silver contracts
became one multi-file Floe project.

2026-10-29 (#545): the descriptor is `governance.yml`, which names domains and
products only. A product no longer lists its Silver inputs or Gold tables; the
schema (`<domain>_silver`, `<product>_gold`) decides which domain or product a
table belongs to.

2026-11-01 (#741): stage catalogs allow drop with purge, so a dbt incremental
model's temporary view can be dropped.

2026-11-06 (#562): the stage catalogs are created by `olf`'s Polaris bootstrap
after the platform apply; the Terraform Job that did it is deleted.

2026-11-07 (#993): stage buckets are `<prefix>-<stage>-<layer>` on local, AWS
and Azure, derived by the resolver; they were `lakehouse-<stage>-<layer>` on
local and Azure and suffixed or legacy names on AWS.

2026-11-12: Gold is one dbt project at `lakehouse_code/gold/`, with a folder
per product under `models/`. Before this, each product had its own dbt project
at `gold/<product>/dbt/`. Those projects differed only in name, so each
repeated the same configuration and its own `packages.yml`. Each also declared
the Silver tables it read under a source named `silver`, so a table two
products read was declared twice. The Gold schema used to come from each
product's dbt profile. It now comes from the model's folder, and products are
dbt groups whose models are private by default.
