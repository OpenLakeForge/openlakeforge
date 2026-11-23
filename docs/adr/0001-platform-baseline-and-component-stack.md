# ADR 0001: Platform baseline and component stack

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS and Azure: proof of concept (ADR 0010) |

## Context

OpenLakeForge is a self-hosted lakehouse platform for small data teams. It has
to prove a complete ingestion-to-analytics path while staying open-source,
portable across local and cloud Kubernetes, and small enough that a team
without a platform engineer can run it. That audience constrains component
choice more than an "enterprise distribution" framing would: every always-on
service is footprint a small team pays for, and every component is one more
thing they have to understand when it breaks.

## Decision

**D1. Kubernetes-native, deployed with Terraform and Helm.** The platform runs
on Kubernetes from the first release and is deployed with Terraform and Helm.
Enforced by: the roots under `infra/terraform/environments/`.

**D2. Cloud-agnostic across local, on-prem and cloud Kubernetes.** Portability
is expressed through provider contracts (ADR 0003), never through
per-provider forks. Enforced by: `olf check contracts`.

**D3. Batch-first.** Streaming is out of scope before `v1.0`. Enforced by:
scope; no streaming component is in `release/component-catalog.yaml`.

**D4. Apache Iceberg is the table format.** Storage stays independent of the
engines that operate on it. Enforced by: Trino's `iceberg` connector in
`infra/terraform/modules/query/trino/main.tf`.

**D5. The component stack is fixed per capability.** One component fills each
capability, and the data path is `source -> dlt (Bronze) -> Floe (Silver
Iceberg) -> dbt-trino (Gold Iceberg) -> Trino -> Superset`. Enforced by:
`release/component-catalog.yaml`; versions are in
[Components and services](../reference/components.md).

| Capability | Component |
| --- | --- |
| Extraction | dlt, writing Bronze from source systems |
| Validation and Silver | Floe; owns Bronze to Silver and runs as one Kubernetes Job per domain on the project-code image |
| Gold transformation | dbt-trino, executing through Trino (D6) |
| Query engine | Trino: the Gold compute engine and the analytics query path |
| Orchestration | Dagster (ADR 0006) |
| Catalog | Apache Polaris (local, Azure) · AWS Glue (AWS) (ADR 0003, ADR 0010) |
| Object storage | SeaweedFS (local, D7) · Amazon S3 (AWS); per-layer buckets (ADR 0004) |
| Metadata database | In-cluster PostgreSQL · Amazon RDS; for Dagster, OpenMetadata, Superset and Polaris |
| Governance | OpenMetadata, optional (ADR 0007) |
| Dashboards | Superset, optional (D8) |
| Ingress | Traefik, the one ingress for every route (ADR 0013); local, when the profile sets `spec.access` |
| Certificates | cert-manager, issuing route certificates and Trino's internal certificate (ADR 0013, ADR 0016); local, when the profile sets `spec.access` |
| Sign-in issuer | Dex, federating GitHub teams (ADR 0014); local, when `spec.identity.issuer` is `github` |
| Perimeter | oauth2-proxy, checking a person's sign-in and role in front of the routes (ADR 0014); local, when the profile sets `spec.identity` |
| Pod log collection | Fluent Bit, a DaemonSet archiving pod logs to the ops bucket (ADR 0022); local, Slim and Full |
| Metrics, optional | Prometheus with a restricted kube-state-metrics (ADR 0024); local, on by default in Full, or with `spec.observability.metrics.enabled` |

**D6. Trino is the Gold compute engine.** Gold dbt models run through Trino's
Iceberg connector with `table` materialization and `on_table_exists:
replace`, so each build is an Iceberg `CREATE OR REPLACE TABLE` commit and a
failed replacement keeps the previous snapshot. dbt executes against, and
records in its manifest, the provider catalog name (`lakehouse_<stage>`), so
lineage events carry names that resolve to real catalog entities. Trino is
sized for transformation load, not only for interactive queries. Enforced by:
`lakehouse_code/gold/dbt_project.yml`, checked by
`tools/olf/tests/common/test_dbt.py`.

**D7. Local object storage is SeaweedFS**, from its upstream Helm chart with no
S3-compatibility workarounds. Enforced by: the `seaweedfs` chart pin in
`release/component-catalog.yaml`.

**D8. Superset runs a custom image and reads Trino read-only.** The image
extends `apache/superset` with the `trino` and `psycopg2-binary` drivers and a
patch for Trino Iceberg partition discovery, so startup never depends on a
package index. Superset connects to Trino with `allow_dml: false`, holds no
copy of the data, and uses the platform PostgreSQL for metadata and a
chart-managed Redis for cache and workers. Enforced by:
`images/superset/Dockerfile` and the database template in
`tools/olf/olf/common/scaffold/_templates.py`.

**D9. Slim and Full are the two supported footprints from one codebase.** Slim
is the core path; Full adds OpenMetadata and Superset. The optional layers
change nothing about ingestion to Gold, and e2e assertions for a disabled
layer are skipped, not failed. Enforced by: the `slim` and `full` presets in
`tools/olf/olf/common/profile.py`.

**D10. Every component version and chart digest is pinned in
`release/component-catalog.yaml`** and verified before Terraform applies it.
Enforced by: `olf check components`.

## Consequences

- Production secret management (Vault or External Secrets) is a requirement
  the platform does not yet meet: local and proof-of-concept deployments use
  development credentials and Kubernetes Secrets. Traefik, cert-manager and
  the identity issuer (Dex, ADR 0014) are built for the local provider only.
  The roadmap schedules these for the secure beta profile.
- AWS keeps Glue until it moves to Polaris (#681, ADR 0019).

## Boundaries

| Not decided here | Where |
| --- | --- |
| How services are reached (ingress, routes, port-forward fallback) | ADR 0013 |
| Identity, sign-in and the perimeter | ADR 0014 |
| Pod logs, alerts and metrics | ADR 0022, ADR 0023, ADR 0024 |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| dbt-duckdb for Gold | Needed a custom Iceberg materialization, a Glue UDF or plugin and dbt-held catalog credentials, and its Gold replacement was not atomic |
| Garage for local object storage | Polaris writes through AWS SDK v2 failed until a JVM property disabled optional S3 checksums, the chart had to be kept in-repo, and the failure surfaced in Trino as a misleading catalog error |
| Installing Superset's drivers at runtime | Startup would depend on a package index |

## History

- Undated: New record, merging the former ADRs 0001 (v1 platform baseline),
  0002 (SeaweedFS), 0007 (Superset over Trino) and 0018 (Trino Gold
  materialization, which replaced ADR 0005's dbt-duckdb Gold).
- 2026-10-28 (#533): D5 changed: Floe runs as one Kubernetes Job per domain on
  the project-code image; the separate Floe runner image is removed.
- 2026-11-22 (#1387): Rewritten in the standard form; no decision changed.
- 2026-11-23: D5 lists the access, identity and observability components.
