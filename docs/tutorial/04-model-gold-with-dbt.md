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
`lakehouse_code/lakehouse.yaml` and creates:

```text
lakehouse_code/
├── gold/campaign_performance/
│   ├── __init__.py
│   └── dbt/
│       ├── dbt_project.yml
│       ├── packages.yml
│       └── models/
│           ├── sources.yml
│           └── gold/
│               ├── mart_campaign_performance.sql
│               └── schema.yml
└── pipelines/dagster/campaign_performance.py
```

## 2. Know the generated dbt project

You do not edit most of it:

- `dbt_project.yml` materializes every model as an Iceberg table that is
  replaced on each run, in the catalog of the stage being deployed. Do not
  hard-code Trino hosts or catalog settings in it: OpenLakeForge renders the
  dbt `profiles.yml` for each environment.
- `packages.yml` pulls in the shared OpenLakeForge dbt package from `libs/`.
- `models/sources.yml` declares the Silver table as a dbt source, in the
  `marketing_silver` schema. dbt reads the table Floe owns and does not build a
  second staging layer.

Ownership follows the layer: Bronze belongs to dlt, Silver to Floe, Gold to
dbt. The Gold namespace is owned by the product and has no domain prefix:
`campaign_performance_gold`.

## 3. Write the model

The scaffold generated a placeholder that selects everything from Silver.
Replace it with an aggregation by channel:

```bash
cat > lakehouse_code/gold/campaign_performance/dbt/models/gold/mart_campaign_performance.sql <<'SQL'
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
SQL
```

## 4. Document and test the model

Describe the mart and its columns, and add dbt tests. dbt runs them in the
same `dbt build` that creates the table, so a failing test fails the run:

```bash
cat > lakehouse_code/gold/campaign_performance/dbt/models/gold/schema.yml <<'YAML'
version: 2

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
olf deploy --provider local --phase artifacts
```

It reconciles the catalog namespaces, generates the Floe manifests, builds the
project-code image, loads it into the cluster and restarts the Dagster code
location. The namespaces `marketing_platform_bronze`, `marketing_silver` and
`campaign_performance_gold` are created for you; no Terraform change is
needed.

## Check it worked

```bash
olf e2e run --env local --suite full
```

The suite discovers products from the project, so `campaign_performance` is
included without any registration. It launches every product's job, verifies
the Silver and Gold tables exist and requires rows in every Gold mart,
including `mart_campaign_performance`. It ends with:

```text
Local OpenLakeForge full e2e validation passed.
```

## What you learned

- `olf product new` creates a product-owned dbt project and a Dagster module
  and registers the product in `lakehouse.yaml`.
- Gold is plain SQL over declared Silver sources; tests run as part of the
  build.
- `olf deploy --phase artifacts` ships code changes without touching the
  platform.

## Next

[Orchestrate and schedule](05-orchestrate-and-schedule.md): run the product's
job from Dagster and see how scheduling works.
