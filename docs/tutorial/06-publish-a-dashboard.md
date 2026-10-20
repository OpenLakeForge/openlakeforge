# Publish a dashboard

This lesson switches the project to the Full preset, which adds Superset, and
treats a dashboard as code: you change a chart in the Superset UI and export it
back into `lakehouse_code/`, where it is versioned like any other file.

**You will** enable Superset, edit a demo dashboard and export it, in about 20
minutes.

**Start from** the project of [lesson 5](05-orchestrate-and-schedule.md): the
`campaign_performance` product is deployed on the Slim preset. The dashboards
in this lesson are the ones the demo project ships; `campaign_performance` has
none yet.

## 1. Switch to the Full preset

Set `preset: full` under `spec` in `openlakeforge.yaml`:

```yaml
spec:
  provider:
    type: local
  preset: full
```

**Full** adds OpenMetadata (governance and lineage, lesson 7) and Superset
(dashboards) to the Slim data path. The data path itself does not change.

## 2. Redeploy

Tear down the Slim deployment, then deploy again. Your code in
`lakehouse_code/` stays on disk and is redeployed:

```bash
olf destroy --provider local
olf deploy --provider local
```

The deploy imports the dashboards under `lakehouse_code/dashboards/superset/`
into Superset in the artifacts phase.

## 3. Fill the tables

A fresh deployment has no data, so the charts are empty. Run every product's
job with the end-to-end suite, which also verifies the Superset dashboards:

```bash
olf e2e run --env local --suite full
```

## 4. Open Superset

```text
http://superset-dev.olf.localhost
```

Sign in with the development credentials `admin` / `admin` (local only).
Open **Dashboards**: the project ships `Sales Customer Health`, `Sales Order
Revenue` and `Supply Chain Inventory Reliability`, each reading the Gold marts
of its product through the `OpenLakeForge Trino` database.

## 5. Change a chart

Open `Sales Customer Health`, edit the chart `Health Score by Segment`, rename
it `Health Score by Segment (edited)` and save it.

## 6. Export the dashboard as code

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

- `spec.preset: full` adds Superset and OpenMetadata without changing the data
  path.
- Dashboards live in `lakehouse_code/dashboards/superset/<dashboard>/` and are
  declared in `lakehouse.yaml` with the products they read.
- `olf report export` pulls UI edits into git; `olf report import` and `olf
  deploy` push the bundle back.

## Next

Govern and trace lineage (lesson 7): see the same project's domains, data
products and lineage in OpenMetadata.
