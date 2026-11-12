# Model Gold with dbt

This lesson creates the `campaign_performance` data product with `olf product
new`, writes its dbt model and tests, and deploys it. Gold is the layer that
holds business marts, built in SQL by dbt-trino from the Silver tables.

**You will** build one Gold mart from the Silver `campaigns` table and deploy
it, in about 15 minutes.

**Start from** the project of [lesson 3](03-validate-into-silver.md): the
`marketing` domain and its Floe contract exist.

## 1. Create the product

```bash
olf product new marketing/campaign_performance \
  --silver-input campaigns \
  --gold-table mart_campaign_performance
```

```text
Created product marketing/campaign_performance with 1 Gold table(s): mart_campaign_performance.
```

`--silver-input` names a Silver table of the domain that the product reads,
and `--gold-table` a mart it produces. The command adds the product to
`lakehouse_code/governance.yml` and creates:

```text
lakehouse_code/
├── gold/
│   ├── dbt_project.yml
│   ├── packages.yml
│   └── models/
│       ├── _sources/
│       │   └── marketing.yml
│       └── campaign_performance/
│           ├── mart_campaign_performance.sql
│           └── schema.yml
└── pipelines/dagster/campaign_performance.py
```

## 2. Know the generated dbt project

Gold is one dbt project for every product, and `campaign_performance/` is
this product's folder in it. The first product creates the shared files;
the next ones add their folder and a line to `dbt_project.yml`. You do not
edit most of it:

- `dbt_project.yml` materializes every model as an Iceberg table that is
  replaced on each run, in the catalog of the stage being deployed. Do not
  hard-code Trino hosts or catalog settings in it: OpenLakeForge supplies one
  dbt profile whose connection comes from each stage's environment. It also
  puts each product's folder in a dbt group of the same name, whose models
  other products cannot `ref()` unless they are marked public.
- `packages.yml` pulls in the shared OpenLakeForge dbt package. It ships
  with OpenLakeForge, not with your project, so the file names it through
  `OPENLAKEFORGE_DBT_PACKAGES_DIR`, which `olf` and the project-code image
  set.
- `models/_sources/marketing.yml` declares the `marketing` domain's Silver
  tables as one dbt source, in the `marketing_silver` schema. Every product of
  the domain reads them from there. dbt reads the table Floe owns and does not
  build a second staging layer.

Ownership follows the layer: Bronze belongs to dlt, Silver to Floe, Gold to
dbt. The Gold namespace is owned by the product and named after its folder,
with no domain prefix: `campaign_performance_gold`.

## 3. Write the model

The scaffold generated a placeholder that selects everything from Silver.
Replace it with an aggregation by channel:

```bash
cat > lakehouse_code/gold/models/campaign_performance/mart_campaign_performance.sql <<'SQL'
with campaigns as (
    select
        campaign_id,
        cast(campaign_date as date) as campaign_date,
        channel,
        cast(spend as double) as spend,
        cast(conversions as bigint) as conversions
    from {{ source('marketing', 'campaigns') }}
)

select
    channel,
    cast(count(*) as bigint) as campaign_count,
    cast(sum(spend) as double) as total_spend,
    cast(sum(conversions) as bigint) as total_conversions
from campaigns
group by channel
SQL
```

## 4. Document and test the model

Describe the mart and its columns, and add dbt tests. dbt runs them in the
same `dbt build` that creates the table, so a failing test fails the run.
Keep the `groups` entry the scaffold wrote: it declares the product's dbt
group.

```bash
cat > lakehouse_code/gold/models/campaign_performance/schema.yml <<'YAML'
version: 2

groups:
  - name: campaign_performance
    owner:
      name: campaign_performance

models:
  - name: mart_campaign_performance
    description: Campaign spend and conversions aggregated by channel.
    columns:
      - name: channel
        description: Marketing channel of the campaigns.
        data_tests:
          - unique
          - not_null
      - name: total_spend
        description: Sum of campaign spend for the channel.
YAML
```

## 5. Deploy the product

The platform is already running, so deploy only the artifacts phase, which
covers everything derived from `lakehouse_code/`:

```bash
olf infra deploy --provider local --phase artifacts
```

It reconciles the catalog namespaces, builds the project-code image, loads it
into the cluster and restarts the Dagster code location. The namespaces `marketing_silver` and
`campaign_performance_gold` are created for you; no Terraform change is
needed.

## Check it worked

```bash
olf infra e2e run --env local --suite full
```

The suite discovers products from the project, so `campaign_performance` is
included without any registration. It launches every product's job, verifies
the Silver and Gold tables exist and requires rows in every Gold mart,
including `mart_campaign_performance`. It ends with:

```text
Local OpenLakeForge full e2e validation passed.
```

Then look at the rows themselves. `olf infra query` runs SQL against the DEV
stage's catalog, so a table is named `<schema>.<table>`:

```bash
olf infra query "SELECT * FROM campaign_performance_gold.mart_campaign_performance ORDER BY channel"
```

It prints one row per marketing channel, with its `campaign_count`,
`total_spend` and `total_conversions`. Add `--format csv` or `--format json`
for other output.

`olf infra query` runs as the stage's workload user, through your cluster
access. If your deployment signs people in with GitHub
(`spec.identity.issuer: github`), you can run the same query as yourself:

```bash
olf login
olf query "SELECT * FROM campaign_performance_gold.mart_campaign_performance ORDER BY channel"
```

Your role then decides what you read: an `analyst` reads this Gold mart but is
refused the Silver tables beneath it
([Roles and permissions](../reference/roles-and-permissions.md#what-each-role-reads-and-writes)).

## What you learned

- `olf product new` creates a product-owned dbt project and a Dagster module
  and registers the product in `governance.yml`.
- `olf infra query` reads any Silver or Gold table of a stage from your machine;
  `olf query` runs SQL as yourself, limited to what your role reads.
- Gold is plain SQL over declared Silver sources; tests run as part of the
  build.
- `olf infra deploy --phase artifacts` ships code changes without touching the
  platform.

## Next

[Orchestrate and schedule](05-orchestrate-and-schedule.md): run the product's
job from Dagster and see how scheduling works.
