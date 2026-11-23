# ADR 0007: Governance and lineage

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS and Azure: one governed stage registered (#131), no quality reconciler |

## Context

The platform writes Iceberg tables at two points, Floe for Silver and dbt for
Gold, and both engines can report what they wrote as OpenLineage events. The
goal is a governed catalog with end-to-end lineage and the pipelines' quality
results beside the tables. Two earlier designs, a normalising lineage proxy
and lineage pushed from Dagster, produced graphs that looked right without the
engines having reported them. OpenMetadata 1.13 has no Iceberg connector, so
the catalog reaches the tables through Trino, and 1.13.6 cannot create the
tables and pipelines a lineage event names. Quality results are a record the
platform already keeps, the per-run manifest in the stage's ops bucket
(ADR 0023), so the catalog consumes them rather than re-running checks.

## Decision

**D1. OpenMetadata is the governance target, and it is optional.** It provides
catalog discovery, domain and data-product entities, lineage and quality
results. It is deployed only when a stage enables governance (the Full
profile); Slim omits it, and its e2e assertions are skipped rather than failed.
The artifacts phase seeds domains, data products and the medallion bucket
containers over REST. Enforced by: `module "openmetadata"`'s
`count = local.governance_enabled ? 1 : 0` in
`infra/terraform/environments/local/main.tf`.

**D2. One OpenMetadata per deployment; stage is carried by service identity,
never by a Domain.** Every governed stage gets its own
`<catalog service>.lakehouse_<stage>` database root, a `dagster_<stage>`
pipeline service and, when it also enables analytics, a `superset_<stage>`
dashboard service. The catalog service is `polaris`, or `aws_glue` on AWS.
The bootstrap removes the services of a stage that stopped being governed.
Enforced by: `bootstrap_inputs` in `tools/olf/olf/infra/openmetadata/_platform.py`
and `_stage_services.py`.

**D3. The lakehouse service crawls through Trino, one governed stage's catalog
at a time, and never deletes.** In Trino every stage is one catalog,
`lakehouse_<stage>`, which OpenMetadata lists as one database; the crawl is
filtered to that catalog and runs as the read-only `openmetadata` user. It
never marks schemas or tables deleted, so a crawl before a stage's first run
cannot retire what D5 registered. Enforced by: `ingestion_config` in
`tools/olf/olf/infra/openmetadata/_ingestion.py`.

**D4. Domains are set on schemas, and a data product's assets are its Gold
tables.** `olf infra openmetadata deploy-metadata` adds each stage's domains and
descriptions under that stage's root and sets a domain on the
`<domain>_silver` and `<product>_gold` schemas; the tables found there inherit
it. A product's assets are the tables under its Gold schema, attached once the
crawl has found them; its Silver inputs appear as lineage. Enforced by:
`MetadataReconciler` in `packages/lakehouse-services/openlakeforge_services/governance.py`.

**D5. Each governed stage's lineage lands on its own tables, which are
registered at activation.** Each stage emits under its own `dagster_<stage>`
namespace, and OpenMetadata resolves a dataset by
`<service>.<database>.<schema>.<table>`, so a stage's edges stay on its own
`lakehouse_<stage>` tables. An edge needs both tables registered before the
event arrives, so `deploy-metadata` registers at activation each
`<domain>_silver` and `<product>_gold` schema and every table the stage's
pipelines write: one per Floe entity under Silver, one per dbt model under
Gold. A table is only ever created, never overwritten; the crawl adopts it by
name and fills in its columns. Edges are recorded without a pipeline
reference, and nothing may rely on pipeline entities. Enforced by:
`assert_openmetadata_stage_lineage` in `tools/olf/olf/infra/e2e/_assertions.py`
(each stage's Gold lineage exists and never reaches another stage's database).

**D6. Lineage is emitted by the engine that performed the write, natively and
directly to OpenMetadata, or it is not emitted.** Floe and dbt-trino send
OpenLineage events to OpenMetadata's `/api/v1/openlineage/lineage`, and
OpenMetadata owns entity resolution and persistence. The Floe profile the code
location renders carries the endpoint; each domain's Floe Job receives the
ingestion-bot JWT as `OPENLINEAGE_API_KEY` through a `secretKeyRef`, so the
token never enters the profile or the Job spec (AGENTS.md rule 7).
`openlineage-dbt` ships in the project-code image, and Dagster sets the
product job name around the dbt invocation so events carry stable logical
identities. There is no lineage proxy and no lineage REST push. Enforced by:
`packages/runtime/openlakeforge_runtime/floe_profile.py`.

**D7. The dataset namespace is the catalog identity.** It is `polaris` locally
and on Azure and `aws_glue` on AWS. dbt-trino's namespace is the Trino URI it
connects to, so each governed stage's coordinator address is mapped to the
catalog service. Only the HTTP listener is mapped: the coordinators serve
plain HTTP and nothing else (ADR 0016 D1). Enforced by: `_trino_namespaces` in
`tools/olf/olf/infra/openmetadata/_platform.py` and `enable_openlineage` in
`_catalog_seed.py`.

**D8. Quality results reach OpenMetadata from the quality manifests, through
its TestCase APIs.** A short-lived reconciler reads the normalized per-run
manifest (`run-artifacts/quality/<domain>/<product>/<run>/manifest.json` in
the stage's ops bucket) and publishes it through the `TestDefinition`,
`TestCase` and `TestCaseResult` routes with the shared `OpenMetadataClient`.
OpenMetadata never reruns Floe or dbt. This is the one place the platform
writes to the catalog on an engine's behalf, and it is quality only. Native
dbt `run_results.json` ingestion never creates test results; native dbt
metadata ingestion may enrich the catalog later on that condition. Enforced
by: `QualityPublisher` in
`packages/lakehouse-services/openlakeforge_services/quality_publisher.py`.

**D9. Publication runs after the pipeline run, never inside it.** It runs on
its own schedule and on demand (`olf infra openmetadata quality-sync`), keyed
by stage and Dagster run. A run's outcome never depends on OpenMetadata being
reachable; a failed publication is a retriable state shown beside the
pipeline's quality, not instead of it. Pending, failed and synced publications
and a summary (sync lag, last success and failure) are objects under the
stage's `quality-sync/` in the same ops bucket; there is no new database. Slim
has no reconciler: no OpenMetadata request is made and no credential is
mounted. Enforced by: `infra/terraform/modules/governance/quality-reconciler`
and the Slim case in `infra/terraform/environments/local/tests/wiring.tftest.hcl`.

**D10. A result is published only to an existing table in its own stage.** The
table FQN is `<catalog service>.lakehouse_<stage>.<schema>.<table>`, built from
the stage the manifest was written for, so a DEV manifest can name only DEV
FQNs. An unregistered table or column keeps the result pending, retried with
backoff, and nothing is created implicitly; after `MAX_ATTEMPTS` (10) the
result is `failed` until a replay with `--retry-failed`. Enforced by:
`QualityCatalog` in `quality_publisher.py` and `MAX_ATTEMPTS` in
`packages/core/openlakeforge_core/quality_sync.py`.

**D11. A case is stable, and an execution adds one result.** A case is named
from the engine and the check id and lives under the table or column it
checks, so a second run adds a result to the same case. The result's
timestamp is the engine's observation time in milliseconds; a result already
there with that timestamp, or with the same `olf_identity` within 24 hours,
makes a replay or a restamped manifest add nothing. Enforced by: `has_result`
in `quality_publisher.py`.

**D12. PASS publishes as `Success`, a blocking ERROR as `Failed`, an advisory
WARN as `Aborted`.** `Success` would hide a warning, and `Failed` opens an
incident nobody chose for a check the pipeline survived. Every result carries
the original outcome, engine severity and pipeline outcome in its details, so
OpenMetadata's coarser status never replaces them. An operator who wants
incidents for WARN sets the WARN status to `Failed`. `not_run` and `unknown`
are never published. Enforced by: `om_status` in
`packages/core/openlakeforge_core/quality_sync.py`.

**D13. Each governed stage publishes as its own least-privilege bot,
`olf-quality-writer-<stage>`, never as the ingestion bot.** Its token is in
the Secret `openmetadata-quality-writer-<stage>` in the platform namespace,
mounted only by that stage's reconciler CronJob and read by `olf infra
openmetadata quality-sync`. Every bot holds one role, `OlfQualityWriterRole`,
granting what the publisher's routes need and nothing else, and the bootstrap
removes the `DefaultBotRole` OpenMetadata adds; the grants are in
[identity-service-permissions](../architecture/identity/identity-service-permissions.md#the-quality-writer-bots).
The bootstrap reconciles the bots on every deploy, keeps a token that still
works, and deletes a stage's bot and Secret when it loses governance. With
governance off for every stage, `olf infra deploy` deletes every stage's
quality-writer Secret and the ingestion-bot Secret and its copies, and the
bots only while OpenMetadata still answers; a bot OpenMetadata comes back
with has no Secret, so the next governed bootstrap rotates or deletes it.
Enforced by: `tools/olf/olf/infra/openmetadata/_quality_writer.py` and the
Full e2e in `tools/olf/olf/infra/e2e/_quality_writer.py` (description and
lineage edits refused with the stage's own token).

**D14. The Dagster connector stays deferred.** OpenMetadata's Dagster pipeline
service and its ingestion pipeline are registered but never triggered: the
connector queries the deprecated `PipelineRuns` GraphQL type while Dagster 1.x
exposes `Runs`. The service definition stays in place for when the connector
is corrected. Enforced by: `_stage_services.py` (pipeline created paused,
deployed without a run).

**D15. The Trino metadata crawl runs as an OLF-owned Job, not as an
OpenMetadata ingestion pipeline.** `olf infra openmetadata deploy-metadata`
runs the crawl of D3 once per artifacts deploy as the Job
`openmetadata-trino-ingestion` beside the stage's coordinator, and applies
the hourly refresh as a CronJob per governed stage
(`openmetadata-trino-ingestion-<catalog>`) built from the same pod spec. Both
run the pinned `ingestion-base` image's `metadata ingest`, with the
ingestion-bot JWT from a `secretKeyRef` and the `openmetadata` principal's
Polaris token minted in the pod just before the crawl, so no token is in the
Job spec or the service OpenMetadata stores. OpenMetadata registers no
ingestion pipeline for the lakehouse service, and the Job and its credential
are revoked from a stage that drops governance. Enforced by:
`run_trino_ingestion` in `tools/olf/olf/infra/openmetadata/_ingestion.py` and
`tools/olf/tests/infra/test_openmetadata_ingestion.py`.

## Consequences

- The Bronze→Silver→Gold graph resolves in OpenMetadata only when governance
  is enabled, and the Floe profile stays credential-free.
- An edge whose table was not registered is lost: OpenMetadata 1.13.6 does not
  auto-create the entities an event names (the entity is inserted without
  `updatedAt`), which is why D5 registers tables.
- OpenMetadata policies cannot match a table's FQN, so `OlfQualityWriterRole`
  does not keep stages apart. A stage's bot is a separate credential to revoke
  and a separate author in a result's history; the DEV/PROD boundary is the
  stage's ops-bucket key and its pass's database root (D10).
- Superset dashboards appear in OpenMetadata only after its dashboard
  ingestion pipeline crawls the Superset API; report import and that crawl are
  separate, and Superset is the source of dashboard runtime state.
- The AWS and Azure roots still register one governed stage until their
  adapters follow (#131), and deploy no quality reconciler, so there `olf infra
  openmetadata quality-sync` is the only way to publish.
- The OpenMetadata 1.13.6 limits the publisher works around, and the routes it
  calls, are in [health signals](../reference/health-signals.md#openmetadata-1136-limits-and-routes).

## Boundaries

| Not decided here | Where |
| --- | --- |
| The quality manifest's content and its ops-bucket location | ADR 0023 |
| How the Trino crawl authenticates (its Polaris token) | ADR 0016 D1, ADR 0019 section 9 |
| The quality reconciler's network flows (F52, F53) | ADR 0017, `release/network-flows.yaml` |
| Publication health and the re-sync runbook | [health signals](../reference/health-signals.md#quality-publication-to-openmetadata), [service health](../guides/operations/service-health.md#quality-syncpublication) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| A normalising OpenLineage proxy rewriting Floe and dbt events | It ran and produced incomplete or wrongly attached lineage: the faults were upstream (non-UUID Floe run IDs, dbt-duckdb's local namespace), and the proxy turned them into a silent correctness problem |
| Lineage edges built in Dagster and pushed over REST | Lineage that looks right without the engines reporting what they did |
| Native dbt `run_results.json` ingestion for quality results | A second copy of the manifest that covers neither Floe nor WARN versus ERROR, with a reported regression in 1.13.0 (open-metadata/OpenMetadata#29824) |
| WARN as `Success` or `Failed` | `Success` hides it; `Failed` opens an incident |
| Publishing quality results as the ingestion bot | A per-stage bot is a separate credential to revoke and a separate author in a result's history (D13) |
| OpenMetadata's own Trino ingestion pipeline (the Kubernetes pipeline client) | Its pods take only plain-string environment values, so the crawl's Polaris token could not come from a Secret at run time |
| A stage carried by an OpenMetadata Domain | A Domain holds the business domain (D4); stage is service identity (D2) |

## History

- Merged from the former ADRs 0006 (OpenMetadata with an OpenLineage proxy),
  0009 (lineage deferred; the proxy and a Dagster REST push rejected) and 0023
  (native emission restored) (D1, D6).
- 2026-09-23 (#131): one OpenMetadata with stage-qualified service roots
  (`lakehouse_<stage>`, `dagster_<stage>`, `superset_<stage>`) replaced the
  single unqualified services (D2).
- 2026-09-29 (#131): every governed stage emits under its own namespace on
  OpenMetadata 1.13.6, and the lakehouse service crawls through Trino every
  governed stage (D3, D5).
- 2026-10-01 (#131, #257): registered tables survive the crawl, and lineage
  pipeline entities are not relied on (D3, D5).
- 2026-10-28 (#533): Floe emits from one Kubernetes Job per domain, the JWT a
  Secret reference on the Job (D6).
- 2026-10-29 (#545): domains are set on schemas, not tables; products attach
  their Gold tables only (D4).
- 2026-10-31: tables are registered at activation again, from the Floe
  entities and dbt models (D5).
- 2026-10-31: Trino's HTTPS listener is mapped beside its HTTP one (D7).
- 2026-11-17 (#797): quality results reach OpenMetadata through its TestCase
  APIs from the ops-bucket manifests, after the run (D8 to D12).
- 2026-11-19 (#346): a result that never finds its table ends `failed` after
  10 attempts (D10); quality results are published by a per-stage
  `olf-quality-writer-<stage>` bot instead of the ingestion bot (D13).
- 2026-11-23: rewritten in the standard form. D7 now maps the HTTP listener
  only, since ADR 0016 D1 removed the HTTPS listener (#983). D15 added: the
  OLF-owned Trino crawl Job, decided under ADR 0016 (#26, #471) and recorded
  only in its former History until now.
