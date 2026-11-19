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

The lakehouse database service (named `polaris`, or `aws_glue` on AWS)
crawls through Trino: OpenMetadata 1.13 removed its Iceberg connector, and in
Trino every stage is one catalog, `lakehouse_<stage>`, which OpenMetadata lists
as one database. The crawl is filtered to the governed stages' catalogs, so
every governed stage is crawled into its own root, as the read-only
`openmetadata` Trino user. `olf infra openmetadata deploy-metadata` adds each stage's
domains and descriptions under that root. It sets a domain on the
`<domain>_silver` and `<product>_gold` schemas, and the tables the crawl finds
inherit it. A data product's assets are the tables under its Gold schema,
attached once the crawl has found them; its Silver inputs appear as lineage.

Every governed stage emits its own lineage. OpenMetadata 1.13+
resolves a lineage dataset by `<service>.<database>.<schema>.<table>`, so a
stage's edges land on its own `lakehouse_<stage>` tables even though schema and
table names are identical across stages; 1.12 matched on schema and table
alone, and could attach one stage's lineage to another's. Each stage emits
under its own `dagster_<stage>` namespace. An edge needs both of its tables registered before the lineage arrives:
OpenMetadata 1.13.6 fails to auto-create the tables and pipelines an event
references (an upstream bug: the entity is inserted without `updatedAt`), so
an event naming an unknown table records no edge. `olf infra openmetadata
deploy-metadata` therefore registers, at activation, each `<domain>_silver`
and `<product>_gold` schema and every table the stage's pipelines write: one
per Floe entity under the Silver schema and one per dbt model under the Gold
schema. A table is only ever created, never overwritten, and the crawl adopts
it by name and fills in its columns. The crawl never deletes schemas or
tables, so a crawl before the stage's first run cannot retire them. Edges are
recorded without a pipeline reference, and nothing may rely on pipeline
entities. The nightly asserts that each stage's
Gold lineage exists and never reaches another stage's database. The local root registers every
governed stage; the AWS and Azure roots still register one until their
adapters follow (#131).

### Lineage is emitted natively, directly to OpenMetadata

Floe and dbt-trino each emit OpenLineage events straight to OpenMetadata's
`/api/v1/openlineage/lineage` endpoint.

- The Floe environment profile the code location renders carries the endpoint;
  each domain's Floe Job receives the ingestion-bot JWT as `OPENLINEAGE_API_KEY`
  through a Kubernetes Secret reference (`secretKeyRef`), and Floe reads it from
  the environment, so the token never enters the profile or the Job spec.
- `openlineage-dbt` ships in the project-code image; Dagster sets the product job
  name around the dbt invocation so events carry stable logical identities.
- The dataset namespace matches the catalog identity: `polaris` locally and on
  Azure, `aws_glue` on AWS. dbt-trino's namespace is the Trino URI it connects
  to, so both of Trino's listeners are mapped to the catalog service: the HTTP
  port, and the HTTPS port dbt uses once workload authentication is on.
- OpenMetadata owns entity resolution and lineage persistence.

### Quality results use the OpenMetadata TestCase APIs

Lineage and quality are different data. Lineage is a statement by the engine
that wrote a table, so it travels the engine's own OpenLineage emission and
nothing else. A quality result is a record the platform already holds: the
normalized per-run manifest the pipeline writes to the stage's ops bucket
(ADR 0023, `run-artifacts/quality/<domain>/<product>/<run>/manifest.json`).
OpenMetadata is a consumer of that record and never reruns Floe or dbt.

- **API.** A short-lived reconciler reads the manifests and publishes them
  through OpenMetadata's `TestDefinition`, `TestCase` and `TestCaseResult`
  routes (`/api/v1/dataQuality/...`) with the shared `OpenMetadataClient`.
  This is the one place the platform writes to the catalog on an engine's
  behalf, and it is narrow: quality only. There is no lineage proxy and no
  lineage REST push; the two rejected approaches below stay rejected.
- **No native dbt ingestion for results.** dbt's `run_results.json` ingestion
  would publish a second copy of what the manifest already carries, covers
  neither Floe nor the WARN/ERROR distinction below, and had a reported
  regression in 1.13.0 (open-metadata/OpenMetadata#29824). Native dbt
  metadata ingestion may enrich the catalog later, provided it does not
  create test results.
- **After the run, never inside it.** Publication runs on its own schedule
  and on demand (`olf`), keyed by stage and Dagster run. A run's outcome never
  depends on OpenMetadata being reachable, and a failed publication is a
  retriable state shown beside the pipeline's quality, not instead of it.
  Slim has no governance layer and no reconciler: no OpenMetadata request is
  ever made and no credential is mounted.
- **Existing tables only, in the result's own stage.** The table FQN is
  `<catalog service>.lakehouse_<stage>.<schema>.<table>`, built from the
  stage the manifest was written for. The reconciler looks the table up and
  publishes only when it exists; an unregistered table keeps the result
  pending until registration, and nothing is created implicitly. A DEV
  manifest can name only DEV FQNs.
- **Stable cases, one result per execution.** A case is named from the engine
  and the check id and lives under the table (or column) it checks, so a
  second run adds a result to the same case. The result's timestamp is the
  engine's observation time in milliseconds, which also makes a replay find the
  result already there.
- **Statuses.** OpenMetadata knows `Success`, `Failed` and `Aborted`. PASS is
  `Success` and a blocking ERROR is `Failed`. An advisory WARN cannot be
  `Success`, which would hide it, and `Failed` opens an incident
  (`TestCaseResultRepository.setTestCaseResultIncidentId`, read on 1.13.6), a
  side effect nobody chose for a check the pipeline survived. WARN therefore
  publishes as `Aborted`, which shows without an incident, and every result
  carries the original outcome, engine severity and pipeline outcome in its
  details so OpenMetadata's coarser status never replaces them. An operator
  who wants incidents for WARN sets the WARN status to `Failed`. `not_run` and
  `unknown` are never published: an unexecuted check has no result.
- **State.** Pending, failed and synced publications are objects in the same
  ops bucket, under `quality-sync/`, keyed by stage and run; a summary there
  carries the sync lag and the last success and failure for the health
  signal. There is no new database.
- **Identity.** Each governed stage publishes as its own OpenMetadata bot,
  `olf-quality-writer-<stage>`, never as the ingestion bot. Its token is in
  the Secret `openmetadata-quality-writer-<stage>` in the platform
  namespace, which only that stage's reconciler CronJob mounts and
  `olf infra openmetadata quality-sync` reads. The OpenMetadata bootstrap
  reconciles the bots on every deploy, keeps a token that still works, and
  deletes the bot and its Secret when a stage loses governance. Every bot
  holds one role, `OlfQualityWriterRole`, whose policy grants what the
  publisher's routes are authorized against on 1.13.6 and nothing else:
  `ViewBasic`, `ViewTests` and `EditTests` on tables, and `Create` and
  `EditAll` on test definitions. `EditTests` on the table is the only grant
  that lets 1.13.6 create a new test case, and it also covers adding a
  result. The role has no other edit on a table, so a description, tag or
  lineage edit answers 403; the Full e2e asserts the description and
  lineage refusals with the stage's own token. It has no `ViewAll` either,
  which would match a table's sample data and a case's failed rows. A bot
  that OpenMetadata creates also gets `DefaultBotRole`, whose
  `DataConsumerPolicy` allows description and tag edits on every resource,
  so the bootstrap removes that role. OpenMetadata policies cannot match on
  a table's FQN, so the role does not keep stages apart. A stage's bot is a
  separate credential to revoke and a separate author in a result's
  history. The DEV/PROD boundary is still the stage's ops-bucket key and
  its pass's database root, as described above.

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
began passing its entities to its lineage observer with resolved Bronze
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

The Floe profile stays credential-free; this ADR does not create an exception
to that rule.

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

2026-09-29 (#131): every governed stage emits lineage under its own namespace,
OpenMetadata auto-creates lineage entities, and OpenMetadata moves to 1.13.6
for database-qualified dataset resolution. 1.13 has no Iceberg connector, so
the lakehouse service crawls through Trino, which also crawls every governed
stage instead of only the canonical one.

2026-10-01 (#131, #257): table stubs survive the crawl, and lineage pipeline
entities are recorded as not created on OpenMetadata 1.13.6.

2026-10-29 (#545): domains are set on schemas, not tables, and table stubs are
no longer seeded; products attach their Gold tables only.

2026-10-31: tables are registered at activation again, named from the Floe
entities and dbt models, because OpenMetadata 1.13.6 cannot auto-create them
and dropped every Gold edge without them.

2026-10-31: Trino's HTTPS listener is mapped beside its HTTP one. dbt has
reported its datasets under the TLS port since workload authentication;
OpenMetadata 1.13.6 still resolved them by table name, but its main branch drops
a dataset whose namespace is not mapped.

2026-10-28 (#533): Floe emits lineage from one Kubernetes Job per domain, with
the endpoint in the profile the code location renders and the JWT as a Secret
reference on the Job; there is no generated manifest left to keep
credential-free.

2026-11-17 (#797): quality results reach OpenMetadata through its TestCase
APIs from the manifests in the ops bucket, after the run; lineage stays
native, and no proxy is added.

2026-11-19 (#346): quality results are published by a least-privilege
`olf-quality-writer-<stage>` bot per governed stage instead of the
ingestion bot.
