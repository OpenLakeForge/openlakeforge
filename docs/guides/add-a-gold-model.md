# Add a Gold model

How to create a data product with `olf product new`, write its dbt model and
tests, deploy it, and add further Gold models to a product that exists. A
product owns one dbt project; each Gold mart is one SQL model in it, built by
dbt-trino from Silver tables.

The example creates `marketing/campaign_performance` from the Silver table
`campaigns` of the domain `marketing`. Run every command from the project
directory.

## 1. Create the product

```bash
olf product new marketing/campaign_performance \
  --silver-input campaigns \
  --gold-table mart_campaign_performance
```

```text
Created product marketing/campaign_performance with 1 Gold table(s): mart_campaign_performance.
```

The target is `<domain>/<product>`. Repeat `--gold-table` for each mart. The
inputs are:

| Option | Use |
| --- | --- |
| `--silver-input <table>` | A Silver table the domain already has. Repeatable. |
| `--input <source>/<resource>` | A Bronze resource to validate into the domain's Silver as well as read. If the domain does not exist, it is created. Repeatable. |
| `--with-report` | Also scaffold a draft Superset report; see [Dashboards as code](dashboards-as-code.md). |
| `--display-name` | A title other than the title-cased product name. |

The product id must be unique across the whole lakehouse and match
`^[a-z][a-z0-9_]*$`. The command adds the product to `lakehouse.yaml` and
creates:

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

You do not edit `dbt_project.yml`, `packages.yml` or `sources.yml`.
`dbt_project.yml` materializes every model as an Iceberg table in the catalog
of the stage being deployed, so do not hard-code Trino hosts or catalogs in it.
`sources.yml` declares the Silver tables as the dbt source `silver`, in the
`marketing_silver` schema.

## 2. Write the model

The scaffold writes a placeholder that selects everything from the first
Silver input, with a `TODO`. Replace it:

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

## 3. Document and test it

dbt runs the tests in the same build that creates the table, so a failing test
fails the run:

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

## 4. Add another mart to the product

For each further Gold model, make three edits:

1. Add `models/gold/<mart>.sql` and describe it in `models/gold/schema.yml`.
2. Declare it under the product in `lakehouse_code/lakehouse.yaml`:

   ```yaml
   gold_tables:
     tables:
       - name: mart_campaign_performance
       - name: mart_campaign_daily
   ```

3. Add its name to the Gold assets tuple in
   `lakehouse_code/pipelines/dagster/campaign_performance.py`:

   ```python
   CAMPAIGN_PERFORMANCE_GOLD_ASSETS = (
       "mart_campaign_performance",
       "mart_campaign_daily",
   )
   ```

## 5. Deploy

The platform is already running, so deploy only the artifacts phase, which
covers everything derived from `lakehouse_code/`:

```bash
olf deploy --provider local --phase artifacts
```

It reconciles the catalog namespaces, generates the Floe manifests, builds the
project-code image, loads it into the cluster and restarts the Dagster code
location. The Gold namespace `campaign_performance_gold` is created for you;
no Terraform change is needed.

## Check it worked

Launch the job `campaign_performance_pipeline` in Dagster, then query the mart:

```bash
olf query "select * from campaign_performance_gold.mart_campaign_performance order by channel"
```

It prints one row per channel with its campaign count, spend and conversions.
`olf e2e run --env local --suite full` also covers the new product, because it
discovers products from the project.
