# Orchestrate and schedule

This lesson runs the `campaign_performance_pipeline` job from Dagster, reruns
it, and explains where the recurring schedule of a product comes from.

**You will** launch and rerun your product's job and read its schedule, in
about 10 minutes.

**Start from** the project of [lesson 4](04-model-gold-with-dbt.md): the
`campaign_performance` product is deployed.

## 1. Read the product definition

You write no Dagster code for a product. `lakehouse_code/pipelines/definitions.py`
is the code location, and it builds one job per product in `governance.yml`:

```python
from __future__ import annotations

from openlakeforge_runtime.code_location import build_code_location

from lakehouse_code.pipelines.products import PRODUCTS

defs = build_code_location(PRODUCTS)
```

For each product it connects the three layers: the Bronze loader of
`marketing_platform`, the Floe Silver table `campaigns` and the dbt Gold mart.
It reads them from the project: the dbt models are the Gold assets, the dbt
sources are the Silver inputs, and the Floe entity of each source names its
Bronze resource ([Runtime API](../reference/runtime-api.md)). It also defines
the job `campaign_performance_pipeline`. `lakehouse_code/pipelines/products.py`
lists only the products that depart from the defaults, so a new product needs
no entry there.

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
code location adds a daily schedule named
`campaign_performance_daily` (`0 3 * * *`, UTC), but only in the PROD stage.
It is created stopped, and enabling it is a deliberate act in the Dagster UI,
unless the product's entry in `pipelines/products.py` sets a `ProductCadence`
with `enabled_in_prod=True`, which makes PROD start it on deploy (see the [runtime API](../reference/runtime-api.md)).
DEV and UAT stay manual on purpose: the same immutable revision is deployed to
every stage, and a schedule there would be a production-shaped workload on a
development catalog. You add PROD in lesson 9, and the schedule appears in
that stage's Dagster.

## Check it worked

```bash
olf infra e2e run --env local --suite full
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

[Publish a dashboard](06-publish-a-dashboard.md): put Gold marts in front of
analysts in Superset.
