# Descriptor reference

The fields and rules of the two descriptors that declare a project's data:
`lakehouse_code/lakehouse.yaml`, one per project, and
`lakehouse_code/bronze/<source>/source.yaml`, one per source. It also lists the
names OpenLakeForge derives from them.

Both are validated against JSON Schemas,
[`lakehouse.schema.json`](../schema/lakehouse.schema.json) and
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

## `lakehouse.yaml`

```yaml
apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: openlakeforge
displayName: OpenLakeForge
description: Example lakehouse.
status: active
sources:
  - marketing_platform
domains:
  - name: marketing
    displayName: Marketing
    description: Marketing analytics data products.
    status: active
    silver_tables:
      tables:
        - name: campaigns
          source: marketing_platform
          resource: campaigns
          description: Validated campaign data.
    products:
      - id: campaign_performance
        displayName: Campaign Performance
        description: Campaign spend and conversion performance.
        status: active
        silver_inputs:
          - campaigns
        gold_tables:
          tables:
            - name: mart_campaign_performance
              description: Campaign performance aggregated by channel.
dashboards: []
```

### Top level

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | `openlakeforge.io/v1alpha3` |
| `kind` | string | yes | `Lakehouse` |
| `name` | identifier | yes | The lakehouse's machine name. |
| `displayName`, `description`, `status` | as for a source | yes | Metadata. |
| `sources` | array of identifiers, at least one, unique | yes | Must match the `bronze/*/source.yaml` files on disk exactly. |
| `domains` | array, at least one | yes | At least one domain in the lakehouse must declare a product. |
| `dashboards` | array, may be empty | yes | Superset dashboards. |

### `domains[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | identifier | yes | Unique across the lakehouse. |
| `displayName`, `description`, `status` | | yes | Metadata. |
| `silver_tables.tables` | array, at least one, unique | yes | The domain's Silver tables. |
| `silver_tables.tables[].name` | identifier | yes | Unique within the domain. |
| `silver_tables.tables[].source` | identifier | yes | A source listed in `sources`. |
| `silver_tables.tables[].resource` | identifier | yes | A resource that source declares. |
| `silver_tables.tables[].description` | string | no | |
| `products` | array, may be empty | yes | The domain's data products. |

A domain may declare Silver tables before any product reads them; they still
get their namespace and Floe assets.

### `domains[].products[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Unique across the whole lakehouse, not only the domain. |
| `displayName`, `description`, `status` | | yes | Metadata. |
| `domain` | string | no | If written, must equal the enclosing domain's `name`. |
| `silver_inputs` | array of identifiers, at least one, unique | yes | Silver tables of this domain the product reads. |
| `gold_tables.tables` | array, at least one, unique | yes | The Gold tables the product publishes. |
| `gold_tables.tables[].name` | identifier | yes | Unique within the product. |
| `gold_tables.tables[].description` | string | no | |

A table must not carry `fqn` or `fullyQualifiedName`, and `gold_tables` must
not carry `schema`: physical names would tie the descriptor to one provider.

### `dashboards[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | identifier | yes | Unique; also the bundle directory under `dashboards/superset/`. |
| `products` | array of identifiers, at least one, unique | yes | Declared product ids the dashboard reads. |

## Implementation files

Each identity selects the files that implement it:

| Identity | Files |
| --- | --- |
| Source `name` | `lakehouse_code/bronze/<source>/` |
| Domain `name` | `lakehouse_code/silver/<domain>/contracts/floe/<domain>.yml`, the domain's only Floe contract |
| Product `id` | `lakehouse_code/gold/<id>/dbt/` and `lakehouse_code/pipelines/dagster/<id>.py` |
| Dashboard `name` | `lakehouse_code/dashboards/superset/<name>/` |

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
catalog is `lakehouse_<stage>`.

Renaming a source, resource, domain, product or table changes these names, so
on a deployed stage plan it as a data migration. `displayName`, `description`
and `status` are metadata only.

## Validation errors

```bash
olf check contracts
```

| Error | Cause |
| --- | --- |
| Source list does not match discovered descriptors | `sources` lists a source with no `source.yaml`, or a `source.yaml` exists that `sources` does not list. |
| Name must match source directory | `bronze/marketing_platform/source.yaml` says `name: marketing`. |
| Invalid identifier | A name such as `campaign-performance`; use `campaign_performance`. |
| Duplicate product id | Two products declare the same `id`, even in different domains. |
| Silver mapping names an undeclared resource | A Silver table maps to a resource its source does not declare. |
| Empty table group | `silver_tables: {tables: []}`, a product without `silver_inputs`, or one without Gold tables. |
| Physical name in a descriptor | `fullyQualifiedName` or `gold_tables.schema` is set. |

`olf check contracts` reads each descriptor on its own. `olf check
project-code` loads the whole inventory, and catches a reference between
descriptors that does not resolve.

`olf init --empty` writes a `lakehouse.yaml` with empty `sources` and
`domains`. Only the scaffolding commands accept it; every check and deploy
needs at least one product.

## Graph the lineage

```bash
olf project graph --project .
```

This prints the ownership chain the descriptors declare as Mermaid text:

| Edge | Read from |
| --- | --- |
| Source → Domain | `domains[].silver_tables.tables[].source` |
| Domain → Product | `domains[].products[]` |
| Product → Dashboard | `dashboards[].products` |

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
