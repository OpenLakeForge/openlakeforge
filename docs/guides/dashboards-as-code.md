# Dashboards as code

How to keep Superset dashboards in git: edit a dashboard in the Superset UI,
export it back into `lakehouse_code/dashboards/superset/`, declare it in
`lakehouse.yaml`, validate it and import it again. Dashboards need the Full
preset, which adds Superset.

## 1. Use the Full preset

Set `preset: full` under `spec` in `openlakeforge.yaml`, then deploy:

```yaml
spec:
  provider:
    type: local
  preset: full
```

```bash
olf deploy --provider local
```

The deploy applies the change in place: Terraform adds OpenMetadata and
Superset and leaves the data path and its tables as they are. Its artifacts
phase imports every dashboard bundle under `lakehouse_code/dashboards/superset/`.
`olf status --provider local` lists the Superset URL, for example
`http://superset-dev.olf.localhost`. With sign-in off, Superset opens as its
local Admin.

## 2. Know the bundle

Each dashboard is a directory, a bundle, holding what Superset exports:

```text
lakehouse_code/dashboards/superset/sales_customer_health/
├── metadata.yaml
├── databases/
├── datasets/
├── charts/
└── dashboards/
```

A bundle is promotable only when `lakehouse.yaml` declares it and the products
it reads. One dashboard can read several products:

```yaml
dashboards:
  - name: sales_customer_health
    products: [customer_health]
```

The deploy requires exact parity between the declared dashboards and the
bundle directories.

## 3. Start a new dashboard

`olf product new` accepts `--with-report` when it creates a product. Use it
for a new product, not one that already exists:

```bash
olf product new marketing/campaign_summary \
  --silver-input campaigns --gold-table mart_campaign_summary \
  --with-report
```

It writes a draft under `lakehouse_code/dashboards/superset/campaign_summary/`:
the Trino database, one dataset per Gold mart and a README. It adds no
`metadata.yaml` and no `dashboards:` entry, so the draft is neither declared
nor promotable yet, and the deploy does not import it into Superset.

Build the dashboard in Superset instead. After the product's job has run once,
open **Datasets**, add a dataset from the `OpenLakeForge Trino` database, the
`campaign_summary_gold` schema and the `mart_campaign_summary` table, chart it
and save a dashboard. Then export it.

## 4. Export a dashboard

After you build or change a dashboard in the Superset UI, export it back into
its bundle. `olf report export` takes the stage you authored in; author in
DEV. For the `campaign_summary` draft of step 3, which has nothing exported
yet, name the dashboard by the slug you gave it in Superset (its
**Properties**):

```bash
SUPERSET_REPORT_SOURCE_DIR=lakehouse_code/dashboards/superset/campaign_summary \
SUPERSET_DASHBOARD="campaign-summary" \
  olf report export --stage dev
```

For a dashboard that is already in a bundle, such as the demo
`sales_customer_health`, set only `SUPERSET_REPORT_SOURCE_DIR` to its
directory.

- `SUPERSET_REPORT_SOURCE_DIR` picks the bundle. It must be a directory
  directly under `lakehouse_code/dashboards/superset/`. Without it the command
  exports the first dashboard declared in `lakehouse.yaml`.
- The dashboard is found by the uuid stored in the bundle, which survives a
  rename in Superset. For a draft with nothing exported yet, set
  `SUPERSET_DASHBOARD` to its uuid or slug.
- `SUPERSET_ADMIN_USERNAME` and `SUPERSET_ADMIN_PASSWORD` default to `admin`
  and `admin`.

The export writes DEV's live connection into the database file, and a bundle
must stay stage-neutral so it can be promoted to PROD. Set the `sqlalchemy_uri`
line in `databases/openlakeforge_trino.yaml` back to:

```yaml
sqlalchemy_uri: trino://superset@trino:8080/iceberg
```

Then declare the dashboard in `lakehouse.yaml` as shown above if it is new.

## 5. Import a bundle

The next `olf deploy` imports every bundle. To import without a full deploy:

```bash
olf report import --stage dev
```

`olf project build` packages the bundles into a project revision, so a revision
promoted to PROD carries its dashboards.

## Check it worked

```bash
olf report validate
```

It checks every bundle for stable identities, resolvable references and no
stage-bound values. With the three demo dashboards it prints:

```text
3 report bundle(s) are promotable.
```

Pass a dashboard name to check only that one. Without a name, the command also
fails when a bundle directory is not declared in `lakehouse.yaml`.
