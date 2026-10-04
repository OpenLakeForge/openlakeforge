# Build your first data product

This tutorial walks through adding a new data product to OpenLakeForge and running it end to end.

By the end, you will have:

```text
CSV source
   ↓
dlt ingestion
   ↓
Bronze
   ↓
Floe validation
   ↓
Silver Iceberg
   ↓
dbt-trino
   ↓
Gold Iceberg
   ↓
Trino
```

with the complete pipeline orchestrated by Dagster.

The example adds a new `marketing` domain and a new `marketing_platform` Bronze source,
containing one data product:

```text
campaign_performance
```

with one source resource:

```text
campaigns.csv
```

and one Gold mart:

```text
mart_campaign_performance
```

---

# Fast path: the scaffold

`olf` generates this exact file tree for you. From an initialized installed
project, run:

```bash
olf source new marketing_platform --resource campaigns
olf product new marketing/campaign_performance \
  --input marketing_platform/campaigns \
  --gold-table mart_campaign_performance
```

`olf product new` creates the `marketing` domain inline here because it does not exist
yet. Each command validates its result against the canonical model before writing
anything, and writes nothing at all if validation fails.

A domain can also be created up front, ahead of any product — useful when you know the
Silver tables you want before you know what will consume them:

```bash
olf source new workday --resource employees
olf domain new hr --input workday/employees
# ... later, once you know what product to build:
olf product new hr/headcount \
  --silver-input employees --gold-table mart_headcount_by_org
```

Run `olf source new --help` (and the `domain`/`product` equivalents) for the
full flag reference, including `--with-report` to also scaffold a Superset
report skeleton. In a contributor checkout, prefix the same commands with
`uv run --project tools/olf`.

