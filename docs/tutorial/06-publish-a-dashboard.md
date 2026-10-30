# Publish a dashboard

This lesson switches the project from Slim to the Full preset, which adds
Superset, and treats a dashboard as code: you change a chart in the Superset UI
and export it back into `lakehouse_code/`, where it is versioned like any other
file.

**You will** turn on Superset, edit a demo dashboard and export it, in about
20 minutes.

**Start from** the project of [lesson 5](05-orchestrate-and-schedule.md): the
`campaign_performance` product is deployed on the Slim preset, and the
end-to-end run of lesson 5 filled every Gold mart. The dashboards in this
lesson are the ones the demo project ships; `campaign_performance` has none
yet.

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

## 2. Deploy the change

```bash
olf infra deploy --provider local
```

`olf infra deploy` applies the change to the running deployment: Terraform adds
OpenMetadata and Superset and leaves the data path, your product and its
tables as they are. The deploy imports every dashboard under
`lakehouse_code/dashboards/superset/` into Superset in its artifacts phase.

## 3. Open Superset

```text
http://superset-dev.olf.localhost
```

With sign-in off, Superset opens already signed in as its local Admin.
Open **Dashboards**: the project ships `Sales Customer Health`, `Sales Order
Revenue` and `Supply Chain Inventory Reliability`, each reading the Gold marts
of its product through the `OpenLakeForge Trino` database.

## 4. Change a chart

Open `Sales Customer Health`, edit the chart `Health Score by Segment`, rename
it `Health Score by Segment (edited)` and save it.

## 5. Export the dashboard as code

Export the dashboard back into its bundle directory, which `olf infra report export`
does for the stage you authored in:

```bash
SUPERSET_REPORT_SOURCE_DIR=lakehouse_code/dashboards/superset/sales_customer_health \
  olf infra report export --stage dev
```

`SUPERSET_REPORT_SOURCE_DIR` picks the bundle; without it the command exports
the first dashboard bundle it finds. The bundle's YAML files,
charts, datasets and the Trino database, are rewritten under that directory:

```bash
grep -rl "(edited)" lakehouse_code/dashboards/superset/sales_customer_health
```

The export keeps the bundle stage-neutral: it replaces DEV's live Trino
connection with a placeholder that each deploy rewrites, so the same bundle can
be promoted to PROD.

A directory is a promotable report once the export has written its
`metadata.yaml`. Its datasets name the Gold schema they read, which is how
OpenLakeForge knows the products the dashboard serves. One dashboard can
consume several products. The next `olf infra deploy` (or `olf infra
report import`) imports the bundle into Superset, and `olf infra project build`
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

- `spec.preset: full` plus `olf infra deploy` adds Superset and OpenMetadata in place;
  the deploy imports every dashboard bundle in the project.
- Dashboards live in `lakehouse_code/dashboards/superset/<dashboard>/` and are
  found by their `metadata.yaml`; their datasets name the products they read.
- `olf infra report export` pulls UI edits into git; `olf infra report import` and `olf infra
  deploy` push the bundle back.

## Next

Govern and trace lineage (lesson 7): see the same project's domains, data
products and lineage in OpenMetadata.
