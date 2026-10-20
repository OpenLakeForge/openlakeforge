# Publish a dashboard

This lesson treats a dashboard as code: you change a chart in the Superset UI
and export it back into `lakehouse_code/`, where it is versioned like any other
file.

**You will** edit a demo dashboard in Superset and export it, in about 15
minutes.

**Start from** the project of [lesson 5](05-orchestrate-and-schedule.md): the
`campaign_performance` product is deployed on the Full preset, which includes
Superset, and the end-to-end run of lesson 5 filled every Gold mart. The
dashboards in this lesson are the ones the demo project ships;
`campaign_performance` has none yet.

The deploy imports every dashboard under `lakehouse_code/dashboards/superset/`
into Superset in its artifacts phase, so they are already there.

## 1. Open Superset

```text
http://superset-dev.olf.localhost
```

With sign-in off, Superset opens already signed in as its local Admin.
Open **Dashboards**: the project ships `Sales Customer Health`, `Sales Order
Revenue` and `Supply Chain Inventory Reliability`, each reading the Gold marts
of its product through the `OpenLakeForge Trino` database.

## 2. Change a chart

Open `Sales Customer Health`, edit the chart `Health Score by Segment`, rename
it `Health Score by Segment (edited)` and save it.

## 3. Export the dashboard as code

Export the dashboard back into its bundle directory, which `olf report export`
does for the stage you authored in:

```bash
SUPERSET_REPORT_SOURCE_DIR=lakehouse_code/dashboards/superset/sales_customer_health \
  olf report export --stage dev
```

`SUPERSET_REPORT_SOURCE_DIR` picks the bundle; without it the command exports
the first dashboard declared in `lakehouse.yaml`. The bundle's YAML files,
charts, datasets and the Trino database, are rewritten under that directory:

```bash
grep -rl "(edited)" lakehouse_code/dashboards/superset/sales_customer_health
```

A bundle only exists as a promotable report once `lakehouse.yaml` declares it
and the products it reads:

```yaml
dashboards:
  - name: sales_customer_health
    products: [customer_health]
```

One dashboard can consume several products. The next `olf deploy` (or `olf
report import`) imports the bundle into Superset, and `olf project build`
packages it into a project revision.

## Check it worked

```bash
olf report validate
```

It checks every bundle for stable identities, resolvable references and no
stage-bound values, and prints:

```text
3 report bundle(s) are promotable.
```

## What you learned

- The Full preset's Superset reads the Gold marts through Trino; the deploy
  imports every dashboard bundle in the project.
- Dashboards live in `lakehouse_code/dashboards/superset/<dashboard>/` and are
  declared in `lakehouse.yaml` with the products they read.
- `olf report export` pulls UI edits into git; `olf report import` and `olf
  deploy` push the bundle back.

## Next

Govern and trace lineage (lesson 7): see the same project's domains, data
products and lineage in OpenMetadata.
