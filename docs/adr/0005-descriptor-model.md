# ADR 0005: The descriptor model

| | |
| --- | --- |
| Status | Binding |
| Built | ✔ |

## Context

The descriptor is the contract between a user's data products and everything
the platform derives from them: Dagster jobs, catalog namespaces, artifact
paths, Terraform inputs and e2e expectations. Two problems shaped it. The
platform once parsed descriptors twice, in `olf` and in the project-code
runtime, and kept the two in parity by hand. And the descriptor listed every
Silver and Gold table, which Floe's entity files and dbt's models already
define, so each table had two sources of truth. Business metadata (domains
and products) is the one thing nothing else knows.

## Decision

**D1. A project has three descriptors.** Each has a closed JSON Schema and a
typed model. Field-by-field reference: [Descriptor reference](../reference/descriptors.md).
Enforced by: `olf check contracts`.

| Kind | Path | Declares |
| --- | --- | --- |
| `Governance` (`openlakeforge.io/v1beta2`) | `lakehouse_code/governance.yml`, one per project | Domains, and the data products nested under them |
| `Source` (`openlakeforge.io/v1beta1`) | `lakehouse_code/bronze/<source>/source.yaml`, one per source | A Bronze source and the resources it exposes |
| `AlertPolicy` (`openlakeforge.io/v1beta1`) | `lakehouse_code/alerts.yml`, optional, one per project | Which conditions alert for which products and stages, and through which routes (ADR 0023) |

**D2. A table belongs to a domain or product by its schema, not by a list.** A
product's `id` is its only identity and is unique across the lakehouse.
`<domain>_silver` belongs to the domain; `<product>_gold` belongs to the
product and, through it, to the product's domain. Silver tables are the
entities of the domain's Floe files and Gold tables are the dbt models in the
product's folder of the Gold project (ADR 0004); neither is declared in a
descriptor. A dbt model that writes outside `<product>_gold` is rejected,
because its table would belong to no product. Enforced by:
`tools/olf/olf/common/commands/checks/_dbt.py`.

**D3. Nothing is declared twice.** Sources are the `bronze/*/source.yaml` files
on disk, and dashboards are the `dashboards/superset/*/` directories that hold
a `metadata.yaml`; a dashboard serves the products whose `<product>_gold`
schema its datasets read. A product's Dagster definition defaults its Gold
assets, Silver inputs and Bronze inputs to the dbt models, the dbt sources and
the Floe entities' Bronze resources; an explicit list still wins. Enforced
by: `packages/core/openlakeforge_core/inventory.py` and `ProductDefinitionSpec`
in `packages/runtime/openlakeforge_runtime/product_dagster.py`.

**D4. Descriptors are closed against unknown fields.** Each is validated against
its JSON Schema (`docs/schema/governance.schema.json`,
`docs/schema/source.schema.json`, `docs/schema/alert-policy.schema.json`) and
its typed model. Closure is what enforces provider-neutrality (ADR 0003): a
`bucket` or `catalog` key is rejected, not ignored. Enforced by: the schemas
and `olf check contracts`.

**D5. Load modes are declared where each layer is owned.** A `Source` resource
declares `load.mode`: `full` (the default) or `incremental`, with a `cursor`
and a `primary_key`, which are column names, never values or queries; the
connector still owns how it reads the cursor. Silver's mode stays in the Floe
contract and Gold's in the dbt model, because Floe and dbt own those layers.
Enforced by: `olf check contracts`, which checks that the Bronze and Silver
modes fit together.

**D6. `openlakeforge-core` is the only implementation.** The `packages/core/`
distribution, imported as `openlakeforge_core`, owns descriptor constants,
loading, validation, the provider-neutral value objects (`Source`, `Domain`,
`Product`, `Table`, `FloeEntity`, `Dashboard`, `LakehouseInventory`),
logical-to-physical name derivation and artifact-path derivation. Both `olf`
and the project-code image install it. It has no Dagster, Terraform or
provider dependency; Dagster-specific discovery is a runtime adapter in
`openlakeforge_runtime/`, not part of the model. Enforced by: the dependency
list of `packages/core/pyproject.toml`.

