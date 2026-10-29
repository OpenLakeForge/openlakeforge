# ADR 0005: The descriptor model

## Status

Binding.

## Context

The descriptor is the contract between a user's data products and everything the
platform derives from them: Dagster jobs, catalog namespaces, artifact paths,
Terraform inputs, and e2e expectations. Two problems shaped its current form.

First, the platform originally parsed descriptors twice — once in the `olf` CLI
and once in the project-code runtime. The two implementations had to be kept in
parity by hand while naming, validation, and identity rules were still moving.

Second, the descriptor kept facts that other tools already own. It listed every
Silver and Gold table, which Floe's entity files and dbt's models also define,
so each table had two sources of truth and every scaffold or rename had to
update both. Business metadata — domains and products — is the one thing
nothing else knows.

## Decision

### Two descriptors

| Kind | Path | Declares |
| --- | --- | --- |
| `Governance` (`openlakeforge.io/v1beta1`) | `lakehouse_code/governance.yml` (one per project) | Domains, and the data products that name their domain |
| `Source` (`openlakeforge.io/v1alpha3`) | `lakehouse_code/bronze/<source>/source.yaml` (one per source) | A Bronze source and the resources it exposes |

A product's `id` is its only identity and is unique across the lakehouse. A
table belongs to a domain or product by the schema it lives in, not by a list:
`<domain>_silver` belongs to the domain, `<product>_gold` to the product and,
through it, the product's domain. Silver tables are the entities of the
domain's Floe files; Gold tables are the product's dbt models; neither is
declared in a descriptor. A dbt model that writes outside `<product>_gold` is
rejected, because its table would belong to no product.

Sources are the `bronze/*/source.yaml` files on disk, and dashboards are the
`dashboards/superset/*/` directories that hold a `metadata.yaml`; a dashboard
serves the products whose `<product>_gold` schema its datasets read. None of
these is declared a second time.

Both descriptors are validated against JSON Schema
(`docs/schema/governance.schema.json`, `docs/schema/source.schema.json`) and
against the typed model. The schemas are closed against unknown fields, which
is what enforces provider-neutrality (ADR 0003): a `bucket` or `catalog` key is
rejected rather than ignored.

### `openlakeforge-domain-model` is the only implementation

The `packages/domain-model/` distribution, imported as `openlakeforge_domain`,
owns descriptor constants, loading, validation, the provider-neutral value
objects (`Source`, `Domain`, `Product`, `Table`, `FloeEntity`, `Dashboard`,
`LakehouseInventory`), logical-to-physical name derivation, and artifact-path
derivation. Both `olf` and
the project-code image install it.

The package has no Dagster, Terraform, or provider dependency. Dagster-specific
discovery — importing product modules and merging their `Definitions` — is a
runtime adapter in `libs/`, not part of the model.

### v1alpha1 and v1alpha2 are removed

The legacy `Domain` descriptor, its loaders and validators, and its schemas are
deleted. Nothing in the platform parses the old shape.

Upgrading a project from `v0.1.0-alpha.1` means rewriting its descriptors by
hand into the `Governance` + `Source` pair. This is a real cost, accepted for two
reasons: the ownership model changed underneath the descriptor, so a mechanical
field-by-field migration would produce a syntactically valid descriptor with the
wrong Silver namespaces; and carrying a second descriptor model that no runtime
path can reach is a standing invitation for it to drift out of parity with the
one that matters — the exact failure the single-implementation decision above
exists to prevent.

Alpha releases carry no forward-compatibility guarantee, and the affected
population is small enough that a documented manual rewrite is the honest
option.

### Transitional projects

`olf init --empty` writes no `governance.yml`. It deliberately fails strict
validation: a project with no product cannot deploy or run e2e. Only the
scaffolding commands (`olf source new`, `olf domain new`, `olf product new`)
accept it. The first `olf domain new` or `olf product new` creates
`governance.yml`, and the first `olf product new` is what turns the project
into a schema-valid one.

## Consequences

A descriptor change alone moves what the platform discovers — jobs, namespaces,
artifact paths, and e2e assertions — with no edit to shared platform code. That
property is what `olf check contracts` protects on every pull request.

The inventory is cached per resolved project root, so repeated loads within one
process are free.

## History

Merges the decisions previously recorded as ADR 0021 (the inventory-required
descriptor shape), 0024 (the canonical domain-model package), 0026's descriptor
half, and 0032's transitional-project rule. ADR 0026 previously required keeping
the v1alpha1/v1alpha2 parsers for migration diagnostics; that requirement is
withdrawn here.

2026-10-29 (#545): `lakehouse.yaml` (`Lakehouse`, `v1alpha3`) is replaced by
`governance.yml` (`Governance`, `v1beta1`), which names domains and products
only. The per-domain `silver_tables`, per-product `silver_inputs` and
`gold_tables`, `sources` and `dashboards` lists are removed: tables follow the
schema rule above, sources and dashboards are found from their directories, and
no host command needs a Gold table name. `olf e2e` lists the
tables in a stage's `<product>_gold` and `<domain>_silver` schemas through
Trino. A project still on `lakehouse.yaml` does not load; rewrite it as
`governance.yml` (alpha, no production deployments).
