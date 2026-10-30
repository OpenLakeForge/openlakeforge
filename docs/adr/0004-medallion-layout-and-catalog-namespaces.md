# ADR 0004: Medallion layout, ownership, and catalog namespaces

## Status

Binding.

## Context

The platform originally organised user code by domain: `domains/<domain>/`
contained that domain's Bronze, Silver, Gold, reports, and a `domain.yaml`
describing all of it. Every downstream namespace followed the product —
`{asset_prefix}_silver` and `{asset_prefix}_gold` — which made the *product* the
owner of all three medallion layers.

That does not match how the medallion architecture actually divides ownership,
and the mismatch had costs:

- **Bronze is not domain-owned.** It is a raw representation of an external
  source, reused by whatever domains need it. Nesting it under one domain meant
  ingesting the same CRM feed twice when two domains wanted different slices,
  duplicating both the extract and the storage.
- **Silver is not product-owned.** Validated entities are a domain asset. Keying
  the Silver namespace on `asset_prefix` gave two products in the same domain two
  separate Silver namespaces for the same conceptual table.
- **Only Gold is genuinely product-owned.**

## Decision

### `lakehouse_code/` replaces `domains/`

User code is organised by medallion layer, and each layer names its owner:

```text
lakehouse_code/
├── bronze/<source>/          source-owned: source.yaml + dlt extract
├── silver/floe.yml, sources.yml   the Floe project and its named Bronze sources
├── silver/<domain>/          domain-owned: Floe domain defaults and one file per entity
├── gold/<product>/           product-owned: dbt project
├── dashboards/superset/<dashboard>/   consumption-owned: exported report bundles
├── pipelines/dagster/        user-maintained orchestration
└── governance.yml            the descriptor (ADR 0005)
```

The domain-oriented layout is **removed**, not deprecated. `domains/`,
`domain.yaml`, and `asset_prefix` do not exist.

### Ownership rule

```text
Source   ->  <source>_bronze
Domain   ->  <domain>_silver
Product  ->  <product>_gold
```

A Bronze source is ingested once and referenced by any domain. A domain
validates its sources into one Silver namespace. A product consumes Silver
tables from its domain and owns only its Gold namespace. Product `id` is
unique across the whole lakehouse, so `<product>_gold` needs no domain prefix.

The namespace contract is **flat** — a single-level name, not a hierarchy. This
is deliberate: it is the shape both Polaris and AWS Glue implement natively, so
the identical namespace names work on either catalog with no translation layer.

### Per-layer buckets

Each medallion layer gets its own bucket rather than a path prefix inside one:

| Bucket (local DEV) | Layer | Written by |
| --- | --- | --- |
| `lakehouse-dev-bronze` | Bronze | dlt |
| `lakehouse-dev-silver` | Silver | Floe |
| `lakehouse-dev-gold` | Gold | dbt-trino |
| `openlakeforge-ops` | operational artifacts | the artifacts phase |

Buckets are the unit at which IAM policies, lifecycle rules, retention, and
replication apply. A single bucket cannot grant BI consumers read access to Gold
without also exposing raw Bronze, and cannot give raw CSVs a different retention
policy from validated tables. Path prefixes inside one bucket cannot express
either.

Because the layer is implied by the bucket, table paths carry no `bronze/`,
`silver/`, or `gold/` prefix.

### Operational artifact prefixes

`openlakeforge-ops` is outside the medallion split and uses stable prefixes:

```text
floe/reports/<domain>/
logs/
run-artifacts/dbt/<domain>/<product>/
```

Floe run reports are keyed by domain, matching domain-owned Silver, and land
under the stage's own artifact prefix (`activations/<stage>/`) once a stage is
activated.

The local observability adapter is `observability.object_log_archive`: metrics
and tracing are disabled, and logs, Floe reports, and dbt run artifacts are
archived to object storage. A Loki/Grafana/Prometheus stack is a later
observability adapter, not a replacement for this baseline.

## Consequences

Adding a product that reuses an existing source touches
`lakehouse_code/gold/<product>/`, `governance.yml`, and one Dagster module at
`lakehouse_code/pipelines/dagster/<product>.py`; no Bronze or Silver work is
duplicated.

Catalog namespaces are derived from descriptors, so they are reconciled in the
artifacts phase by `olf infra catalog sync-namespaces` (ADR 0002), never templated into
Terraform.

Migrating an installation from the `domains/` layout is a manual rewrite. There
is no automated path and no parser for the old descriptor shape (ADR 0005).

## History

Merges the decisions previously recorded as ADR 0013 (medallion bucket split),
0014 (ops artifact bucket and prefixes), and 0026 (medallion ownership and the
flat catalog namespace contract, which superseded ADR 0021's product-owned
Silver).

Bucket names follow `lakehouse-<stage>-<layer>` for every stage, matching the
`lakehouse_<stage>` catalogs. DEV previously kept the v0.2 names
(`lakehouse-bronze`, `-silver`, `-gold`) and the other stages used
`<profile>-<stage>-<layer>`; the local and Azure environments now use the single scheme,
and the AWS environment is still on the old one.

2026-10-28 (#533): Floe 0.8 builds its manifests in memory in the code
location, so `floe/manifests/` is no longer written; the Silver contracts
became one multi-file Floe project.

2026-10-29 (#545): the descriptor is `governance.yml`, which names domains and
products only. A product no longer lists its Silver inputs or Gold tables; the
schema (`<domain>_silver`, `<product>_gold`) decides which domain or product a
table belongs to.
