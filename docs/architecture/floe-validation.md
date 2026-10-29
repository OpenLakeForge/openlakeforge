# Floe Validation and Silver Materialization

[Floe](https://github.com/OpenLakeForge/floe) is OpenLakeForge's default
Bronze-to-Silver engine. dlt lands source data in Bronze; Floe reads that data
against domain-owned contracts, separates rejected rows, and writes accepted
rows as Silver Iceberg tables through the configured catalog. dbt-trino then
uses those Silver tables to build Gold marts.

| Layer | Owner | Responsibility |
| --- | --- | --- |
| Bronze | dlt | Land source data without applying the Silver contract. |
| Silver | Floe | Validate, quarantine rejected rows, and materialize Iceberg. |
| Gold | dbt-trino | Apply business transformations to Silver through Trino. |

## The Floe project

All Silver contracts form one Floe 0.8 project under `lakehouse_code/silver/`.
It describes the technical boundary between Bronze inputs and Silver outputs;
it is not a dbt model or a Dagster job.

| File | What it declares |
| --- | --- |
| [`floe.yml`](../../lakehouse_code/silver/floe.yml) | The project: report location and the includes below. |
| [`sources.yml`](../../lakehouse_code/silver/sources.yml) | One named source per Bronze source: CSV options, cast mode, and the `<source>/{{resource}}` path dlt lands it under. |
| `<domain>/contracts/floe/_domain.yml` | The domain, and the defaults every entity inherits: the Iceberg sink in the `<domain>_silver` namespace, the CSV quarantine sink, the `reject` policy, and column normalization. |
| `<domain>/contracts/floe/<entity>.yml` | One entity per file: its `source: { ref, resource }`, primary key, and columns. |

Start with the domains the seed products use:
[sales](../../lakehouse_code/silver/sales/contracts/floe/_domain.yml) and
[supply chain](../../lakehouse_code/silver/supply_chain/contracts/floe/_domain.yml).
An entity such as
[`orders.yml`](../../lakehouse_code/silver/sales/contracts/floe/orders.yml)
names only what is specific to it; its `severity: reject` policy, inherited
from `_domain.yml`, sends invalid rows to the quarantine path instead of mixing
them into Silver. `olf domain new` and `olf product new` write these files and
declare a Bronze source in `sources.yml` the first time an entity reads it.

Storages and the catalog are not in the project. They are stage bindings:
when the code location loads, `libs/floe_profile.py` renders a Floe
EnvironmentProfile from the provider-contract values the stage activation
puts on the pod — buckets, the object-store endpoint, the Polaris REST
catalog and its credential Secret keys, and the OpenLineage endpoint. One
project-code image therefore serves every stage, and nothing stage-bound is
baked into it.

## From contract to Dagster asset

dagster-floe builds the manifest in memory from the project and the rendered
profile (`manifest_from_config`); no manifest is generated, published or
versioned. [The Dagster integration](../../libs/product_dagster.py) loads one
subsettable asset per domain and points each Silver table at the Bronze
resource its entity reads, `<source.ref>/<source.resource>`, through
dagster-floe's `source_key_fn`; left alone, dagster-floe would key every entity
of a named source to one shared `sources/<project>/<source>` asset, and Silver
would not wait for the Bronze resource it reads. Each entity's quarantined rows
are the asset `<domain>/rejected/<table>`. The manifest is named
(`openlakeforge`), so each domain's op keeps its name across contract edits.

| Concern | How it works |
| --- | --- |
| Execution | The profile's runner is `kubernetes_job` with `job_scope: domain`. A run that materializes a domain's asset launches one Kubernetes Job for that domain in the stage namespace, on the run pod's own project-code image and the `dagster` service account. The Job runs plain `floe run`; dagster-floe passes it the run pod's rendered profile as text (`-p env:FLOE_PROFILE_CONTENT`), and deletes the Job if the Dagster run is terminated or times out. |
| Selection | A run that needs a whole domain passes `--domain <domain>`; one that needs only some of its tables passes just those (`--entities sales.orders,sales.accounts`). |
| Concurrency | The profile's `sequential` orchestration runs a Job's entities one after another, and becomes `max_concurrent: 1` on each product job, so one domain Job runs at a time. |
| Credentials | Each Job receives only Secret references (`secretKeyRef`): on a REST catalog the Polaris Floe client id and secret, the storage access keys where the stage uses static keys, and, when governance is on, the OpenMetadata ingestion-bot JWT as `OPENLINEAGE_API_KEY`. No credential value enters the profile, the manifest or the Job spec. |
| Isolation | A domain's Job has its own pod and memory; it does not share the Dagster run pod's memory or its mounted credentials. A run creates one pod per domain it touches. Each Job requests 500m CPU and 1 GiB and is limited to 6 GiB of memory: Floe caps DuckDB (merges, compaction) at 4 GB and needs about 2 GiB above it. The Job and its pod carry the `dagster/run-id` label of the run that started them. |
| Run reports | Floe writes them to `floe/reports/<domain>/run_<id>/` under the stage's artifact prefix, and dagster-floe reads them back with `boto3` to attach each entity's row, accepted and rejected counts and asset-check detail. |

## Supported catalogs

The local and Azure providers write Silver through Polaris, a REST catalog. An
AWS stage writes through AWS Glue: its catalog definition names the database
`<stage prefix>{domain}`, which Floe completes with each domain's Silver
namespace, so `sales` lands in `lakehouse_dev_sales_silver`, the database
`olf catalog sync-namespaces` creates. Floe never creates a Glue database
itself, and the Job authenticates with the pod's own AWS identity. Glue
supports `overwrite` and `append`; merge write modes need a REST catalog. The
AWS writer path is still a rollout gate in the
[technical-debt register](../technical-debt.md).

## Capability comparison at the Bronze-to-Silver boundary

The following compares the capabilities needed at this particular boundary. It
does not rate the projects generally: dbt tests, Soda, and dlt can all be useful
alongside Floe or in a differently shaped pipeline.

| Capability | Floe here | dbt tests | Soda | Raw dlt assembly |
| --- | --- | --- | --- | --- |
| Technical contract | One entity file declares source, schema and keys; its domain supplies the accepted sink, rejected sink, and policy. | Generic and singular tests assert properties of queryable relations; a Silver model and its input relation must also be defined. | Checks declare or calculate data-quality conditions against a datasource; source-to-Silver mapping remains separate. | dlt schemas and schema contracts can govern loading, but the domain contract and Bronze-to-Silver mapping must be assembled. |
| Invalid rows | The declared reject policy writes rejected rows to a per-entity quarantine path while accepted rows continue to their declared sink. | Tests return failures; `store_failures` can persist failed test rows, but routing a Bronze reject lane is separate work. | Checks can identify failing rows and report a failed check; persisting and routing a quarantine dataset is separate work. | A schema contract can reject or discard incompatible data; a retained reject dataset, its format, and its handling must be implemented. |
| Silver materialization | The same contract sends accepted rows to a declared Iceberg table through the catalog. | A dbt model can materialize a relation in its target platform, but it must be supplied with a Bronze query path and separate reject handling. | Soda evaluates data; it does not materialize the Silver table. | dlt can load into a chosen destination; the Iceberg write, catalog use, and transformation policy are choices of the assembly. |
| Orchestration | dagster-floe turns each domain into a subsettable Dagster asset and runs the selected entities in one Kubernetes Job per domain. | The scheduler and dbt adapter must provide the execution environment. | The scheduler and Soda integration must provide the execution environment. | The pipeline and its scheduler must provide the image, isolation, retries, and run settings. |
| Run result | Floe's run events and report give Dagster the entity's status, row counts and rejections. | Test results and artifacts are provided by dbt; orchestration result handling is separate. | Check results are provided by Soda; orchestration result handling is separate. | The assembly defines its own status, reports, and orchestration contract. |

In this stack, dlt remains the Bronze loader and dbt tests remain available for
assertions on modeled relations, especially after Silver. Soda can provide
additional checks or monitoring where it fits. Choosing any of these as the
Silver engine would mean explicitly providing the other rows in the table; this
page makes no performance or general-superiority claim.

## Maintenance and contingency

Floe and OpenLakeForge are separate projects with independent releases. Floe
owns its project format and runtime behavior. OpenLakeForge owns the domain
contracts, the profile rendering, the Dagster integration, and validation of a
Floe upgrade in this platform. OpenLakeForge pins released `floe-python` and
`dagster-floe` versions in the project-code image
([`pyproject.toml`](../../images/project-code/pyproject.toml)) and does not
patch either at run time.

The [technical-debt register](../technical-debt.md) records the integration
history and the known limitations of the current versions.

The operational dependency contingency is intentionally not defined by this
page. [Issue #51](https://github.com/OpenLakeForge/openlakeforge/issues/51), the
operations handbook, tracks the Floe pin, potential fork strategy, contingency
owners and triggers, and an exit path. Until that handbook is published, this
documentation describes the current dependency boundary rather than promising a
support or replacement procedure.
