# Govern and trace lineage

This lesson opens OpenMetadata, which the Full preset deploys, and follows the
domains, data products and lineage that OpenLakeForge fills in from your
project.

**You will** find your `marketing` domain and `campaign_performance` product
in OpenMetadata and trace a Gold table back to Bronze, in about 15 minutes.

**Start from** the project of [lesson 6](06-publish-a-dashboard.md): the Full
preset is deployed and every product's job has run once (the end-to-end run of
[lesson 5](05-orchestrate-and-schedule.md) did it).

## 1. Open OpenMetadata

```text
http://openmetadata.olf.localhost
```

Sign in with the local development credentials `admin@open-metadata.org` /
`admin`.

## 2. Find the domain and data product

You wrote no OpenMetadata configuration. The artifacts phase of `olf deploy`
reads `lakehouse_code/lakehouse.yaml` and creates:

| lakehouse.yaml | OpenMetadata |
| --- | --- |
| A domain such as `marketing` | A domain, shown as `Marketing` |
| A product such as `campaign_performance` | A data product named `campaign_performance`, shown as `Campaign Performance`, inside that domain |

In the left menu, open **Domains** and select `Marketing`. Its data product
`Campaign Performance` is listed, with the description from `lakehouse.yaml`.
To change either, edit that file and run `olf deploy --provider local --phase
artifacts`.

## 3. Find the tables

OpenMetadata crawls the Iceberg catalog through Trino, so every Silver and
Gold table is listed under the database of your stage, `lakehouse_dev`.
Search for `mart_campaign_performance`. Its full name is
`polaris.lakehouse_dev.campaign_performance_gold.mart_campaign_performance`.

## 4. Trace the lineage

Open the table and select its **Lineage** tab. Floe and dbt each emit
OpenLineage events to OpenMetadata every time the job runs, so the graph shows
the Silver table `marketing_silver.campaigns` feeding the Gold mart. Nothing
in your project turns this on.

> [!NOTE]
> Lineage appears after a job has run. If the tab is empty, launch
> `campaign_performance_pipeline` in Dagster again and refresh.

## Check it worked

```bash
olf e2e run --env local --suite full
```

With the Full preset the suite also checks OpenMetadata: it requires every
domain and data product from `lakehouse.yaml`, and the Gold lineage of the
stage. It ends with:

```text
Local OpenLakeForge full e2e validation passed.
```

## What you learned

- Governance metadata is derived from `lakehouse.yaml`: one OpenMetadata domain
  per domain, one data product per product.
- Lineage comes from the runs themselves, through Floe and dbt, not from
  configuration.
- Change names and descriptions in the descriptor and redeploy the artifacts.

## Next

[Add PROD and promote a revision](08-add-prod-and-promote.md). To control who
may open these services, see
[Authentication and permissions](../setup/github-oauth-app.md).
