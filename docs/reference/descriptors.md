# Descriptor reference

The fields and rules of the two descriptors that declare a project's data:
`lakehouse_code/governance.yml`, one per project, and
`lakehouse_code/bronze/<source>/source.yaml`, one per source. It also lists the
names OpenLakeForge derives from them.

Both are validated against JSON Schemas,
[`governance.schema.json`](../schema/governance.schema.json) and
[`source.schema.json`](../schema/source.schema.json), and by the canonical
inventory, with `olf check contracts`. Unknown fields are rejected at every
level, and every identifier matches `^[a-z][a-z0-9_]*$`.

Descriptors are provider-neutral: they name logical things only. They never
contain Polaris, Glue, S3 or SeaweedFS names, endpoints, Kubernetes resources,
account ids or credentials; OpenLakeForge derives those from the active
provider.

## `source.yaml`

```yaml
apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: marketing_platform
displayName: Marketing Platform
description: Marketing campaign performance source system.
status: active
resources:
  - name: campaigns
    description: Raw CSV marketing campaign performance data.
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | `openlakeforge.io/v1alpha3` |
| `kind` | string | yes | `Source` |
| `name` | identifier | yes | Must equal the directory name and be unique in the lakehouse. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | yes | May be empty. |
| `status` | string, non-empty | yes | Free text; typically `planned`, `active` or `deprecated`. |
| `resources` | array, at least one | yes | The source's table-like inputs. |
| `resources[].name` | identifier | yes | Unique within the source. |
| `resources[].description` | string | no | Human-readable description. |
| `resources[].load.mode` | `full` or `incremental` | no | `full` (the default) replaces the resource's Bronze files on every run. `incremental` appends only the rows past the cursor. See [Load modes](#load-modes). |
| `resources[].load.cursor` | column name | with `incremental` | The column whose last loaded value the next run starts after. Not allowed with `full`. |
| `resources[].load.primary_key` | array of column names | with `incremental` | The row's identity, which Silver merges a changed row onto. |
| `runtime.config` | array of key names | no | Names of the non-secret settings the loader reads, such as an API base URL. |
| `runtime.secrets` | array of key names | no | Names of the credentials the loader reads, such as an API token. A name appears in at most one of the two lists. |

A resource has no schema here: its columns belong to the Floe contract of each
domain that reads it.

`runtime` lists key names, never values: each stage binds its own. For a
source `crm_api` with `runtime: {config: [base_url], secrets: [api_token]}`:

| Key | Bound by the stage in | Reaches the loader as |
| --- | --- | --- |
| `base_url` | ConfigMap `olf-source-crm-api` | `SOURCES__CRM_API__BASE_URL` |
| `api_token` | Secret `olf-source-crm-api-credentials` | `SOURCES__CRM_API__API_TOKEN` |

These are the names dlt itself resolves for a `@dlt.source` in
`bronze/crm_api/dlt/crm_api.py`, so the loader reads them with
`dlt.config.value` and `dlt.secrets.value`, and fails if one is missing. In
the Kubernetes names, `_` becomes `-`. Because of both conversions, a source
with a `runtime` block, and each of its keys, must be underscore-separated
words: `crm_api` and `base_url`, not `crm_`, `_url` or `a__b`.

Where they apply:

- Each product job carries the bindings of the sources it loads. They reach
  that job's run pod only, never the code-location server.
- Steps of one run share its pod, so every loader in a job sees every binding
  the job carries.
- A Bronze asset materialized outside its product job gets no binding and
  fails with dlt's missing-config error.
- `olf deploy` refuses to activate a revision whose sources the stage has not
  bound. It reads Secret key names only, never values.

A loader that is a real dlt source runs it through
`libs.bronze_dlt.bronze_pipeline(<source>)`:

- Files land at `<stage Bronze bucket>/<source>/<resource>/`, the path Floe
  reads Bronze from.
- dlt's incremental state lives in that same bucket, under the pipeline
  `<stage>__<source>`. Each run starts from an empty working directory and
  restores its cursor from its own stage, so a DEV run cannot move PROD's.
- `olf check project-code` proves this with a mocked API source run as DEV
  and as PROD.

### Load modes

Each layer declares its load mode where its owner keeps it:

| Layer | Declared in | Incremental | Full |
| --- | --- | --- | --- |
| Bronze | `resources[].load` in `source.yaml` | dlt appends the rows past `cursor` as new files | dlt replaces the files |
| Silver | the entity's Floe contract | `incremental_mode: file` with `merge_scd1`, `merge_scd2` or `append` | `overwrite` |
| Gold | the dbt model's `materialized` config | `incremental` with a `unique_key` and the `merge` strategy | `table` |

`olf check contracts` rejects a Silver entity whose mode does not fit its
Bronze resource. An entity reading an incremental resource needs
`incremental_mode: file` and `append` or a merge mode. An entity reading a
full resource cannot use `append`, which would add every row again on each
run.

A loader with an incremental resource runs its dlt source through
`libs.bronze_dlt.load_dlt_source`. The connector declares the cursor with
`dlt.sources.incremental` on the column `load.cursor` names, and the load
fails if the two differ. A run with nothing new past the cursor writes no
Bronze file, so Floe has no new file to read either.

Floe merges an entity's pending files in one `MERGE` and keeps the first row
of a repeated key. A Silver run catching up on several Bronze batches must
keep the newest version, so Bronze file names sort the newest batch first.

### Full refresh

A run is incremental unless it carries the run tag
`openlakeforge/full_refresh`. Set the tag in the Dagster launchpad, or run
`olf product run <product> --full-refresh <scope>`:

| Scope | Bronze | Silver | Gold |
| --- | --- | --- | --- |
| `bronze` | Files and cursors of the job's incremental resources dropped, then everything extracted | Merges the whole new extract | `dbt build --full-refresh` |
| `gold` | Incremental | Incremental | `dbt build --full-refresh` |

There is no Silver-only scope: incremental Bronze keeps every batch, so
Silver can only be rebuilt from a Bronze reloaded in full.

Known limits:

- Floe rejects `--full-refresh` with merge modes, and dagster-floe has no
  per-run option for it. After a `bronze` refresh, Silver merges the full
  extract onto the table it already has, so a row deleted at the source stays
  in Silver.
- A run killed after dlt wrote some files of a batch but before it saved
  the cursor leaves those files in Bronze. The rerun extracts the same rows
  again into a newer batch. Silver keeps the newer rows and reports the
  killed batch's rows as rejected duplicates; Bronze keeps both.

Incremental state is per stage. dlt keeps cursors under the pipeline
`<stage>__<source>` in the stage's Bronze bucket. Floe keeps its file state
under the source path in that same bucket, and dbt's state is the stage's
Gold table itself.

## `governance.yml`

```yaml
apiVersion: openlakeforge.io/v1beta1
kind: Governance
domains:
  - name: marketing
    displayName: Marketing
    description: Marketing analytics data products.
    status: active
