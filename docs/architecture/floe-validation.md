# Floe Validation and Silver Materialization

[Floe](https://github.com/malon64/floe) is OpenLakeForge's default
Bronze-to-Silver engine. dlt lands source data in Bronze; Floe reads that data
against a domain-owned contract, separates rejected rows, and writes accepted
rows as Silver Iceberg tables through the configured catalog. dbt-trino then
uses those Silver tables to build Gold marts.

| Layer | Owner | Responsibility |
| --- | --- | --- |
| Bronze | dlt | Land source data without applying the Silver contract. |
| Silver | Floe | Validate, quarantine rejected rows, and materialize Iceberg. |
| Gold | dbt-trino | Apply business transformations to Silver through Trino. |

## The domain contract

Each domain owns one Floe YAML contract under
`lakehouse_code/silver/<domain>/contracts/floe/<domain>.yml`. It describes the technical boundary between
Bronze inputs and Silver outputs; it is not a dbt model or a Dagster job.

| Contract section | What it declares |
| --- | --- |
| `storages` and `report` | Named Bronze, Silver, and ops storage plus the report location. |
| `entities[].source` | Input format, object-storage path, file options, and cast mode. |
| `entities[].schema` | Column names and types, nullability, normalization, and keys. |
| `entities[].sink.accepted` | The Iceberg table and storage location for valid rows. |
| `entities[].sink.rejected` and `policy` | The quarantine location and the treatment of invalid rows. |

Start with the contracts that the seed products actually use:

| Domain | Contract | Generated runner manifest |
| --- | --- | --- |
| Sales | [sales.yml](../../lakehouse_code/silver/sales/contracts/floe/sales.yml) | [sales.manifest.json](../../lakehouse_code/silver/sales/contracts/floe/manifests/sales.manifest.json) |
| Supply chain | [supply_chain.yml](../../lakehouse_code/silver/supply_chain/contracts/floe/supply_chain.yml) | [supply_chain.manifest.json](../../lakehouse_code/silver/supply_chain/contracts/floe/manifests/supply_chain.manifest.json) |

For example, `sales.yml` defines CSV Bronze sources with strict casts,
column types, nullability, primary keys, and snake-case normalization. Each
entity sends accepted rows to a named Iceberg table in the domain's Silver
namespace. Its `severity: reject` policy sends invalid rows to the declared
CSV quarantine path instead of mixing them into Silver. The generated manifest
records `success_or_rejected` as exit code `0`, so a run with quarantined rows
is distinct from a technical runner failure.

## From contract to Kubernetes runner

Floe generates the manifest from the contract and the environment-specific
Floe profile during the dynamic artifact deployment. OpenLakeForge validates the
contract first, then asks Floe to generate a deterministic manifest with
resolved URIs. The manifest is a generated artifact: OpenLakeForge does not
edit it after generation.

| Manifest capability | How OpenLakeForge uses it |
| --- | --- |
| Entity list and asset keys | Dagster creates the domain's Floe assets from the manifest. |
| Runner definition | `dagster-floe` builds the configured runner rather than embedding Floe in the project-code image. |
| Image, command, arguments, and result contract | The runner Job invokes `floe run --manifest` and reports completion to Dagster. |
| Kubernetes timeout, TTL, secrets, and environment | The runner Job receives these operational settings from the manifest, including Secret references rather than credentials in the artifact. |
| Sequential execution | A product run launches its selected Floe Jobs in manifest order. |

[The Dagster integration](../../libs/product_dagster.py) resolves the manifest
that Dagster uses, builds job configuration from it, and builds the Floe runner
from the environment. In the Kubernetes path, Dagster passes the published
manifest URI to the separate manifest-declared runner image. The job model is
therefore one ephemeral Floe Job per entity, sequentially, with its own timeout
and TTL. See [Chart 3](diagrams/chart3-ephemeral-job-lifecycle.md) for the
runner lifecycle and durable reports after the Jobs are collected.

## Capability comparison at the Bronze-to-Silver boundary

The following compares the capabilities needed at this particular boundary. It
does not rate the projects generally: dbt tests, Soda, and dlt can all be useful
alongside Floe or in a differently shaped pipeline.

| Capability | Floe here | dbt tests | Soda | Raw dlt assembly |
| --- | --- | --- | --- | --- |
| Technical contract | One entity contract declares source shape, schema, keys, accepted sink, rejected sink, and policy. | Generic and singular tests assert properties of queryable relations; a Silver model and its input relation must also be defined. | Checks declare or calculate data-quality conditions against a datasource; source-to-Silver mapping remains separate. | dlt schemas and schema contracts can govern loading, but the domain contract and Bronze-to-Silver mapping must be assembled. |
| Invalid rows | The declared reject policy writes rejected rows to a per-entity quarantine path while accepted rows continue to their declared sink. | Tests return failures; `store_failures` can persist failed test rows, but routing a Bronze reject lane is separate work. | Checks can identify failing rows and report a failed check; persisting and routing a quarantine dataset is separate work. | A schema contract can reject or discard incompatible data; a retained reject dataset, its format, and its handling must be implemented. |
| Silver materialization | The same contract sends accepted rows to a declared Iceberg table through the catalog. | A dbt model can materialize a relation in its target platform, but it must be supplied with a Bronze query path and separate reject handling. | Soda evaluates data; it does not materialize the Silver table. | dlt can load into a chosen destination; the Iceberg write, catalog use, and transformation policy are choices of the assembly. |
| Kubernetes runner | Generated manifest declares the runner image and per-entity Job settings, then `dagster-floe` consumes them. | The scheduler and dbt adapter must provide the execution environment; tests do not define a per-entity runner. | The scheduler and Soda integration must provide the execution environment; checks do not define the runner. | The pipeline and its scheduler must provide the image, isolation, retries, and Job settings. |
| Run result | Manifest defines the completion event, summary URI, and exit-code meaning that Dagster receives. | Test results and artifacts are provided by dbt; orchestration result handling is separate. | Check results are provided by Soda; orchestration result handling is separate. | The assembly defines its own status, reports, and orchestration contract. |

In this stack, dlt remains the Bronze loader and dbt tests remain available for
assertions on modeled relations, especially after Silver. Soda can provide
additional checks or monitoring where it fits. Choosing any of these as the
Silver engine would mean explicitly providing the other rows in the table; this
page makes no performance or general-superiority claim.

## Maintenance and contingency

Floe and OpenLakeForge are separate projects with independent releases. Floe
owns its contract and manifest formats and its runner behavior. OpenLakeForge
owns the domain contracts, environment profiles, Dagster integration, and
validation of a Floe upgrade in this platform. OpenLakeForge consumes a
released Floe runner image; it does not vendor Floe or rewrite Floe-generated
manifests.

The [technical-debt register](../technical-debt.md) records the integration
history. Several Floe defects are already resolved upstream: profile-variable
and remote-path rendering (#424), manifest replay of S3 storage definitions
(#425), EKS Pod Identity credentials (#426), and manifest replay lineage
context (#455, consumed in Floe 0.6.11). The register remains the source for
known limitations and the version-specific follow-up work.

The operational dependency contingency is intentionally not defined by this
page. [Issue #51](https://github.com/OpenLakeForge/openlakeforge/issues/51), the
operations handbook, tracks the Floe pin, potential fork strategy, contingency
owners and triggers, and an exit path. Until that handbook is published, this
documentation describes the current dependency boundary rather than promising a
support or replacement procedure.
