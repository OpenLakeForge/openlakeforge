# Lakehouse descriptor reference

`lakehouse_code/lakehouse.yaml` is the one descriptor per repository that
declares its sources, domains, data products and dashboards. This page lists
every field, the rules `olf` enforces, and the namespaces and paths derived from
it.

The descriptor is validated against `docs/schema/lakehouse.schema.json` and by
the canonical inventory in `packages/domain-model`. It is the provider-neutral
source of truth for domain and product identity: it must not name Polaris,
Glue, S3, SeaweedFS, Kubernetes resources, endpoints or credentials. Provider
contracts resolve those separately (see
[ADR 0004](../adr/0004-medallion-layout-and-catalog-namespaces.md)).

---

## Ownership model

```text
Bronze  = source-aligned    lakehouse_code/bronze/<source>/
Silver  = domain-aligned    lakehouse_code/silver/<domain>/
Gold    = product-aligned   lakehouse_code/gold/<product>/
```

A domain declares each Silver table's mapping to a Source resource. A product
selects the Silver tables of its domain; it owns neither ingestion nor Bronze
paths. Sources are declared in their own descriptor, see
[Source descriptor](source-descriptor.md).

---

## Example

A `Lakehouse` with one domain and one product consuming it:

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

`olf init --empty` writes a transitional `lakehouse.yaml` with empty `sources`
and `domains` lists. Only the source, domain and product scaffolding accepts it;
`olf check`, deployment and end-to-end commands stay strict until the first
runnable product exists.

---

## Fields

Unknown fields are rejected at every level. Every identifier below matches
`^[a-z][a-z0-9_]*$`.

### Top level

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | Must be `openlakeforge.io/v1alpha3`. |
| `kind` | string | yes | Must be `Lakehouse`. |
| `name` | identifier | yes | The lakehouse's machine name. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | yes | Human-readable description; may be empty. |
| `status` | string, non-empty | yes | Lifecycle status; no fixed set (typical: `planned`, `active`, `deprecated`). |
| `sources` | array of identifiers, at least one, unique | yes | The Bronze source names. It must match the discovered `lakehouse_code/bronze/*/source.yaml` files exactly. |
| `domains` | array, at least one | yes | The domains. At least one domain in the lakehouse must declare a product. |
| `dashboards` | array, may be empty | yes | The dashboards. |

### `domains[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | identifier | yes | Unique across the lakehouse. |
| `displayName`, `description`, `status` | as above | yes | Metadata. |
| `silver_tables` | object | yes | The domain's Silver tables, see below. |
| `products` | array, may be empty | yes | The domain's data products. |

A domain may declare Silver tables before any product consumes them, for
example to seed an `hr` domain from a Workday source. `olf domain new` always
creates a domain this way; `olf product new <domain>/<product>` adds the first
product. A table with no product still gets its `<domain>_silver` namespace and
Floe Silver assets, only no product job selecting it. The lakehouse as a whole
still needs at least one product, because smoke, dbt checks and end-to-end runs
assume one exists.

### `domains[].silver_tables`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tables` | array, at least one, unique | yes | The Silver tables. |
| `tables[].name` | identifier | yes | Silver table name, unique within the domain. |
| `tables[].source` | identifier | yes | A source declared in `sources`. |
| `tables[].resource` | identifier | yes | A resource of that source. |
| `tables[].description` | string | no | Human-readable description. |

### `domains[].products[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | The product's only identity, unique across the whole lakehouse (not only its domain). There is no separate `name` or prefix field. |
| `displayName`, `description`, `status` | as above | yes | Metadata. |
| `domain` | string | no | If written, must equal the enclosing domain's `name`. |
| `silver_inputs` | array of identifiers, at least one, unique | yes | Silver tables of the enclosing domain this product consumes. |
| `gold_tables` | object | yes | The Gold tables the product publishes. |

