# Build and manage data products

How to add sources, domains, data products, resources and Gold models to a
project, how their names line up, and how to check and deploy a change. For a
guided first walkthrough of the same steps, follow the
[tutorial](../tutorial/02-ingest-a-source.md).

Run every command from the project directory, on a deployed local platform.
The examples use the source `marketing_platform`, the domain `marketing` and
the product `campaign_performance`.

## How a project is laid out

Ownership follows the medallion layer:

| Layer | Owned by | Directory | Built by |
| --- | --- | --- | --- |
| Bronze | A source | `lakehouse_code/bronze/<source>/` | dlt |
| Silver | A domain | `lakehouse_code/silver/<domain>/` | Floe |
| Gold | A data product | `lakehouse_code/gold/<product>/` | dbt-trino |
| Dashboards | A dashboard | `lakehouse_code/dashboards/superset/<dashboard>/` | Superset |

`lakehouse_code/governance.yml` names the domains and products; each source has
a `source.yaml`, and each dashboard is a bundle directory. OpenLakeForge
derives every platform name from these, so you never create catalog namespaces
or edit Terraform to register a product:

| Derived name | Example | Shared by |
| --- | --- | --- |
| Bronze namespace `<source>_bronze` | `marketing_platform_bronze` | Every product reading the source |
| Silver namespace `<domain>_silver` | `marketing_silver` | Every product of the domain |
| Gold namespace `<product>_gold` | `campaign_performance_gold` | The product only |
| Dagster job `<product>_pipeline` | `campaign_performance_pipeline` | The product only |

Every identifier matches `^[a-z][a-z0-9_]*$`, so `campaign_performance`, not
`campaign-performance`. A product id is unique across the whole lakehouse.
Field-by-field rules are in the [descriptor reference](../reference/descriptors.md).

## Add a source

```bash
olf source new marketing_platform --resource campaigns
```

Repeat `--resource` for each table-like input. The command writes
`bronze/marketing_platform/` with a `source.yaml`, a dlt loader under `dlt/`
and one placeholder CSV per resource under `examples/`. It validates its result
first and writes nothing if validation fails.

Replace each placeholder CSV with your real columns **before** you create a
domain or product that reads it: the Silver contract's columns are inferred
from that file at creation time.

> [!NOTE]
> The scaffold loads CSV files from `examples/`, which gives the same result
> on every run. dlt can extract from APIs and databases, and Floe reads
> Parquet, JSON, Avro and other formats; scaffolding for those sources is
> tracked in [#110](https://github.com/OpenLakeForge/openlakeforge/issues/110).

## Add a domain

```bash
olf domain new marketing --input marketing_platform/campaigns
```

A domain owns a set of Silver tables. Each `--input <source>/<resource>`
becomes a Silver table of the same name, validated by its own entity file,
`silver/marketing/contracts/floe/<resource>.yml`, beside the domain's
`_domain.yml`, which sets the Iceberg namespace, the rejected-rows path and the
policy for all of them. An entity declares column types and a primary key;
every column starts as `nullable: false`, so loosen a column by hand when a
value may be empty. [Floe validation](../architecture/floe-validation.md)
describes the layout.

`olf product new` also creates the domain when its `--input` names a domain
that does not exist yet, so this step is optional.

## Add a data product

```bash
olf product new marketing/campaign_performance \
  --silver-input campaigns \
  --gold-table mart_campaign_performance
```

| Option | Use |
| --- | --- |
| `--silver-input <table>` | A Silver table the domain already has. Repeatable. |
| `--input <source>/<resource>` | A Bronze resource to add to the domain's Silver and read. Creates the domain if needed. Repeatable. |
| `--gold-table <mart>` | A Gold mart the product produces. Repeatable, at least one. |
| `--with-report` | Also scaffold a draft Superset report; see [Dashboards as code](dashboards-as-code.md). |

It creates a dbt project under `gold/campaign_performance/dbt/` and a Dagster
module `pipelines/dagster/campaign_performance.py`, and registers the product
in `governance.yml`. Dagster loads every module in that directory by itself.

The generated model `models/gold/mart_campaign_performance.sql` selects
everything from the first Silver input. Replace it with your SQL, reading
Silver through `{{ source('silver', '<table>') }}`, and describe and test it in
`models/gold/schema.yml`; dbt runs the tests in the same build, so a failing
test fails the run. Do not hard-code Trino hosts or catalogs: OpenLakeForge
renders the dbt profile for each stage.

## Add a resource to an existing source

1. Add it under `resources` in `bronze/marketing_platform/source.yaml`.
2. Add its data as `examples/<resource>.csv`.
3. Add its name to the entity tuple in `dlt/marketing_platform.py`:

   ```python
   MARKETING_PLATFORM_ENTITIES = ("campaigns", "ad_groups")
   ```

4. Carry it into Silver. The simplest way is a new product that reads it, which
   also adds the Silver table and its Floe entity:

   ```bash
   olf product new marketing/ad_group_report \
     --input marketing_platform/ad_groups --gold-table mart_ad_groups
   ```

To let an existing product read the resource instead, make these edits by
hand:

| File | Add |
| --- | --- |
| `silver/marketing/contracts/floe/ad_groups.yml` | An entity file, copied from an existing one, with `source: { ref: marketing_platform, resource: ad_groups }` |
| `gold/<product>/dbt/models/sources.yml` | `- name: ad_groups` under the `silver` source |
| `pipelines/dagster/<product>.py` | `"ad_groups"` in the Silver inputs tuple and `("marketing_platform", "ad_groups")` in `bronze_inputs` |
| `gold/<product>/dbt/models/gold/<mart>.sql` | A read of `{{ source('silver', 'ad_groups') }}` |

## Add a Gold model to an existing product

1. Add `models/gold/<mart>.sql` and describe it in `models/gold/schema.yml`.
   No descriptor lists it: a table in `<product>_gold` belongs to the product.
2. Add its name to the Gold assets tuple in
   `pipelines/dagster/<product>.py`:

   ```python
   CAMPAIGN_PERFORMANCE_GOLD_ASSETS = (
       "mart_campaign_performance",
       "mart_campaign_daily",
   )
   ```

## Remove a product

Delete its entry from `governance.yml`, its `gold/<product>/` directory and its
Dagster module. Dagster loads every module under `pipelines/dagster/`,
declared or not, so a leftover module keeps the job alive.

Renaming a source, domain, product or table changes the derived namespaces,
so on a deployed stage treat it as a data migration, not an edit.
`displayName`, `description` and `status` are metadata only.

## Validate and deploy

| Command | Checks |
| --- | --- |
| `olf check contracts` | Each descriptor against its schema: identifiers, duplicates, a domain or product without its directory; each Floe domain writes its own `<domain>_silver` namespace |
| `olf check project-code` | The whole inventory loads in Dagster, including references between descriptors |
| `olf check dbt` | Every product's dbt project compiles |

Then deploy only the artifacts phase, which covers everything derived from
`lakehouse_code/`:

```bash
olf deploy --provider local --phase artifacts
```

It reconciles the catalog namespaces, builds the project-code image and
restarts Dagster's code location.

## Check it worked

Launch `campaign_performance_pipeline` in Dagster, then query the mart:

```bash
olf query "SELECT * FROM campaign_performance_gold.mart_campaign_performance"
```

`olf e2e run --env local --suite full` also covers the new product, because it
discovers products from `governance.yml`.