products:
  - id: campaign_performance
    domain: marketing
    displayName: Campaign Performance
    description: Campaign spend and conversion performance.
    status: active
```

The file names domains and data products and nothing else. Tables belong to a
domain or a product by the schema they live in, so no table is listed:

| Schema | Owner |
| --- | --- |
| `<domain>_silver` | the domain |
| `<product>_gold` | the product, and through it the product's domain |

Sources are the `bronze/*/source.yaml` files on disk. Dashboards are the
`dashboards/superset/*/` directories that hold a `metadata.yaml`; a dashboard
serves the products whose `<product>_gold` schema its dataset files read.

### Top level

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | `openlakeforge.io/v1beta1` |
| `kind` | string | yes | `Governance` |
| `domains` | array, at least one | yes | The business domains. |
| `products` | array, may be empty | yes | The data products. At least one is needed to check or deploy a project. |

### `domains[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | identifier | yes | Unique across the lakehouse; also the directory `silver/<name>/`. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | no | |
| `status` | string, non-empty | no | Free text. |

### `products[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Unique across the whole lakehouse; also the directory `gold/<id>/`. |
| `domain` | identifier | yes | Must name a domain in the file. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | no | |
| `status` | string, non-empty | no | Free text. |

`olf check contracts` also requires one `silver/<domain>/` directory per
domain and one `gold/<product>/` directory per product, and rejects a dbt
model that dbt resolved into any schema but `<product>_gold` (when the
product's `target/manifest.json` exists), since its tables would belong to no
product.

## Implementation files

Each identity selects the files that implement it:

| Identity | Files |
| --- | --- |
| Source `name` | `lakehouse_code/bronze/<source>/` |
| Domain `name` | `lakehouse_code/silver/<domain>/contracts/floe/<domain>.yml`, the domain's only Floe contract |
| Product `id` | `lakehouse_code/gold/<id>/dbt/` and `lakehouse_code/pipelines/dagster/<id>.py` |
| Dashboard | `lakehouse_code/dashboards/superset/<name>/`, once it holds a `metadata.yaml` |

## Derived names

For a `crm` source, a `sales` domain and an `order_revenue` product:

| Name | Value |
| --- | --- |
| Bronze namespace | `crm_bronze` |
| Silver namespace | `sales_silver`, shared by every product of the domain |
| Gold namespace | `order_revenue_gold` |
| Dagster job | `order_revenue_pipeline` |
| Dagster module | `lakehouse_code.pipelines.dagster.order_revenue` |
| Floe report prefix | `floe/reports/sales/` |
| dbt artifact prefix | `run-artifacts/dbt/sales/order_revenue/` |

Tables are addressed as `<stage catalog>.<namespace>.<table>`, for example
`lakehouse_dev.order_revenue_gold.mart_order_revenue_by_day`. The stage
catalog is `lakehouse_<stage>`. Table names are not declared in a descriptor:
Silver tables are the entities of the domain's Floe files and Gold tables are
the product's dbt models.

Renaming a source, resource, domain, product or table changes these names, so
on a deployed stage plan it as a data migration. `displayName`, `description`
and `status` are metadata only.

## Validation errors

```bash
olf check contracts
```

| Error | Cause |
| --- | --- |
| Name must match source directory | `bronze/marketing_platform/source.yaml` says `name: marketing`. |
| Invalid identifier | A name such as `campaign-performance`; use `campaign_performance`. |
| Duplicate product id | Two products declare the same `id`. |
| Product names an unknown domain | A product's `domain` is not in `domains`. |
| Domain or product has no directory | `silver/<domain>/` or `gold/<product>/` is missing, or exists without an entry in `governance.yml`. |
| Silver entity reads an unknown Bronze resource (`olf check project-code`) | A Floe entity's `source.ref`/`resource` is not a resource of a `source.yaml`. |
| Model outside `<product>_gold` | A dbt model or project sets its own schema and a manifest shows it. |

`olf check contracts` reads each descriptor on its own. `olf check
project-code` loads the whole inventory, and catches a reference between
descriptors that does not resolve.

`olf init --empty` writes no `governance.yml`; the first `olf domain new` or
`olf product new` creates it. Only the scaffolding commands accept a project
without one; every check and deploy needs at least one product.

## Graph the lineage

```bash
olf project graph --project .
```

This prints the ownership chain the descriptors declare as Mermaid text:

| Edge | Read from |
| --- | --- |
| Source → Domain | `source.ref` of the domain's Floe entity files |
| Domain → Product | `products[].domain` |
| Product → Dashboard | the `<product>_gold` schema of the dashboard's dataset files |

Nodes and edges are sorted, so the same descriptors always print the same text.
Paste it into a `mermaid` code block on GitHub or into the
[Mermaid Live Editor](https://mermaid.live) to see it drawn. For the CRM/ERP
demo it prints:

```text
flowchart LR
    dashboard_sales_customer_health["sales_customer_health"]
    dashboard_sales_order_revenue["sales_order_revenue"]
    dashboard_supply_chain_inventory_reliability["supply_chain_inventory_reliability"]
    domain_sales["Sales"]
    domain_supply_chain["Supply Chain"]
    product_customer_health["Sales Customer Health"]
    product_inventory_reliability["Supply Chain Inventory Reliability"]
    product_order_revenue["Sales Order Revenue"]
    source_crm["CRM"]
    source_erp["ERP"]
    domain_sales --> product_customer_health
    domain_sales --> product_order_revenue
    domain_supply_chain --> product_inventory_reliability
    product_customer_health --> dashboard_sales_customer_health
    product_inventory_reliability --> dashboard_supply_chain_inventory_reliability
    product_order_revenue --> dashboard_sales_order_revenue
    source_crm --> domain_sales
    source_erp --> domain_supply_chain
```