`gold_tables`:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tables` | array, at least one, unique | yes | The Gold tables. |
| `tables[].name` | identifier | yes | Gold table name, unique within the product. |
| `tables[].description` | string | no | Human-readable description. |

A table entry must not carry `fqn` or `fullyQualifiedName`, and the group must
not carry `schema`: physical names would tie the descriptor to a provider or
environment. Two products can select the same Silver table without duplicating
ingestion or assets.

The `id` selects the product's implementation files:

```text
lakehouse_code/silver/<domain>/contracts/floe/<domain>.yml
lakehouse_code/gold/<id>/dbt/
lakehouse_code/pipelines/dagster/<id>.py
```

### `dashboards[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | identifier | yes | Unique across the lakehouse. |
| `products` | array of identifiers, at least one, unique | yes | Declared product ids the dashboard reads. |

A dashboard is consumption-aligned: its Superset source lives under
`lakehouse_code/dashboards/superset/<dashboard>/`, independent of any domain or
product directory.

---

## Derived names

For a `sales` domain with products `order_revenue` and `customer_health`, and a
`crm` source:

| Resource | Value |
| --- | --- |
| Dagster job (`order_revenue`) | `order_revenue_pipeline` |
| Dagster module | `lakehouse_code.pipelines.dagster.order_revenue` |
| Bronze namespace (source `crm`) | `crm_bronze` |
| Silver namespace (domain `sales`) | `sales_silver`, shared by both products |
| Gold namespace (`order_revenue`) | `order_revenue_gold` |
| Superset source directory (dashboard `sales_order_revenue`) | `lakehouse_code/dashboards/superset/sales_order_revenue` |
| Floe manifest key | `floe/manifests/sales/sales.manifest.json` |
| Floe report prefix | `floe/reports/sales/` |
| dbt artifact prefix | `run-artifacts/dbt/sales/order_revenue/` |

Catalog namespaces are single-level so they work the same for Polaris and AWS
Glue:

```text
<stage_catalog>.<source>_bronze.<resource>
<stage_catalog>.<domain>_silver.<table>
<stage_catalog>.<product>_gold.<table>
```

```text
lakehouse_dev.crm_bronze.orders
lakehouse_dev.sales_silver.orders
lakehouse_dev.order_revenue_gold.mart_order_revenue_by_day
```

A Silver namespace is never derived from a product. The inventory combines
these logical identities with the active provider contract to find physical
storage and catalog locations ([Provider contracts](../architecture/provider-contracts.md)).

Changing a domain `name`, a product `id`, a source `name` or a table name
changes derived namespaces and artifact paths. For a deployed environment, plan
it as a data and catalog migration. `displayName`, `description` and `status`
are metadata only.

---

## Validate

```bash
uv run --project tools/olf --locked olf check contracts
```

This checks `lakehouse_code/lakehouse.yaml` and every `source.yaml` through the
canonical model and the JSON Schemas. The inventory also enforces:

* Source names are unique and match their directory; `sources` matches the
  discovered descriptors.
* Domain names are unique across the lakehouse.
* Product ids are unique across the whole lakehouse.
* Dashboard names are unique, and each product reference resolves to a declared
  product.
* Within a domain, Silver mappings are unique and resolve to declared Source
  resources. Within a product, `silver_inputs` and Gold table names are unique.

| Error | Cause |
| --- | --- |
| Source list does not match discovered descriptors | `sources: [crm, erp]` but only `bronze/crm/source.yaml` exists, or the reverse. |
| Invalid identifier | `id: campaign-performance`; use `campaign_performance`. |
| Duplicate product id | Two products declare `id: order_revenue`, even in different domains. |
| Silver mapping resource not declared | A table maps to `{source: crm, resource: made_up_resource}` and `crm` has no such resource. |
| Empty table group | `silver_tables: {tables: []}`, a product with no `silver_inputs`, or no Gold table. |
| Physical name in a descriptor | `fullyQualifiedName: glue.production.revenue` or `gold_tables.schema`; write only the logical name. |

---

## Graph the lineage

```bash
uv run --project tools/olf --locked olf project graph --project .
```

This prints the ownership chain the descriptors declare as Mermaid text:

| Edge | Read from |
| --- | --- |
| Source → Domain | `domains[].silver_tables` `source` |
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

---

## Related

* [Source descriptor reference](source-descriptor.md)
* [ADR 0004: medallion ownership and catalog namespaces](../adr/0004-medallion-layout-and-catalog-namespaces.md)
* [Lakehouse JSON Schema](../schema/lakehouse.schema.json)
* [Provider contracts](../architecture/provider-contracts.md)
* [`olf` CLI](../../tools/olf/README.md)
