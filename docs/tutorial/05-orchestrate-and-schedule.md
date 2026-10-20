# Orchestrate and schedule

This lesson runs the `campaign_performance_pipeline` job from Dagster, reruns
it, and explains where the recurring schedule of a product comes from.

**You will** launch and rerun your product's job and read its schedule, in
about 10 minutes.

**Start from** the project of [lesson 4](04-model-gold-with-dbt.md): the
`campaign_performance` product is deployed.

## 1. Read the product definition

Dagster code for a product lives in
`lakehouse_code/pipelines/dagster/campaign_performance.py`, which
`olf product new` generated:

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

`build_product_definitions` connects the three layers of the product: the
Bronze loader of `marketing_platform`, the Floe Silver table `campaigns` and
the dbt Gold mart. It also defines the job `campaign_performance_pipeline`.
`lakehouse_code/definitions.py` finds every module in
`lakehouse_code/pipelines/dagster/` by itself, so you register nothing.

## 2. Launch the job

Open Dagster:

```text
http://dagster-dev.olf.localhost
```

Open **Jobs**, select `campaign_performance_pipeline`, open its **Launchpad**
and select **Launch Run**. The graph runs in order:

```text
marketing_platform/campaigns   dlt writes campaigns.csv to Bronze
marketing/campaigns            Floe validates it into marketing_silver.campaigns
campaign_performance/mart_campaign_performance
                               dbt builds and tests the Gold mart
```

Select the Bronze asset to see its `rows`, `bronze_uri` and `source_file`, as
in lesson 1.

## 3. Rerun

Open the finished run and use **Re-execute** to run the same job again. A rerun
is safe: Floe writes Silver with `write_mode: "overwrite"` and dbt replaces
the Gold table, so the tables reflect the current input rather than
accumulating duplicates.

## 4. Know where the schedule comes from

A product's job has no recurring trigger of its own in DEV. The shared
`build_product_definitions` adds a daily schedule named
`campaign_performance_daily` (`0 3 * * *`, UTC), but only in the PROD stage.
It is created stopped, and enabling it is a deliberate act in the Dagster UI.
DEV and UAT stay manual on purpose: the same immutable revision is deployed to
every stage, and a schedule there would be a production-shaped workload on a
development catalog. You add PROD in lesson 9, and the schedule appears in
that stage's Dagster.

## Check it worked

```bash
olf e2e run --env local --suite full
```

It launches every product's job, `campaign_performance_pipeline` included,
verifies the Silver and Gold tables, and requires rows in every Gold mart. It
ends with:

```text
Local OpenLakeForge full e2e validation passed.
```

## What you learned

- Each product has one `<product>_pipeline` job that runs Bronze, Silver and
  Gold in order.
- Rerunning a job is safe; Silver and Gold are overwritten.
- Recurring schedules belong to PROD, which you add in lesson 9.

## Next

Publish a dashboard (lesson 6): put the Gold mart in front of analysts in
Superset.
