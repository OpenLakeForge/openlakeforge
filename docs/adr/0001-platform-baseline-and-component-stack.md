# ADR 0001: Platform baseline and component stack

## Status

Binding.

## Context

OpenLakeForge is a self-hosted lakehouse platform for small data teams. It has
to prove a complete ingestion-to-analytics path while staying open-source,
portable across local and cloud Kubernetes, and small enough that a team without
a platform engineer can run it.

That target audience constrains component choice more than a generic
"enterprise distribution" framing would: every always-on service is runtime
footprint a small team pays for, and every component is one more thing they have
to understand when it breaks.

## Decision

### Baseline

- **Kubernetes-native** from the beginning, deployed with Terraform and Helm.
- **Cloud-agnostic** across local, on-prem, and cloud Kubernetes. Portability is
  expressed through provider contracts (ADR 0003), not through per-provider
  forks.
- **Batch-first.** Streaming is out of scope before `v1.0`.
- **Apache Iceberg** is the table format. Storage stays independent of the
  engines operating on it.

### Component stack

| Capability | Component | Notes |
| --- | --- | --- |
| Extraction | dlt | Writes Bronze from source systems |
| Validation & Silver materialization | Floe | Owns Bronze→Silver; runs as one Kubernetes Job per domain on the project-code image |
| Table format | Apache Iceberg | — |
| Gold transformation | dbt-trino | Executes through Trino; see below |
| Query engine | Trino | Both the Gold compute engine and the analytics query path |
| Orchestration | Dagster | See ADR 0006 |
| Catalog | Apache Polaris (local, Azure) / AWS Glue (AWS) | ADR 0003, ADR 0010 |
| Object storage | SeaweedFS (local) / AWS S3 (AWS) | Per-layer buckets, ADR 0004 |
| Metadata database | In-cluster PostgreSQL / AWS RDS | Dagster, OpenMetadata, Superset, Polaris |
| Governance | OpenMetadata *(optional)* | ADR 0007 |
| Dashboards | Superset *(optional)* | — |

The data path is:

```text
CSV / source
  -> dlt            -> Bronze
  -> Floe           -> Silver Iceberg
  -> dbt-trino      -> Gold Iceberg
  -> Trino          -> Superset
```

### Trino is the Gold compute engine

Gold dbt models run through Trino's Iceberg connector with `table`
materialization and `on_table_exists: replace`, producing Iceberg
`CREATE OR REPLACE TABLE` commits. A failed replacement preserves the previous
snapshot.

This replaced an earlier dbt-duckdb implementation, which needed a custom
Iceberg materialization, a Glue UDF/plugin, and dbt-held catalog credentials,
and whose Gold replacement was non-atomic. Routing Gold through the Trino
service that already exists removes all four.

Trino exposes the provider catalog name (normally `lakehouse_dev`) as a second
catalog alias sharing the `iceberg` connector configuration. dbt uses that
canonical alias for execution and for its manifest database, so lineage events
carry names that resolve to real catalog entities.

Consequence: Trino is sized for transformation load, not only for interactive
queries.

### Local object storage is SeaweedFS

The first implementation used Garage. Polaris writes through AWS SDK v2 failed
against it until optional S3 checksum behaviour was disabled with a JVM system
property, the chart had to be maintained in-repo, and the failure surfaced in
Trino as a catalog error even though the cause was S3 compatibility — an
actively misleading first-run experience for a contributor.

SeaweedFS is used instead: mature upstream Helm chart, no S3-compatibility
workarounds. Garage is not a bad project; it is not the right default while the
platform is trying to be reproducible for newcomers.

### Superset needs a custom image

The upstream Superset chart ships no Trino or PostgreSQL driver. The platform
builds an image extending `apache/superset` with `trino` and `psycopg2-binary`,
plus a patch for Trino Iceberg partition discovery. Installing drivers at
runtime instead would make startup depend on a package index.

Superset connects to Trino read-only (`allow_dml: false`) and holds no copy of
the data. It uses the shared PostgreSQL for metadata and a chart-managed Redis
for cache and workers.

### Deferred

Vault or External Secrets is a production requirement the platform does not
yet meet; Traefik, cert-manager and the identity issuer (Dex, ADR 0014) are
built for the local profile only. Local and POC
deployments use development credentials, Kubernetes Secrets, and
`kubectl port-forward`. These are named here so their absence reads as a known
gap rather than an oversight; the roadmap schedules them for the secure beta
profile.

## Consequences

Slim and Full are the two supported footprints from one codebase. Slim is the
core path above; Full adds OpenMetadata and Superset. The optional layers change
nothing about ingestion-to-Gold, and e2e assertions for a disabled layer are
skipped rather than failed.

Every component version and chart digest is pinned in
`release/component-catalog.yaml` and verified before Terraform applies it.

## History

Merges the decisions previously recorded as ADR 0001 (v1 platform baseline),
0002 (SeaweedFS), 0007 (Superset over Trino), and 0018 (Trino Gold
materialization, which superseded ADR 0005's dbt-duckdb Gold).

2026-10-28: Floe moved into the project-code image (`floe-python` 0.8,
`dagster-floe`) and runs as one Kubernetes Job per domain on that image; the
separate Floe runner image is removed (#533).
