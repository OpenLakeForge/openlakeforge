# Tour the platform

This lesson opens the portal and Dagster on a running local deployment,
launches one of the demo data products, and shows what each step produced. It
is the first of the tutorial lessons that build one project, one capability at
a time.

**You will** open the portal, read the Dagster asset graph and run a data
product from Bronze to Gold, in about 15 minutes.

**Start from** the project you deployed in the
[Quickstart](../get-started/quickstart.md) (steps 1 and 2), on the default
**Slim** preset with its three demo data products. Lesson 6 switches the same
deployment to Full in place, without losing anything you built. Run every
`olf` command in this tutorial from that project directory.

## 1. Open the portal

```text
http://olf.localhost
```

The portal links to every service the profile enables. `olf infra status --provider
local` lists the same addresses in its `URLs` section. Browsers resolve every
`*.localhost` name to your machine, so no hosts-file entry is needed, and
there is no sign-in yet.

## 2. Open Dagster

Select Dagster in the portal, or go to it directly:

```text
http://dagster-dev.olf.localhost
```

Dagster orchestrates every pipeline in OpenLakeForge. From its UI you can
inspect the asset graph, launch a job, inspect each materialization and read
the execution logs.

## 3. Read the asset graph

Open **Assets** and its lineage graph. Each data product is a chain of three
layers, and the asset key tells you which:

| Layer | Built by | Asset key | Example |
| --- | --- | --- | --- |
| Bronze | dlt | `<source>/<resource>` | `crm/accounts` |
| Silver | Floe | `<domain>/<table>` | `sales/accounts` |
| Gold | dbt | `<product>/<mart>` | `customer_health/mart_customer_health_score` |

The demo project has two sources (`crm`, `erp`), two domains (`sales`,
`supply_chain`) and three products (`order_revenue`, `customer_health`,
`inventory_reliability`). Follow the arrows from `crm/accounts` to a Gold mart
to see one product end to end.

## 4. Launch a product job

Every product gets one job named `<product>_pipeline`. Open **Jobs**, select
`customer_health_pipeline`, open its **Launchpad** and select **Launch Run**.
The run loads the CSV files into Bronze, validates them into Silver Iceberg
tables, and builds the Gold marts with dbt. The graph updates as each asset
materializes.

## 5. Inspect the materializations

When the run succeeds, select the Bronze asset `crm/accounts` and open its
latest materialization. dlt records the number of `rows` loaded, the
`bronze_uri` it wrote to in object storage and the `source_file` it read. The
run page lists the logs of every step.

## Check it worked

```bash
olf infra e2e run --env local --suite full
```

The suite launches every demo product's job, checks that each Silver and Gold
table exists, and queries every Gold mart through Trino for rows. It ends
with:

```text
Local OpenLakeForge full e2e validation passed.
```

## What you learned

- The portal links every service; Dagster is where you run and inspect
  pipelines.
- A data product is a Bronze, Silver and Gold chain with one
  `<product>_pipeline` job.
- `olf infra e2e run` checks every deployed data product end to end; you will rerun
  it after each change to a product.

## Next

[Ingest a source](02-ingest-a-source.md): add a source of your own.
