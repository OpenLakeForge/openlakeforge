# ADR 0007: Governance and lineage

## Status

Binding.

## Context

The platform emits lineage naturally at two points — Floe writing Silver Iceberg
tables, and dbt writing Gold Iceberg tables. Surfacing that in a governed catalog
with end-to-end lineage is the goal. Getting there took two failed attempts,
and what was learned from them is worth more than the final configuration.

## Decision

### OpenMetadata is the governance target, and it is optional

OpenMetadata provides catalog discovery, domain and data-product entities, and
lineage. It is deployed only in the Full profile; Slim omits it entirely and e2e
assertions for it are skipped rather than failed.

The artifacts phase seeds domains, data products, and medallion bucket
containers over REST.

### One OpenMetadata, stage-qualified service roots

A deployment runs one OpenMetadata however many stages enable governance. Stage
is carried by service identity, never by a Domain: every governed stage gets its
own `<catalog service>.lakehouse_<stage>` database root, a `dagster_<stage>`
pipeline service, and a `superset_<stage>` dashboard service when that stage
also enables analytics. The bootstrap removes the services of a stage that
stopped being governed.

The Iceberg service holds one connection, so exactly one stage, the
*canonical* stage, is crawled, and its pipeline service receives OpenLineage
pipelines. Other governed stages are represented by what `olf openmetadata
deploy-metadata` seeds under their own root. The local root registers every
governed stage; the AWS and Azure roots still register one until their
adapters follow (#131).

### Lineage is emitted natively, directly to OpenMetadata

Floe and dbt-trino each emit OpenLineage events straight to OpenMetadata's
`/api/v1/openlineage/lineage` endpoint.

- Floe environment profiles carry the endpoint and receive the ingestion-bot JWT
  only through a Kubernetes Secret reference — never through a generated
  manifest.
- `openlineage-dbt` ships in the project-code image; Dagster sets the product job
  name around the dbt invocation so events carry stable logical identities.
- The dataset namespace matches the catalog identity: `polaris` locally and on
  Azure, `aws_glue` on AWS.
- OpenMetadata owns entity resolution and lineage persistence.

### Two rejected approaches, still rejected

**A normalising OpenLineage proxy.** An earlier design deployed a proxy that
rewrote Floe and dbt events before forwarding them. It ran successfully and
produced incomplete or wrongly-attached lineage, because the real problems were
upstream: Floe emitted non-UUID run IDs and omitted dataset context, and
dbt-duckdb resolved datasets under the local DuckDB file namespace rather than
the Iceberg catalog. The proxy converted those into a silent correctness problem
instead of a visible failure.

**A custom Dagster REST push.** Constructing lineage edges from Dagster and
pushing them directly was rejected for the same reason: it produces lineage that
looks right without the engines actually reporting what they did.

Both remain rejected. The rule they establish: **lineage is emitted by the engine
that performed the write, or it is not emitted.** Anything else is a plausible
graph that nobody has verified.

Native emission only became viable once the upstream causes were fixed — Floe
began passing manifest entities to its lineage observer with resolved Bronze
source URIs and Silver outputs, and Gold moved from dbt-duckdb to dbt-trino,
whose dataset namespaces resolve to the real catalog (ADR 0001).

### The Dagster connector stays deferred

OpenMetadata's Dagster pipeline service and ingestion pipeline are registered but
not triggered: the connector queries the deprecated `PipelineRuns` GraphQL type
while Dagster 1.x exposes `Runs`. Registering without triggering keeps the
service definition in place for when the connector is corrected, without
scheduling an ingestion that would fail on every run.

## Consequences

The complete Bronze→Silver→Gold graph resolves in OpenMetadata when the
governance layer is enabled.

Generated Floe manifests stay credential-free and provider-neutral; this ADR
does not create an exception to that rule.

Superset dashboards appear in OpenMetadata only after its dashboard ingestion
pipeline crawls the Superset API. Report import and metadata crawl are separate
operations, and Superset — not OpenMetadata — is the source of dashboard runtime
state.

## History

Merges the decisions previously recorded as ADR 0006 (OpenMetadata governance
with an OpenLineage proxy), 0009 (deferring lineage, and rejecting both the proxy
and a custom REST push), and 0023 (restoring native emission).

2026-09-23 (#131): stage-qualified service roots replace the single unqualified
`dagster` and `superset` services, and the one-governed-stage limit is lifted
on the local root.
