# Source descriptor reference

`lakehouse_code/bronze/<source>/source.yaml` declares one Bronze source and the
resources it exposes. This page lists every field, the rules `olf` enforces and
the namespaces derived from a source.

The descriptor is validated against `docs/schema/source.schema.json` and by the
canonical inventory in `packages/domain-model`. It names a logical source only:
Kubernetes resources, catalog implementations, storage endpoints and
credentials do not belong in it, because provider contracts resolve those (see
[ADR 0004](../adr/0004-medallion-layout-and-catalog-namespaces.md)).

---

## Example

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

It lives at `lakehouse_code/bronze/marketing_platform/source.yaml`, and the
source must also be listed under `sources` in `lakehouse_code/lakehouse.yaml`
(see [Lakehouse descriptor](lakehouse-descriptor.md)).

---

## Fields

Unknown fields are rejected at every level.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | Must be `openlakeforge.io/v1alpha3`. |
| `kind` | string | yes | Must be `Source`. |
| `name` | string, `^[a-z][a-z0-9_]*$` | yes | Stable machine identifier. Must equal the name of the directory holding the descriptor, and be unique across the lakehouse. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | yes | Human-readable description; may be empty. |
| `status` | string, non-empty | yes | Lifecycle status. There is no fixed set; typical values are `planned`, `active` and `deprecated`. |
| `resources` | array, at least one | yes | The Bronze resources the source exposes. |

### `resources[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string, `^[a-z][a-z0-9_]*$` | yes | Resource identifier, unique within the source. |
| `description` | string | no | Human-readable description. |

The descriptor does not define a resource's schema: that belongs to the Floe
contract of each domain that consumes it. It also holds no physical path, so a
provider-specific location such as `s3://...` must not be written here.

---

## Rules

* The `name` must match the containing directory: for
  `lakehouse_code/bronze/marketing_platform/source.yaml`, `name` must be
  `marketing_platform`.
* The lakehouse's `sources` list must match the discovered `source.yaml` files
  exactly: every declared source needs a descriptor, and every descriptor found
  on disk must be declared.
* A domain's Silver table maps to a source resource with `source` and
  `resource`; the pair must name a resource declared here.

## Derived names

A source `name` gives the Bronze namespace `<source>_bronze` in each stage's
catalog, and each resource is a table in it:

```text
<stage_catalog>.<source>_bronze.<resource>
lakehouse_dev.crm_bronze.orders
```

Renaming a source or a resource changes that identity, so plan it as a
data and catalog migration rather than an edit. `displayName`, `description`
and `status` are metadata only.

---

## Validate

```bash
uv run --project tools/olf --locked olf check contracts
```

This checks every `lakehouse_code/bronze/*/source.yaml` through the canonical
model and the JSON Schema.

| Error | Cause | Fix |
| --- | --- | --- |
| Source list does not match discovered descriptors | `lakehouse.yaml` lists `crm` and `erp` but only `bronze/crm/source.yaml` exists, or the reverse. | Make the list and the directories agree. |
| Name must match source directory | `bronze/marketing_platform/source.yaml` says `name: marketing`. | Use `name: marketing_platform`. |
| Invalid identifier | A name such as `campaign-performance`. | Use lowercase letters, digits and underscores, starting with a letter. |
| Silver mapping names an undeclared resource | A domain maps to `{source: crm, resource: made_up}` and `crm` has no `made_up`. | Declare the resource or fix the mapping. |

---

## Related

* [Lakehouse descriptor reference](lakehouse-descriptor.md)
* [ADR 0004: medallion ownership and catalog namespaces](../adr/0004-medallion-layout-and-catalog-namespaces.md)
* [Source JSON Schema](../schema/source.schema.json)
* [Provider contracts](../architecture/provider-contracts.md)
* [`olf` CLI](../../tools/olf/README.md)