**D7. A superseded descriptor version is refused, never read beside the
current one.** A document on a previous beta version fails with a pointer to
`olf project migrate`, which rewrites it textually, keeping comments and
order; for `governance.yml` it refuses on an unknown domain, a duplicate id or
an already nested domain. The alpha shapes (the `Domain` descriptor of v1alpha1 and v1alpha2,
and `lakehouse.yaml`) have no parser and no migration: a project on them is
rewritten by hand. Enforced by: `packages/core/openlakeforge_core/governance.py`,
`descriptors.py` and `tools/olf/olf/common/descriptor_migration.py`.

**D8. `olf init --empty` makes a transitional project.** It writes no
`governance.yml` and deliberately fails strict validation: a project with no
product cannot deploy or run e2e. Only the scaffolding commands (`olf source
new`, `olf domain new`, `olf product new`) accept it. The first `olf domain
new` or `olf product new` creates `governance.yml`, and the first `olf
product new` makes the project schema-valid. Enforced by: the scaffolding
commands in `tools/olf/olf/common/scaffold/`.

## Consequences

- A descriptor change alone moves what the platform discovers (jobs,
  namespaces, artifact paths, e2e assertions) with no edit to shared platform
  code; `olf check contracts` protects that on every pull request.
- The inventory is cached per resolved project root, so repeated loads in one
  process are free.
- Upgrading a project from an alpha descriptor is a manual rewrite (D7). Alpha
  releases carry no forward-compatibility guarantee.

## Boundaries

| Not decided here | Where |
| --- | --- |
| The `lakehouse_code/` layout, buckets and catalog namespaces | ADR 0004 |
| How Dagster loads products into a code location | ADR 0006 |
| What a project revision contains | ADR 0012 |
| The alert policy's semantics | ADR 0023 |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Parsing descriptors separately in `olf` and the runtime | Two implementations kept in parity by hand while naming, validation and identity rules still moved |
| Listing Silver and Gold tables, sources and dashboards in the descriptor (`lakehouse.yaml`) | Two sources of truth per table; every scaffold or rename updated both |
| A mechanical migration from the alpha `Domain` descriptor | The ownership model changed underneath it, so it would produce a valid descriptor with the wrong Silver namespaces |
| Keeping the v1alpha1 and v1alpha2 parsers for migration diagnostics | A second model no runtime path reaches drifts out of parity with the one that matters |
| Accepting two `Governance` layouts side by side | Every consumer would handle both; a version change fails with a pointer to `olf project migrate` instead |

## History

- Undated: New record, merging the former ADRs 0021 (inventory-required
  descriptor shape), 0024 (the canonical core package), 0026's descriptor half
  and 0032's transitional-project rule (D8); 0026's requirement to keep the
  v1alpha1 and v1alpha2 parsers is withdrawn and both are removed.
- 2026-10-29 (#545): D2 and D3 added: `governance.yml` (`Governance`
  v1beta1) replaces `lakehouse.yaml` (`Lakehouse` v1alpha3) and names domains
  and products only; table, source and dashboard lists are removed.
- 2026-10-30 (#342, #343): D5 added: `Source` resources gain an optional
  `load` block.
- 2026-11-07 (#917): D1 and D7 changed: `Governance` is `v1beta2`, products
  nest under their domain, and `v1beta1` fails with a pointer to `olf project
  migrate`.
- 2026-11-07 (#995): D1 and D7 changed: `Source` is `v1beta1`; `v1alpha3`
  fails with a pointer to `olf project migrate`.
- 2026-11-07 (#994): D3 changed: a product's Dagster definition defaults its
  assets and inputs to what dbt and Floe declare.
- 2026-11-17 (#1205): D1 changed: `alerts.yml` (`AlertPolicy` v1beta1) is the
  third descriptor.
- 2026-11-22 (#1387): Rewritten in the standard form; no decision changed.
