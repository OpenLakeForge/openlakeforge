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
  pending, retried with backoff, and nothing is created implicitly. After
  `MAX_ATTEMPTS` (10) the result is `failed` until a replay with
  `--retry-failed` ([limits](#openmetadata-1136-compatibility-limits)). A DEV
  manifest can name only DEV FQNs.
- **Stable cases, one result per execution.** A case is named from the engine
  and the check id and lives under the table (or column) it checks, so a
  second run adds a result to the same case. The result's timestamp is the
  engine's observation time in milliseconds, which also makes a replay find the
  result already there; a result whose `olf_identity` value matches within 24
  hours also counts, so a manifest restamped for the same execution adds
  nothing.
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
  With governance off for every stage the bootstrap does not run;
  `olf infra deploy` then deletes every stage's quality-writer Secret and
  the ingestion-bot Secret and its copies, and deletes the bots only while
  OpenMetadata still answers. A bot OpenMetadata comes back with has no
  Secret, so the next governed bootstrap rotates its token or deletes it.

#### OpenMetadata 1.13.6 compatibility limits

What the publisher works around on 1.13.6, as the code does it today. The
publisher is `QualityCatalog` and `QualityPublisher` in
`packages/lakehouse-services/openlakeforge_services/quality_publisher.py`; the
status mapping and names are in `packages/core/openlakeforge_core/quality_sync.py`.
`tools/olf/tests/common/test_quality_publisher.py` fakes these routes, and
`tools/olf/olf/infra/e2e/_quality_publication.py` checks them against a real
OpenMetadata.

| Limit on 1.13.6 | What the publisher does |
| --- | --- |
| A result status is only `Success`, `Failed` or `Aborted` (`OmStatus`; `Queued` is never sent) | PASS is `Success`, ERROR is `Failed`, WARN is `Aborted` by default (`om_status`, `warn_status`); `not_run` and `unknown` publish nothing. The OLF outcome, engine severity and pipeline outcome travel in `testResultValue` (`result_body`) |
| `Failed` opens an incident (`TestCaseResultRepository.setTestCaseResultIncidentId`) | That is why WARN is not `Failed`; an operator who wants incidents for WARN passes `--warn-status Failed` to `olf infra openmetadata quality-sync`, or sets the reconciler module's `warn_status` |
| `FullyQualifiedName.add` quotes a segment that contains a dot, and case names (`olf.<engine>.<check id>`, `case_name`) do | The case FQN is `<table>."<name>"`, or `<table>.<column>."<name>"` for a column check. `upsert_case` takes the FQN from OpenMetadata's response and addresses the case by it; the e2e builds the same form with `case_fqn` |
| Tables are never auto-created | `table_columns` looks the table up first; an unknown table or column leaves the result pending with `table not registered in OpenMetadata` (or `column ...`), and nothing is created. Each attempt backs off; after `MAX_ATTEMPTS` (10) the result is `failed` until a replay with `--retry-failed` |
| Native dbt `run_results.json` ingestion is not used (open-metadata/OpenMetadata#29824) | Results come only from the quality manifest, for the reasons in "No native dbt ingestion for results" above |
| A result's `timestamp` is epoch milliseconds, and a repeated timestamp on a case is a duplicate | The timestamp is the engine's observation time (`epoch_millis`). Before adding, `has_result` reads the case's results within 24 hours of it and skips the add when the timestamp or the `olf_identity` value matches, so a replay, or a manifest restamped for the same execution, adds nothing |

The routes the publisher calls, and no others:

| Method | Route | Used for |
| --- | --- | --- |
| `GET` | `/api/v1/tables/name/{table FQN}?fields=columns` | The table exists in this stage's root, and its columns |
| `PUT` | `/api/v1/dataQuality/testDefinitions` | Create or update `olf_<engine>_table` / `olf_<engine>_column` |
| `PUT` | `/api/v1/dataQuality/testCases` | Create or update the case, by entity link |
| `GET` | `/api/v1/dataQuality/testCases/testCaseResults/{case FQN}?startTs=&endTs=` | Whether this execution is already there |
| `POST` | `/api/v1/dataQuality/testCases/testCaseResults/{case FQN}` | Add the result |

FQNs in a path are percent-encoded whole (`quote(fqn, safe="")`), so the quotes
and slashes of a name are escaped. When results wait or give up, the re-sync
runbook is
[service health § quality-sync.publication](../guides/operations/service-health.md#quality-syncpublication);
the state and summary objects are described in
[health signals](../reference/health-signals.md#quality-publication-to-openmetadata).

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

2026-11-19 (#346): the OpenMetadata 1.13.6 limits the quality publisher works
around, and the routes it calls, are listed; no decision changes. The
existing-tables bullet is corrected to match the code: a result that never
finds its table ends `failed` after 10 attempts.

2026-11-19 (#346): quality results are published by a least-privilege
`olf-quality-writer-<stage>` bot per governed stage instead of the
ingestion bot.
