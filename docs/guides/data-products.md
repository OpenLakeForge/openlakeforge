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
| Gold | A data product | `lakehouse_code/gold/models/<product>/` | dbt-trino |
| Dashboards | A dashboard | `lakehouse_code/dashboards/superset/<dashboard>/` | Superset |

`lakehouse_code/governance.yml` names the domains and, nested under each, its
products; each source has a `source.yaml`, and each dashboard is a bundle directory. OpenLakeForge
derives every platform name from these, so you never create catalog namespaces
or edit Terraform to register a product:

| Derived name | Example | Shared by |
| --- | --- | --- |
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
> on every run. For an API or a database, `--kind api` or `--kind sql` writes
> an incremental dlt connector instead; see [Load an external source
> incrementally](incremental-sources.md).

## Add a domain

```bash
olf domain new marketing --input marketing_platform/campaigns
```

A domain owns a set of Silver tables. Each `--input <source>/<resource>`
becomes a Silver table of the same name, validated by its own entity file,
`silver/marketing/contracts/<resource>.yml`, beside the domain's
`domain.yml`, which sets the Iceberg namespace, the rejected-rows path and the
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

It creates the product's folder `gold/models/campaign_performance/` in the
Gold dbt project and a Dagster module `pipelines/dagster/campaign_performance.py`,
and registers the product in `governance.yml`. It also adds the product's
`+group` block to `gold/dbt_project.yml` and declares any Silver table the
domain's `gold/models/_sources/<domain>.yml` lacks; the first product creates
the project itself. Dagster loads every module in `pipelines/dagster/` by
itself.

The generated model `mart_campaign_performance.sql` selects everything from
the first Silver input. Replace it with your SQL, reading Silver through
`{{ source('<domain>', '<table>') }}`, and describe and test it in the
product's `schema.yml`; dbt runs the tests in the same build, so a failing
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
| `silver/marketing/contracts/ad_groups.yml` | An entity file, copied from an existing one, with `source: { ref: marketing_platform, resource: ad_groups }` |
| `gold/models/_sources/marketing.yml` | `- name: ad_groups` under the `marketing` source |
| `gold/models/<product>/<mart>.sql` | A read of `{{ source('marketing', 'ad_groups') }}` |

## Add a Gold model to an existing product

1. Add `gold/models/<product>/<mart>.sql` and describe it in the product's
   `schema.yml`. No descriptor lists it: its folder puts it in
   `<product>_gold`, and a table there belongs to the product.

   The product's Dagster job picks the model up from the dbt project; nothing in
   `pipelines/dagster/<product>.py` lists it.

A product's models are private to it: `gold/dbt_project.yml` sets
`+access: private`, and each product is a dbt group. To let another product
`ref()` a mart, set `access: public` on that model in its `schema.yml`:

```yaml
models:
  - name: mart_campaign_performance
    access: public
```

A `ref()` to a model that is not public fails to parse. The reading product's
job uses the mart as it is and never rebuilds it.

Keep one model's reads to about 150 tables or fewer. Trino counts about two
stages per table a query reads and stops at `max_stage_count` (300 in every
`query.size`, see
[the deployment profile](../reference/deployment-profile.md)) with
`QUERY_HAS_TOO_MANY_STAGES`. A union over a whole domain should be split into
staging models.

## Remove a product

Delete its entry from `governance.yml`, its `gold/models/<product>/` folder,
its block in `gold/dbt_project.yml` and its Dagster module. Dagster loads every module under `pipelines/dagster/`,
declared or not, so a leftover module keeps the job alive.

Renaming a source, domain, product or table changes the derived namespaces,
so on a deployed stage treat it as a data migration, not an edit.
`displayName`, `description` and `status` are metadata only.

## Validate and deploy

| Command | Checks |
| --- | --- |
| `olf check contracts` | Each descriptor against its schema: identifiers, duplicates, a domain or product without its directory; each Floe domain writes its own `<domain>_silver` namespace |
| `olf check project-code` | The whole inventory loads in Dagster, including references between descriptors |
| `olf check dbt` | Every product's dbt project parses and compiles, and its models and sources sit in the schemas the descriptors name. Incremental models are parsed (refs, sources, Jinja) but not compiled: `is_incremental()` needs a warehouse connection, so their rendered SQL is not checked |

Then deploy only the artifacts phase, which covers everything derived from
`lakehouse_code/`:

```bash
olf infra deploy --provider local --phase artifacts
```

It reconciles the catalog namespaces, builds the project-code image and
restarts Dagster's code location.

### Python dependencies

A project that needs a Python package (an API client, a dlt source) lists it in
`lakehouse_code/requirements.txt`, with plain pins or ranges. The image installs the file in its own
layer after the platform dependencies, constrained to their exact versions, so a project cannot change
a platform pin. `olf check project-code` and the image build resolve the file against those pins and
fail on a conflict. Unlike the platform lock, this layer is not hash-verified. Without the file the
image is unchanged.

## Check it worked

Launch `campaign_performance_pipeline` in Dagster, then query the mart:

```bash
olf infra query "SELECT * FROM campaign_performance_gold.mart_campaign_performance"
```

`olf infra e2e run --env local --suite full` also covers the new product, because it
discovers products from `governance.yml`.