The rest of this tutorial builds the same `campaign_performance` product by hand, file
by file. Read it to understand *what* the scaffold generates and *why* — the file tree
and ownership model are identical either way, but the **content** is not: the scaffold
can't invent business logic, so it writes a one-column placeholder example CSV
(`campaigns_id`), a Floe contract inferred from that same placeholder (`campaigns_id`
as the sole, required column), and a Gold model that is a literal `select * from` its
Silver source with a `# TODO` to fill in the real transformation. The hand-built
walkthrough below uses real columns (`channel`, `spend`, `conversions`, ...) and a real
aggregation, and later steps — notably [step 17's query](#17-query-the-gold-table),
which selects a `channel` column — assume that real schema.

If you took the fast path and want to continue into this real schema, **replace the
example CSV before running `product new`**: the Floe contract's columns are inferred
from `lakehouse_code/bronze/marketing_platform/examples/campaigns.csv` at the moment
the domain is created, so swapping it in afterward leaves a contract that still expects
`campaigns_id` and rejects the real data. Run `source new`, replace that CSV with
[step 4](#4-add-some-source-data)'s version below, *then* run `product new`; afterward,
still replace the generated `dbt/models/gold/*.sql` with [step 9](#9-create-the-gold-model)'s
aggregation. Otherwise, treat the rest of the tutorial as a reference rather than
commands to run verbatim against the scaffolded output.

---

# Before you start

This tutorial assumes you already have the local **Slim** environment running.

If not:

```bash
olf deploy --provider local
```

The preset comes from `openlakeforge.yaml`, which `olf init` writes as Slim.
From a contributor checkout, where `olf` is not on `PATH`, run the same
deployment through `uv` instead: `uv run --project tools/olf --locked olf
deploy --provider local`. Its `openlakeforge.yaml` resolves the profile the
same way.

See the [local installation guide](../setup/local.md) for the complete setup.

Slim is recommended for this tutorial because it contains the entire data-engineering path without the optional OpenMetadata and Superset services.

---

# How OpenLakeForge organizes data products

User code lives under:

```text
lakehouse_code/
```

Ownership follows the medallion layer, not one vertical per domain (see
[ADR 0004](../adr/0004-medallion-layout-and-catalog-namespaces.md)):

```text
Bronze = source-aligned    lakehouse_code/bronze/<source>/
Silver = domain-aligned    lakehouse_code/silver/<domain>/
Gold   = product-aligned   lakehouse_code/gold/<product>/
Dashboards = consumption-aligned  lakehouse_code/dashboards/superset/<dashboard>/
Pipelines  = user-maintained orchestration  lakehouse_code/pipelines/dagster/
```

For example, the seed data already shows two products sharing one domain's Silver
namespace:

```text
lakehouse_code/
├── bronze/
│   ├── crm/            ← source: shared by both Sales products
│   └── erp/
├── silver/
│   ├── sales/           ← domain: order_revenue AND customer_health both validate into here
│   └── supply_chain/
├── gold/
│   ├── order_revenue/
│   ├── customer_health/
│   └── inventory_reliability/
└── lakehouse.yaml
```

For this tutorial:

```text
source  = marketing_platform
domain  = marketing
product = campaign_performance
```

OpenLakeForge derives platform identities from those logical names:

```text
Dagster job
campaign_performance_pipeline

Bronze namespace
marketing_platform_bronze

Silver namespace
marketing_silver

Gold namespace
campaign_performance_gold
```

Silver is domain-owned: if you later add a second `marketing` product, it shares
`marketing_silver` rather than getting its own namespace. You do **not** create those
namespaces manually and you do **not** modify Terraform to register a product.

---

# 1. Create the source, domain, and product directories

From the repository root:

```bash
mkdir -p lakehouse_code/bronze/marketing_platform/dlt
mkdir -p lakehouse_code/silver/marketing/contracts/floe
mkdir -p lakehouse_code/gold/campaign_performance/dbt/models/gold

touch lakehouse_code/bronze/marketing_platform/dlt/__init__.py
touch lakehouse_code/silver/marketing/__init__.py
touch lakehouse_code/gold/campaign_performance/__init__.py
```

Your new code should now look like:

```text
lakehouse_code/
├── bronze/
│   └── marketing_platform/
│       ├── source.yaml
│       ├── examples/
│       │   └── campaigns.csv
│       └── dlt/
│           ├── __init__.py
│           └── marketing_platform.py
│
├── silver/
│   └── marketing/
│       ├── __init__.py
│       └── contracts/
│           └── floe/
│               └── marketing.yml
│
└── gold/
    └── campaign_performance/
        ├── __init__.py
        └── dbt/
            └── models/
                └── gold/
```

---

# 2. Declare the Bronze source

Create:

```text
lakehouse_code/bronze/marketing_platform/source.yaml
```

with:

```yaml
apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: marketing_platform
displayName: Marketing Platform
description: Marketing campaign performance source system.
status: active
resources:
  - name: campaigns
    description: Raw CSV marketing campaign performance data.
```

`source.yaml` is the **logical source of truth** for one Bronze source: it declares
which resources the source exposes, without naming Polaris, Glue, S3, or any other
provider-specific infrastructure. Its `name` must match the directory it lives in
(`marketing_platform`).

---

# 3. Declare the domain and product in `lakehouse.yaml`

Edit the repository-wide:

```text
lakehouse_code/lakehouse.yaml
```

Add `marketing_platform` to `sources`, and a new `marketing` domain with the
`campaign_performance` product:

```yaml
sources:
  - crm
  - erp
  - marketing_platform

domains:
  # ...existing sales, supply_chain domains...
  - name: marketing
    displayName: Marketing
    description: Marketing analytics data products.
    status: active
    silver_tables:
      tables:
        - name: campaigns
          source: marketing_platform
          resource: campaigns
          description: Validated marketing campaigns.
    products:
      - id: campaign_performance
        displayName: Campaign Performance
        description: Campaign spend and conversion performance by channel.
        status: active
        silver_inputs: [campaigns]
        gold_tables:
          tables:
            - name: mart_campaign_performance
              description: Aggregated campaign performance by channel.
```

`lakehouse.yaml` tells OpenLakeForge:

* which sources, domains, and products exist
* which Source resources map to domain Silver tables
* which Silver tables each product consumes
* which Gold tables the product should produce

The descriptor must remain provider-neutral: no Polaris, Glue, SeaweedFS, S3 endpoints,
Kubernetes names, or other environment-specific infrastructure. Those values are
resolved by OpenLakeForge provider contracts.

---

# 4. Add some source data

Create:

```text
lakehouse_code/bronze/marketing_platform/examples/campaigns.csv
```

with:

```csv
campaign_id,campaign_date,channel,spend,conversions
cmp_001,2026-08-01,search,120.50,18
cmp_002,2026-08-01,social,80.00,9
cmp_003,2026-08-02,search,150.00,24
cmp_004,2026-08-02,email,30.00,12
cmp_005,2026-08-03,social,95.50,11
```

The repository's current golden-path ingestion helper uses local CSV examples. The same
OpenLakeForge product model can support other dlt sources, but this tutorial
deliberately uses the simplest existing ingestion path.

---

# 5. Create the Bronze loader

Create:

```text
lakehouse_code/bronze/marketing_platform/dlt/marketing_platform.py
```

with:

```python
from __future__ import annotations

from pathlib import Path

from libs.bronze_csv import BronzeLoadResult, load_entities_to_bronze

MARKETING_PLATFORM_ENTITIES = ("campaigns",)

_SOURCE_DIR = Path(__file__).resolve().parents[1]
_RAW_DIR = _SOURCE_DIR / "examples"
_BRONZE_PREFIX = "marketing_platform"


def load_marketing_platform_entities_to_bronze(
    entities: tuple[str, ...],
    raw_dir: Path | None = None,
) -> dict[str, BronzeLoadResult]:
    """Load a subset of Marketing Platform resources into Bronze under the ``marketing_platform`` prefix."""
    return load_entities_to_bronze(
        entities=entities,
        raw_dir=raw_dir or _RAW_DIR,
        bronze_prefix=_BRONZE_PREFIX,
    )


def load_all_entities_to_bronze(raw_dir: Path | None = None) -> dict[str, BronzeLoadResult]:
    """Load every resource declared in ``bronze/marketing_platform/source.yaml``."""
    return load_marketing_platform_entities_to_bronze(MARKETING_PLATFORM_ENTITIES, raw_dir=raw_dir)
```

The loader reads `campaigns.csv` through dlt, writes it to the configured Bronze object
store under the source's own prefix, and returns metadata about the loaded entity to
Dagster. Because loading is keyed by *source*, a second product that also needs
`marketing_platform.campaigns` calls the same function instead of duplicating the
ingestion definition.

---

# 6. Define the Bronze → Silver contract

Create:

```text
lakehouse_code/silver/marketing/contracts/floe/marketing.yml
```

with:

```yaml
version: "0.2"

metadata:
  project: "openlakeforge"
  owner: "marketing"
  description: "Marketing Bronze to Silver Floe contracts."
  tags: ["marketing", "silver"]

storages:
  default: "lakehouse_bronze"
  definitions:
    - name: "lakehouse_bronze"
      type: "s3"
      bucket: "{{OPENLAKEFORGE_STORAGE_BRONZE_BUCKET}}"
      region: "{{OPENLAKEFORGE_STORAGE_REGION}}"
    - name: "lakehouse_silver"
      type: "s3"
      bucket: "{{OPENLAKEFORGE_STORAGE_SILVER_BUCKET}}"
      region: "{{OPENLAKEFORGE_STORAGE_REGION}}"
    - name: "openlakeforge_ops"
      type: "s3"
      bucket: "{{OPENLAKEFORGE_OPS_BUCKET_NAME}}"
      region: "{{OPENLAKEFORGE_STORAGE_REGION}}"

report:
  path: "floe/reports/marketing"
  storage: "openlakeforge_ops"

entities:
  - name: "campaigns"
    incremental_mode: "none"
    source:
      format: "csv"
      path: "marketing_platform/campaigns"
      storage: "lakehouse_bronze"
      options: { header: true, separator: ",", encoding: "utf-8", glob: "*.csv" }
      cast_mode: "strict"
    sink:
      write_mode: "overwrite"
      accepted:
        format: "iceberg"
        path: "marketing/campaigns"
        storage: "lakehouse_silver"
        iceberg: { catalog: "iceberg_catalog", namespace: "marketing_silver", table: "campaigns" }
      rejected: { format: "csv", path: "floe/rejected/marketing/campaigns", storage: "lakehouse_silver" }
    policy: { severity: "reject" }
    schema:
      normalize_columns: { enabled: true, strategy: "snake_case" }
      primary_key: ["campaign_id"]
      columns:
        - { name: "campaign_id", type: "string", nullable: false }
        - { name: "campaign_date", type: "date", nullable: false }
        - { name: "channel", type: "string", nullable: false }
        - { name: "spend", type: "number", nullable: false }
        - { name: "conversions", type: "integer", nullable: false }
```

Floe now owns the technical transition from Bronze to Silver Iceberg. Rows satisfying
the contract are written to:

```text
marketing_silver.campaigns
```

Notice the namespace is `marketing_silver` — the **domain**, not the
product. A domain has exactly one Floe configuration and one generated
manifest. When a second `marketing` product needs another Silver table, add
that entity to this existing `marketing.yml`; do not create a product-specific
Floe configuration. Both products then consume tables from the same
`marketing_silver` namespace.

Rejected rows are written to the domain's rejection path instead of silently
entering Silver.

---

# 7. Create the dbt project

Create:

```text
lakehouse_code/gold/campaign_performance/dbt/dbt_project.yml
```

with:

```yaml
name: campaign_performance
version: "1.0.0"
config-version: 2

profile: campaign_performance

model-paths: ["models"]
macro-paths: []
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  campaign_performance:
    +database: "{{ env_var('OPENLAKEFORGE_CATALOG_NAME', 'lakehouse_dev') }}"
    +materialized: table
    +on_table_exists: replace
    +meta:
      dagster:
        group: campaign_performance
    gold:
      +tags: ["marketing", "campaign_performance", "gold"]
```

OpenLakeForge renders the environment-specific dbt `profiles.yml` automatically. You
should therefore **not hardcode Trino hosts or cloud-specific catalog settings into the
dbt project**.

---

## Add the shared OpenLakeForge dbt package

Create:

```text
lakehouse_code/gold/campaign_performance/dbt/packages.yml
```

with:

```yaml
packages:
  - local: ../../../../libs/dbt/openlakeforge_dbt
```

---

# 8. Declare the Silver source

Create:

```text
lakehouse_code/gold/campaign_performance/dbt/models/sources.yml
```

with:

```yaml
version: 2

sources:
  - name: silver
    database: "{{ env_var('OPENLAKEFORGE_CATALOG_NAME', 'lakehouse_dev') }}"
    schema: marketing_silver
    description: Floe-owned Campaign Performance Silver Iceberg tables.
    tables:
      - name: campaigns
        description: Validated marketing campaign data.
```

dbt will consume the table created by Floe rather than owning a second staging layer.
The ownership boundary is:

```text
Bronze  → source-owned, dlt
Silver  → domain-owned, Floe
Gold    → product-owned, dbt
```

Notice `schema: marketing_silver` matches the domain namespace from step 6, not a
product-scoped name.

---

# 9. Create the Gold model

Create:

```text
lakehouse_code/gold/campaign_performance/dbt/models/gold/mart_campaign_performance.sql
```

with:

```sql
with campaigns as (
    select
        campaign_id,
        cast(campaign_date as date) as campaign_date,
        channel,
        cast(spend as double) as spend,
        cast(conversions as bigint) as conversions
    from {{ source('silver', 'campaigns') }}
)

select
    channel,
    cast(count(*) as bigint) as campaign_count,
    cast(sum(spend) as double) as total_spend,
    cast(sum(conversions) as bigint) as total_conversions
from campaigns
group by channel
```

This model will be materialized as an Iceberg table in:

```text
campaign_performance_gold.mart_campaign_performance
```

The Gold namespace is product-owned — no domain prefix.

---

## Document the Gold model

Create:

```text
lakehouse_code/gold/campaign_performance/dbt/models/gold/schema.yml
```

with:

```yaml
version: 2

models:
  - name: mart_campaign_performance
    description: Campaign spend and conversions aggregated by channel.
```

---

# 10. Create the Dagster product definition

Create:

```text
lakehouse_code/pipelines/dagster/campaign_performance.py
```

with:

```python
from __future__ import annotations

from libs.product_dagster import ProductDefinitionSpec, build_product_definitions

CAMPAIGN_PERFORMANCE_SILVER_INPUTS = ("campaigns",)
CAMPAIGN_PERFORMANCE_GOLD_ASSETS = ("mart_campaign_performance",)


defs = build_product_definitions(
    ProductDefinitionSpec(
        domain="marketing",
        product="campaign_performance",
        silver_inputs=CAMPAIGN_PERFORMANCE_SILVER_INPUTS,
        bronze_inputs=(("marketing_platform", "campaigns"),),
        gold_assets=CAMPAIGN_PERFORMANCE_GOLD_ASSETS,
    )
)
```

This produces the Dagster job:

```text
campaign_performance_pipeline
```

and connects the three stages of the product: Bronze assets → Floe Silver assets → dbt
Gold assets. `lakehouse_code/definitions.py` (already in the repository) discovers this
module automatically by scanning `lakehouse_code/pipelines/dagster/` — you do **not**
need to register it anywhere.

---

# 11. Review the completed product

Your new code should now contain approximately:

```text
lakehouse_code/
├── lakehouse.yaml                                     (edited)
├── bronze/
│   └── marketing_platform/
│       ├── source.yaml
│       ├── examples/
│       │   └── campaigns.csv
│       └── dlt/
│           ├── __init__.py
│           └── marketing_platform.py
├── silver/
│   └── marketing/
│       ├── __init__.py
│       └── contracts/
│           └── floe/
│               └── marketing.yml
├── gold/
│   └── campaign_performance/
│       ├── __init__.py
│       └── dbt/
│           ├── dbt_project.yml
│           ├── packages.yml
│           └── models/
│               ├── sources.yml
│               └── gold/
│                   ├── mart_campaign_performance.sql
│                   └── schema.yml
└── pipelines/
    └── dagster/
        └── campaign_performance.py
```

You did **not** modify:

```text
Terraform
Helm
lakehouse_code/definitions.py
Dagster deployment configuration
catalog configuration
cloud configuration
```

Those platform concerns are derived from the lakehouse inventory and provider contracts.

---

# 12. Validate the descriptor

Run:

```bash
olf check contracts
```

Among other platform contract checks, this validates `lakehouse_code/lakehouse.yaml` and
every `lakehouse_code/bronze/*/source.yaml` against the canonical OpenLakeForge model and
JSON Schema.

If a descriptor contains an invalid identifier, missing field, duplicate product
identity, unresolved source reference, or invalid API version, validation fails before
deployment.

For example, identifiers such as source `name` and product `id` use:

```text
^[a-z][a-z0-9_]*$
```

so prefer `campaign_performance` rather than `campaign-performance`.

---

# 13. Validate dbt

Run:

```bash
olf check dbt
```

This discovers every product dbt project under `lakehouse_code/gold/*/dbt` and validates
that it compiles against the OpenLakeForge contracts.

For this product, OpenLakeForge expects:

```text
Silver:
lakehouse_dev.marketing_silver.*

Gold:
lakehouse_dev.campaign_performance_gold.*
```

The exact catalog implementation can differ between environments while those logical
relations remain stable.

---

# 14. Generate and validate the Floe manifest

Floe manifests are generated from each domain contract.

Run:

```bash
olf floe generate-manifests
```

OpenLakeForge discovers:

```text
lakehouse_code/silver/*/contracts/floe/*.yml
```

automatically. For this product it generates:

```text
lakehouse_code/silver/marketing/contracts/floe/manifests/marketing.manifest.json
```

Do not hand-author the generated manifest. The artifact deployment workflow also
generates the immutable runtime version used by Dagster and the Floe runner.

---

# 15. Deploy the new product

Because the platform is already running, you do **not** need to recreate Kubernetes or
reinstall the platform.

For Slim:

```bash
olf deploy --provider local --profile slim --phase artifacts
```

This performs the product-aware deployment phase:

```text
lakehouse.yaml
     ↓
lakehouse inventory
     ↓
reconcile catalog namespaces
     ↓
generate Floe runtime manifests
     ↓
publish runtime artifacts
     ↓
build project-code image
     ↓
load image into kind
     ↓
restart Dagster code
```

The new namespaces are created automatically:

```text
marketing_platform_bronze
marketing_silver
campaign_performance_gold
```

No Terraform product registration is required.

---

# 16. Run the pipeline

Open Dagster (run `olf access trust` once first if your browser warns about
the certificate):

```text
https://dagster.dev.olf.localhost
```

In Dagster, look for:

```text
campaign_performance_pipeline
```

Launch the job. Dagster should execute:

```text
campaigns.csv
     ↓
marketing_platform.campaigns_source
     ↓
dlt → Bronze
     ↓
Floe → campaigns
     ↓
Silver Iceberg (marketing_silver.campaigns)
     ↓
dbt-trino
     ↓
mart_campaign_performance
     ↓
Gold Iceberg (campaign_performance_gold.mart_campaign_performance)
```

You can inspect every step from the Dagster asset graph.

---

# 17. Query the Gold table

Once the pipeline succeeds, query the result through Trino.

From the repository root:

```bash
KUBECONFIG=.tmp/kubeconfigs/local.yaml \
kubectl --context kind-openlakeforge-local \
exec -n olf-system deploy/trino-coordinator -- \
trino --execute "
SELECT *
FROM iceberg.campaign_performance_gold.mart_campaign_performance
ORDER BY channel
"
```

You should see one row per marketing channel with its campaign count, spend, and
conversions.

You can also inspect the tables:

```sql
SHOW TABLES
FROM iceberg.marketing_silver;
```

and:

```sql
SHOW TABLES
FROM iceberg.campaign_performance_gold;
```

---

# 18. Run the complete Slim validation

Finally:

```bash
olf e2e run --env local --suite full
```

The E2E suite discovers products from the lakehouse inventory. Your new product
therefore becomes part of the validation automatically. The suite will:

1. verify the descriptor-derived catalog namespaces
2. discover the Dagster job
3. launch the product pipeline
4. verify the expected Silver table exists
5. verify the expected Gold table exists
6. query the Gold table and confirm that it contains data
7. verify the product's runtime artifacts

There is no seed-product allowlist to update.

---

# What OpenLakeForge derived for you

From:

```yaml
sources:
  - marketing_platform

domains:
  - name: marketing
    silver_tables:
      tables:
        - {name: campaigns, source: marketing_platform, resource: campaigns}
    products:
      - id: campaign_performance
        silver_inputs: [campaigns]
```

OpenLakeForge can derive:

| Resource         | Derived identity                                                          |
| ----------------- | -------------------------------------------------------------------------- |
| Dagster module    | `lakehouse_code.pipelines.dagster.campaign_performance`                    |
| Dagster job        | `campaign_performance_pipeline`                                            |
| Bronze namespace  | `marketing_platform_bronze`                                                |
| Silver namespace  | `marketing_silver` (domain-owned — shared by every `marketing` product)    |
| Gold namespace    | `campaign_performance_gold` (product-owned)                                |
| Floe manifest      | `marketing/marketing.manifest.json`                                        |
| Floe reports        | `marketing/`                                                               |
| dbt runtime artifacts | `marketing/campaign_performance/`                                       |

Provider contracts then resolve those logical identities into the physical storage and
catalog implementation of the active environment. That is why the same data product can
move between local Kubernetes, AWS, and Azure without embedding provider-specific
infrastructure into its business definition.

---

# Adding more source entities

A product can consume more than one Bronze resource. For example:

```text
campaigns
ad_groups
ad_events
```

Add the resources to the source descriptor,
`lakehouse_code/bronze/marketing_platform/source.yaml`:

```yaml
resources:
  - name: campaigns
  - name: ad_groups
  - name: ad_events
```

Map them in the domain and select them from the product in `lakehouse.yaml`:

```yaml
silver_tables:
  tables:
    - {name: campaigns, source: marketing_platform, resource: campaigns}
    - {name: ad_groups, source: marketing_platform, resource: ad_groups}
    - {name: ad_events, source: marketing_platform, resource: ad_events}
products:
  - id: campaign_performance
    silver_inputs: [campaigns, ad_groups, ad_events]
```

Then update the entity tuple in
`lakehouse_code/bronze/marketing_platform/dlt/marketing_platform.py`:

```python
MARKETING_PLATFORM_ENTITIES = ("campaigns", "ad_groups", "ad_events")
```

and define each entity in the Floe contract. The Dagster product definition
aggregator automatically creates the source Bronze and domain Floe definitions.

---

# Adding more Gold models

Add another SQL model under:

```text
lakehouse_code/gold/campaign_performance/dbt/models/gold/
```

For example `mart_campaign_daily.sql`. Declare it in `lakehouse.yaml`:

```yaml
gold_tables:
  tables:
    - name: mart_campaign_performance
    - name: mart_campaign_daily
```

and add it to the Dagster product definition:

```python
CAMPAIGN_PERFORMANCE_GOLD_ASSETS = (
    "mart_campaign_performance",
    "mart_campaign_daily",
)
```

After redeploying the artifacts, both Gold models become part of the product pipeline.

---

# Using the Full profile

The data product created in this tutorial works with the same core pipeline in the Full
profile. When governance is enabled, OpenMetadata metadata is derived from
`lakehouse.yaml` and deployed during the optional artifact phase.

Superset reporting is maintained separately under:

```text
lakehouse_code/dashboards/superset/<dashboard>/
```

and is not required for the Slim tutorial. A dashboard is not required to belong to
exactly one product — one dashboard can consume several Gold products. If you add a
bundle, also declare that dashboard and its non-empty `products` list in
`lakehouse.yaml`; Full deployment and E2E require exact parity between the descriptor
registry and the mounted bundle directories.

The core product pipeline itself does not depend on Superset.

---

# Adding a product to an existing domain

You do not need to create a new domain (or a new source) for every product. To add
another `marketing` product, add another entry under the `marketing` domain's `products`
in `lakehouse_code/lakehouse.yaml`, referencing whichever source it needs — the same
`marketing_platform` source, or a different one — and create the corresponding:

```text
gold/<product>/dbt/
pipelines/dagster/<product>.py
```

Its `silver_inputs` can overlap with an existing product's — the Bronze source and
domain Silver definitions are loaded once and consumed by both in `marketing_silver`
namespace. If the new product needs a new Silver table, add its mapping to the
`marketing` domain's `silver_tables` and add the corresponding entity to the existing
`silver/marketing/contracts/floe/marketing.yml`. Every domain has one Floe
configuration and one `<domain>.manifest.json`; never add a product-specific Floe file
under the same domain. `lakehouse_code/definitions.py` does not need to change.

---

# Ownership identities

When adding a product, keep these identities aligned:

```text
lakehouse.yaml product id
        │
        ├── dbt project directory name (under gold/<product>/)
        └── Dagster module filename (under pipelines/dagster/)

lakehouse.yaml domain name
        └── Floe contract filename (under silver/<domain>/contracts/floe/)
```

For this tutorial, `campaign_performance` is consistently used in:

```text
lakehouse.yaml (product id)
silver/marketing/contracts/floe/marketing.yml
gold/campaign_performance/
pipelines/dagster/campaign_performance.py
```

The product `id` is the globally unique runtime identity used to derive the Gold
namespace (`campaign_performance_gold`); the domain `name` is the runtime identity used
to derive the shared Silver namespace (`marketing_silver`). Consistency here allows
OpenLakeForge to discover and assemble the product without a central product registry.

---

# Next steps

You now have a complete OpenLakeForge data product. From here you can:

* replace the example CSV ingestion with your own dlt source
* add additional Floe validation rules
* create more Silver entities
* build richer dbt Gold marts
* add OpenMetadata governance
* create Superset dashboards
* deploy the same product to AWS or Azure

Useful documentation:

* [Local installation](../setup/local.md)
* [Architecture overview](../architecture/overview.md)
* [Floe validation](../architecture/floe-validation.md)
* [Provider contracts](../architecture/provider-contracts.md)
* [Lakehouse and Source descriptor reference](../reference/domain-descriptor.md)
* [`olf` deployment tooling](../../tools/olf/README.md)
