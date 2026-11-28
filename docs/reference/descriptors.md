# Descriptor reference

The fields and rules of the descriptors that declare a project's data:
`lakehouse_code/governance.yml`, one per project, and
`lakehouse_code/bronze/<source>/source.yaml`, one per source. The optional
`lakehouse_code/alerts.yml` declares its alert policy, and
`lakehouse_code/glossaries/<name>.yml` holds a business glossary. It also lists
the names OpenLakeForge derives from them.

Both are validated against JSON Schemas,
[`governance.schema.json`](../schema/governance.schema.json) and
[`source.schema.json`](../schema/source.schema.json), and by the canonical
inventory, with `olf check contracts`. Unknown fields are rejected at every
level, and every identifier matches `^[a-z][a-z0-9_]*$`.

Descriptors are provider-neutral: they name logical things only. They never
contain Polaris, Glue, S3 or SeaweedFS names, endpoints, Kubernetes resources,
account ids or credentials; OpenLakeForge derives those from the active
provider.

## `source.yaml`

```yaml
apiVersion: openlakeforge.io/v1beta1
kind: Source
name: marketing_platform
displayName: Marketing Platform
description: Marketing campaign performance source system.
status: active
resources:
  - name: campaigns
    description: Raw CSV marketing campaign performance data.
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | `openlakeforge.io/v1beta1` |
| `kind` | string | yes | `Source` |
| `name` | identifier | yes | Must equal the directory name and be unique in the lakehouse. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | yes | May be empty. |
| `status` | string, non-empty | yes | Free text; typically `planned`, `active` or `deprecated`. |
| `resources` | array, at least one | yes | The source's table-like inputs. |
| `resources[].name` | identifier | yes | Unique within the source. |
| `resources[].description` | string | no | Human-readable description. |
| `resources[].load.mode` | `full` or `incremental` | no | `full` (the default) replaces the resource's Bronze files on every run. `incremental` declares that only the rows past the cursor should load. No loader reads it yet; see [Load modes](#load-modes). |
| `resources[].load.cursor` | column name | with `incremental` | The column whose last loaded value the next run starts after. Not allowed with `full`. |
| `resources[].load.primary_key` | array of column names | with `incremental` | The row's identity, which Silver merges a changed row onto. |
| `runtime.config` | array of key names | no | Names of the non-secret settings the loader reads, such as an API base URL. |
| `runtime.secrets` | array of key names | no | Names of the credentials the loader reads, such as an API token. A name appears in at most one of the two lists. |

`openlakeforge.io/v1alpha3` Source documents are the same document with the
older version line and do not load as `v1beta1`. `olf project migrate` changes
that line, in every `source.yaml` and in `governance.yml` together (see
[`governance.yml`](#governanceyml)), and refuses, leaving all files as they
were, if a document does not validate afterwards. No field was removed or
renamed between the two versions.

A resource has no schema here: its columns belong to the Floe contract of each
domain that reads it.

`runtime` lists key names, never values: each stage binds its own. For a
source `crm_api` with `runtime: {config: [base_url], secrets: [api_token]}`:

| Key | Bound by the stage in | Reaches the loader as |
| --- | --- | --- |
| `base_url` | ConfigMap `olf-source-crm-api` | `SOURCES__CRM_API__BASE_URL` |
| `api_token` | Secret `olf-source-crm-api-credentials` | `SOURCES__CRM_API__API_TOKEN` |

Create those bindings through `olf`, using the same profile and stage you
will deploy to. Export `CRM_API_TOKEN` in your shell first:

```bash
olf infra source bind crm_api --stage dev -f openlakeforge.yaml \
  --set base_url=https://api.example.com \
  --secret-from-env api_token=CRM_API_TOKEN
olf infra source bindings --stage dev -f openlakeforge.yaml
```

`bind` checks key names against the descriptor. Pass secret values through
`--secret-from-env KEY=VAR`, `--secret-from-file KEY=PATH` (one trailing newline
is stripped), or the terminal's hidden prompt. Output contains key names only.
`--partial` retains existing declared keys; `--dry-run` reads the current
bindings and prints the plan without writing or prompting. `bindings` warns
about extra keys and exits 1 for missing or unreadable objects or keys. Use
`olf infra source unbind crm_api --stage dev -f openlakeforge.yaml` to remove
both objects. Repeat the binding for each stage with that stage's values.

These are the names dlt itself resolves for a `@dlt.source` in
`bronze/crm_api/dlt/crm_api.py`, so the loader reads them with
`dlt.config.value` and `dlt.secrets.value`, and fails if one is missing. In
the Kubernetes names, `_` becomes `-`. Because of both conversions, a source
with a `runtime` block, and each of its keys, must be underscore-separated
words: `crm_api` and `base_url`, not `crm_`, `_url` or `a__b`.

Where they apply:

- Each product job carries the bindings of the sources it loads. They reach
  that job's run pod only, never the code-location server.
- Steps of one run share its pod, so every loader in a job sees every binding
  the job carries.
- A Bronze asset materialized outside its product job gets no binding and
  fails with dlt's missing-config error.
- `olf infra deploy` refuses to activate a revision whose sources the stage has not
  bound. It reads Secret key names only, never values.
- On `local`, a run pod reaches a host outside the cluster only on a port the
  stage declares in its Deployment Profile, `spec.stages.<stage>.source_egress`
  ([reference](deployment-profile.md#specstages)).

A loader that is a real dlt source runs it through
`openlakeforge_runtime.bronze_dlt.bronze_pipeline(<source>)`:

- Files land at `<stage Bronze bucket>/<source>/<resource>/`, the path Floe
  reads Bronze from.
- dlt's incremental state lives in that same bucket, under the pipeline
  `<stage>__<source>`. Each run starts from an empty working directory and
  restores its cursor from its own stage, so a DEV run cannot move PROD's.
- `olf check project-code` proves this with a mocked API source run as DEV
  and as PROD.

`openlakeforge_runtime.bronze_incremental` provides the read side of an incremental connector
for an API or a database; [Load an external source
incrementally](../guides/incremental-sources.md) walks through one.

### Load modes

Each layer declares its load mode where its owner keeps it:

| Layer | Declared in | Incremental | Full |
| --- | --- | --- | --- |
| Bronze | `resources[].load` in `source.yaml` | dlt appends the rows past `cursor` as new files | dlt replaces the files |
| Silver | the entity's Floe contract | `incremental_mode: file` with `merge_scd1`, `merge_scd2` or `append` | `overwrite` |
| Gold | the dbt model's `materialized` config | `incremental` with a `unique_key` and the `merge` strategy | `table` |

`olf check contracts` rejects a Silver entity whose mode does not fit its
Bronze resource. An entity reading an incremental resource needs
`incremental_mode: file` and `merge_scd1`, `merge_scd2` or `append`. An
entity reading a full resource cannot use `append`, which would add every row
again on each run.

A loader with an incremental resource runs its dlt source through
`openlakeforge_runtime.bronze_dlt.load_dlt_source`. The connector declares the cursor with
`dlt.sources.incremental` on the column `load.cursor` names, and the load
fails if the two differ. A run with nothing new past the cursor writes no
Bronze file, so Floe has no new file to read either. The CSV loader has no
incremental mode: it replaces its files on every run.

Floe merges an entity's pending files in one `MERGE`, in file order, and keeps
the latest file's row when a key repeats across them. dlt names Bronze files
after the load id, so a Silver run catching up on several batches merges each
row's newest version.

### Full refresh

A run is incremental unless it carries the run tag
`openlakeforge/full_refresh`. Run
`olf infra product run <product> --full-refresh <scope>`, or set the tags
yourself in the Dagster launchpad:

| Scope | Bronze | Silver | Gold |
| --- | --- | --- | --- |
| `bronze` | Incremental, as on any run | Rebuilt from every Bronze file (`floe run --full-refresh`) | `dbt build --full-refresh` |
| `gold` | Incremental | Incremental | `dbt build --full-refresh` |

A full refresh re-ingests what Bronze already holds; it never extracts from a
source again, so cursors and Bronze files are untouched. Incremental Bronze
keeps every batch and Floe merges a repeated key from the latest file, so
Silver rebuilt from all of them holds each row's newest version.

A `bronze` refresh needs a second tag, `floe/full_refresh=true`, which makes
dagster-floe run the job's Floe entities with `--full-refresh`.
`olf infra product run` sets both. A run that carries only the first fails at
its first step, before Silver or Gold change.

Known limits:

- A row deleted at the source never reaches Silver: incremental extraction
  sees only rows that still exist, and a full refresh rebuilds from the same
  Bronze. Removing it takes a source-specific step, such as reloading that
  resource's Bronze.

- Floe cannot rebuild a `merge_scd2` table's version history from its
  inputs, so a `bronze` refresh fails at a `merge_scd2` entity.
- A run killed after dlt wrote some files of a batch but before it saved
  the cursor leaves those files in Bronze. The rerun extracts the same rows
  again into a newer batch. A merge keeps the newer rows and reports the
  killed batch's rows as rejected duplicates; Bronze keeps both.

Incremental state is per stage. dlt keeps cursors under the pipeline
`<stage>__<source>` in the stage's Bronze bucket. Floe keeps its file state
under the source path in that same bucket, and dbt's state is the stage's
Gold table itself.

## `governance.yml`

```yaml
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: marketing
    displayName: Marketing
    description: Marketing analytics data products.
    domainType: Consumer-aligned
    owner: {type: team, ref: marketing_data}
    classification: Internal
    products:
      - id: campaign_performance
        displayName: Campaign Performance
        description: Campaign spend and conversion performance.
        type: REPORTS
        tier: Tier2
        lifecycle: PRODUCTION
        termsOfUse:
          allowed: [internal_reporting]
          prohibited: [external_redistribution]
```

The file names domains and the data products nested under them, and nothing
else. Tables belong to a
domain or a product by the schema they live in, so no table is listed:

| Schema | Owner |
| --- | --- |
| `<domain>_silver` | the domain |
| `<product>_gold` | the product, and through it the product's domain |

Sources are the `bronze/*/source.yaml` files on disk. Dashboards are the
`dashboards/superset/*/` directories that hold a `metadata.yaml`; a dashboard
serves the products whose `<product>_gold` schema its dataset files read.

### Top level

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | `openlakeforge.io/v1beta2` |
| `kind` | string | yes | `Governance` |
| `domains` | array, at least one | yes | The business domains. |

### `domains[]`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | identifier | yes | Unique across the lakehouse; also the directory `silver/<name>/`. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | no | |
| `domainType` | enum | no | `Source-aligned`, `Consumer-aligned` or `Aggregate`. |
| `owner` | reference | no | The accountable team or user. Inherited by the domain's products. |
| `experts` | array of references, non-empty | no | Subject-matter experts. Inherited by the products. |
| `classification` | enum | no | `Public`, `Internal`, `Confidential` or `Restricted`. Inherited by the products. |
| `glossaryTerms` | array of term references, non-empty | no | Terms from a [glossary](#glossary) that define the domain. Not inherited by its products. |
| `products` | array, may be empty or omitted | no | The data products of this domain. A project needs at least one product across all its domains to be checked or deployed. |

### `domains[].products[]`

A product belongs to the domain it is nested under, so it carries no `domain`
field. Moving a product to another domain changes which Silver tables it may
read, so it is a deliberate edit of the directory layout as well as the file.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Unique across the whole lakehouse, not only within its domain; also the directory `gold/<id>/`. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string | no | |
| `owner` | reference | no | Overrides the domain's `owner`. |
| `experts` | array of references, non-empty | no | Overrides the domain's `experts`. |
| `reviewers` | array of references, non-empty | no | Who reviews changes to the product. Not inherited. |
| `classification` | enum | no | Overrides the domain's `classification`. |
| `type` | enum | no | What the product is. One of `RAW_DATA`, `DERIVED_DATA`, `DATASET`, `REPORTS`, `ANALYTIC_VIEW`, `VISUALISATION_3D`, `ALGORITHM`, `DECISION_SUPPORT`, `AUTOMATED_DECISION_MAKING`, `DATA_ENHANCED_PRODUCT`, `DATA_DRIVEN_SERVICE`, `DATA_ENABLED_PERFORMANCE`, `BI_DIRECTIONAL`. |
| `tier` | enum | no | `Tier1` (most critical) to `Tier5`. |
| `lifecycle` | enum | no | `IDEATION`, `DESIGN`, `DEVELOPMENT`, `TESTING`, `PRODUCTION`, `DEPRECATED` or `RETIRED`. |
| `termsOfUse` | object | no | `allowed` and/or `prohibited`: non-empty arrays of strings. |
| `profiling` | object | no | `{enabled: true\|false}`, default `false`. Opts the product's Gold facts and dimensions in to OpenMetadata profiling; see [Profiling policy](#profiling-policy). Not inherited. |
| `glossaryTerms` | array of term references, non-empty | no | Terms from a [glossary](#glossary) that define the product, as `<glossary>.<Term>`. Not inherited. |

A reference is `{type: user, ref: <name>}` or `{type: team, ref: <name>}`. It
names an existing user or team; OpenLakeForge never creates one.

The enums are the OpenMetadata 1.13.6 ones (`Domain.domainType`,
`DataProduct.dataProductType` and `lifecycleStage`, the `Tier` tags), so a value
that validates here is a value OpenMetadata accepts. Every field above is
optional.

`owner`, `experts` and `classification` are inherited: a product that does not
set one uses its domain's. `tier`, `lifecycle`, `type`, `reviewers` and
`termsOfUse` belong to the product alone. The loaded inventory carries the
resolved values, so every consumer reads the same answer.

`classification` is metadata. It does not grant or revoke access in Trino or
Polaris, in OpenMetadata or anywhere else; enforcement is tracked in
[#924](https://github.com/OpenLakeForge/openlakeforge/issues/924).

#### Profiling policy

`profiling.enabled: true` allows OpenMetadata to compute statistics for the
product's Gold tables. The product's resolved `classification` decides what
may be stored:

| Classification | Stored per table and column |
| --- | --- |
| `Public`, `Internal` | `rowCount`; `valuesCount`, `nullCount`, `nullProportion`, `distinctCount`, `min`, `max`, `mean` |
| `Confidential` | the same without `min`, `max` and `mean`, which are real values |
| `Restricted` or none | nothing; `olf check contracts` fails if `enabled: true` |

Only tables with the `GoldRole.Fact` or `GoldRole.Dimension` role in
`<product>_gold` are eligible; marts, Silver and Bronze never are. `min`, `max`
and `mean` of a text column are string lengths, not values.

What runs:

- `olf infra openmetadata deploy-metadata` profiles each governed stage once,
  after the crawl and the seeding, with the Job `openmetadata-trino-profiler`
  beside the stage's coordinator. A CronJob
  (`openmetadata-trino-profiler-<catalog>`, daily at 04:30 UTC) refreshes it.
  A stage with no eligible table gets neither, and a CronJob left by an earlier
  policy is removed.
- Each stage profiles only its own `lakehouse_<stage>` tables, with its own Job.
  The Job uses the crawl's image, resources and `openmetadata` principal, one
  thread and a 10-minute limit per product.
- A stage's Gold tables exist only after its pipelines ran. A first deploy
  therefore finds nothing to profile; the next `deploy-metadata`, or the daily
  CronJob, profiles them.

What is read and stored:

- Trino reads the eligible tables' rows to compute the metrics in the table
  above. Nothing else is read: no marts, no other product, and no query other
  than the profiler's. OpenMetadata stores the statistics as time series
  (`/api/v1/tables/<table fqn>/tableProfile` and
  `/api/v1/tables/<column fqn>/columnProfile`), not as fields of the table.
- No sample rows are requested or stored, there are no custom SQL metrics, and
  no row sampling is configured, so every metric covers the whole table.

A failed profile is a warning, not a failed deploy: the Job's status stays
visible with `kubectl get jobs -n <stage namespace>` for an hour, and the
last statistics stay in place. Statistics older than 30 hours (the daily period
plus 6 hours) are stale, and readers such as
`openlakeforge_services.profile_statistics.read_table_statistics` mark them so.

Cost: each profile runs a handful of aggregate queries per table and column on
the stage's Trino. Cost grows with table size, which is why only opted-in
facts and dimensions are profiled and why `threadCount` is 1. Keep
`profiling.enabled` off for a product whose tables are large until its profile
time is known.

To turn it off, remove `profiling` or set `enabled: false` and run
`olf infra openmetadata deploy-metadata`: the CronJob is deleted and the next
deploy runs no profile. Statistics already stored are not deleted; readers mark
them stale after 30 hours.

`status` was removed from domains and products. A `v1beta2` document that
sets it fails with a hint to use `lifecycle`; domains have no lifecycle, so
drop it there. `olf project migrate` refuses a `v1beta1` file that sets
`status` for the same reason, leaving it unchanged, and the migrated file
needs `lifecycle` written by hand.

#### What appears in OpenMetadata

`olf infra deploy` publishes the resolved fields to OpenMetadata 1.13.6 when
the stage runs it. A field you do not declare is not written, so a value a
person set in OpenMetadata stays.

| Field | OpenMetadata | Where |
| --- | --- | --- |
| `owner` | `owners` | Domain and data product |
| `experts` | `experts` | Domain and data product |
| `reviewers` | `reviewers` | Data product |
| `domainType` | `domainType` | Domain |
| `type` | `dataProductType` | Data product |
| `tier` | `Tier.Tier1` to `Tier.Tier5` tag | Data product and its Gold tables in the stage being deployed |
| `classification` | `DataSensitivity.<value>` tag | Domain, data product and the product's Gold tables in the stage being deployed |
| `lifecycle` | Custom property `olfLifecycle` | Data product |
| `termsOfUse` | Custom property `olfTermsOfUse`, a JSON string | Data product |

- A reference resolves to an existing OpenMetadata user or team by name. An
  `owner` must be an existing user or a team of type `Group`: OpenMetadata
  refuses the Organization team. `experts` must be users. A reference that
  does not resolve, an owner team that is not a Group, or a team under
  `experts` fails the deploy before it writes anything, and the error names
  the `governance.yml` path.
- `Tier` and `DataSensitivity` allow one value each, so a different value on
  a table is replaced; tags from any other classification stay. OpenLakeForge
  creates the `DataSensitivity` classification with `Public`, `Internal`,
  `Confidential` and `Restricted` as needed. `Tier` ships with OpenMetadata.
- OpenMetadata 1.13.6 ignores a write to a data product's `lifecycleStage`
  (it answers 200 and keeps `DEVELOPMENT`), so `lifecycle` is stored in
  `olfLifecycle` and the native field stays at `DEVELOPMENT`.
- After a write the deploy reads the entity back and fails if OpenMetadata
  did not keep a field, instead of trusting the 200.
- A second deploy of the same descriptor writes nothing.

`consumesFrom`, `providesTo`, `sla` and glossary terms are published by other
changes.

A root `products:` list, a `domain` inside a product, or the same `id` under
two domains is rejected, with the location written as
`domains[sales].products[order_revenue]`.

`openlakeforge.io/v1beta1` documents declared the products in a root
`products:` list that named their domain. They do not load as `v1beta2`. Run
`olf project migrate --dry-run` to see the rewrite as a diff, then
`olf project migrate` to apply it. The command keeps comments with the entry they sit above, and the order, and
refuses, leaving the file as it was, on an unknown domain, a duplicate `id`, a
file that already nests products, or a layout it cannot rewrite safely. The
migration changes the project revision digest and no asset name: schemas,
jobs, buckets and catalog entries derive from the same domain and product
names (see [ADR 0012](../adr/0012-project-revisions-and-promotion.md)).

`olf check contracts` also requires one `silver/<domain>/` directory per
domain and one `gold/models/<product>/` directory per product, and rejects
a dbt model that dbt resolved into any schema but its folder's
`<product>_gold` (when the Gold project's `target/manifest.json` exists),
since its tables would belong to no product. `olf check contracts --strict`
also reports each product that resolves no `owner`, `tier` or `lifecycle`;
without `--strict` they may be omitted.

## Glossary

`lakehouse_code/glossaries/<name>.yml` holds business terms in Git, apart from
dbt code. Every `glossaries/*.yml` is discovered; `governance.yml` does not
list them.

```yaml
apiVersion: openlakeforge.io/v1alpha1
kind: Glossary
name: sales
displayName: Sales Vocabulary
description: Shared commercial definitions
owner: {type: team, ref: sales_data}
terms:
  - name: NetRevenue
    displayName: Net Revenue
    description: Revenue of fulfilled orders after line discounts and before cost.
    synonyms: [Net Sales]
    status: Approved
    relatedTerms: [sales.GrossRevenue]
    references:
      - {name: Finance policy, endpoint: "https://example.internal/finance/revenue"}
  - name: GrossRevenue
    displayName: Gross Revenue
    description: Revenue of fulfilled orders before line discounts.
```

The schema is [`glossary.schema.json`](../schema/glossary.schema.json).

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | `openlakeforge.io/v1alpha1` |
| `kind` | string | yes | `Glossary` |
| `name` | identifier | yes | The glossary's name, unique across glossaries. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string, non-empty | yes | |
| `owner` | reference | no | `{type: user\|team, ref: <name>}`, as in `governance.yml`. |
| `terms` | array, at least one | yes | The terms. |

Each term:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string | yes | Letters, digits and `_`, starting with a letter; no `.`. Unique in the glossary, ignoring case. |
| `displayName` | string, non-empty | yes | Human-readable name. |
| `description` | string, non-empty | yes | The definition. |
| `synonyms` | array of strings | no | Other names for the term. |
| `status` | enum | no | `Draft`, `In Review`, `Approved`, `Archived`, `Deprecated`, `Rejected` or `Unprocessed`. |
| `relatedTerms` | array of term references | no | `<glossary>.<Term>`; may point into another glossary. |
| `references` | array of `{name, endpoint}` | no | External pages for the term; `endpoint` is an `https` URL that does not contain `http://`. |
| `parent` | string | no | The `name` of another term in the same glossary. |

`status` takes the OpenMetadata 1.13.6 `EntityStatus` values. A term is
referenced as `<glossary>.<Term>`, for example `sales.NetRevenue`, whether or
not it has a `parent`.

A term is linked from three places, all by that reference:

| Where | Field |
| --- | --- |
| A domain or product in `governance.yml` | `glossaryTerms: [sales.NetRevenue]` |
| A column in a Gold `schema.yml` | `meta.openlakeforge.glossaryTerms: [sales.NetRevenue]` |
| Another term | `relatedTerms` |

`olf check contracts` fails on a duplicate glossary or term (names that differ
only by case collide), a missing description, a `parent` or `relatedTerms`
entry that names no term, a `parent` cycle, and a `glossaryTerms` reference in
`governance.yml` or dbt `meta` that names no term. Each message names the file
and the path inside it. A `glossaryTerms` entry on a `mart_` column is a
warning, not an error: marts are not linked column by column, so put the term
on the fact or dimension column that holds it.

Terms are business meaning. They are not classification tags and grant or
revoke no access. The loaded inventory carries the glossaries, their terms and
the resolved links; nothing publishes them to OpenMetadata yet.

## `alerts.yml`

Optional. It declares which conditions alert for which products and stages,
with which options, and through which routes (see
[ADR 0023](../adr/0023-alert-evaluation-and-webhook-delivery.md), alert
policy). A project without it gets no product alerts. Receivers and their
credentials stay in `openlakeforge.yaml`; a route is only a name for one of
them. The schema is [`alert-policy.schema.json`](../schema/alert-policy.schema.json).

```yaml
apiVersion: openlakeforge.io/v1beta1
kind: AlertPolicy
routes:
  on-call: on-call # route name: receiver name in openlakeforge.yaml
defaults:
  notify: [on-call]
rules:
  - match: {domain: "*", product: "*"}
    when:
      run_failed: {}
      quality: {level: error}
```

Every key, type, default and allowed value is in the
[alert policy reference](alert-policy.md). Quote a route, receiver or domain
name that YAML reads as a boolean (`on`, `off`, `yes`, `no`, `y`, `n`).
`olf check contracts` does not check receiver names, because only activation
has the profile.

## Implementation files

Each identity selects the files that implement it:

| Identity | Files |
| --- | --- |
| Source `name` | `lakehouse_code/bronze/<source>/` |
| Domain `name` | `lakehouse_code/silver/<domain>/`, whose `domain.yml` holds the domain's Floe defaults and whose `contracts/` holds one file per entity |
| Product `id` | `lakehouse_code/gold/models/<id>/` |
| Dashboard | `lakehouse_code/dashboards/superset/<name>/`, once it holds a `metadata.yaml` |

## Derived names

For a `crm` source, a `sales` domain and an `order_revenue` product:

| Name | Value |
| --- | --- |
| Silver namespace | `sales_silver`, shared by every product of the domain |
| Gold namespace | `order_revenue_gold` |
| Dagster job | `order_revenue_pipeline` |
| Floe report prefix | `floe/reports/sales/` |
| dbt artifact prefix | `run-artifacts/dbt/sales/order_revenue/` |

Tables are addressed as `<stage catalog>.<namespace>.<table>`, for example
`lakehouse_dev.order_revenue_gold.mart_order_revenue_by_day`. The stage
catalog is `lakehouse_<stage>`. Table names are not declared in a descriptor:
Silver tables are the entities of the domain's Floe files and Gold tables are
the product's dbt models.

Renaming a source, resource, domain, product or table changes these names, so
on a deployed stage plan it as a data migration. `displayName`, `description`
and `status` are metadata only.

## Validation errors

```bash
olf check contracts
```

| Error | Cause |
| --- | --- |
| Name must match source directory | `bronze/marketing_platform/source.yaml` says `name: marketing`. |
| Invalid identifier | A name such as `campaign-performance`; use `campaign_performance`. |
| Duplicate product id | Two products declare the same `id`. |
| Product names an unknown domain | A product's `domain` is not in `domains`. |
| Domain or product has no directory | `silver/<domain>/` or `gold/models/<product>/` is missing, or exists without an entry in `governance.yml`. |
| Silver entity reads an unknown Bronze resource (`olf check project-code`) | A Floe entity's `source.ref`/`resource` is not a resource of a `source.yaml`. |
| Alert rule names an unknown route, domain or product (`alerts.yml`) | `notify` is not a key of `routes`, or `match` is not in `governance.yml`, or the product is nested under another domain. |
| Alert rules overlap (`alerts.yml`) | Two rules of the same specificity set one condition for the same stage. |
| Model outside `<product>_gold` | A dbt model sets its own schema and a manifest shows it. |

`olf check contracts` reads each descriptor on its own. `olf check
project-code` loads the whole inventory, and catches a reference between
descriptors that does not resolve.

`olf init --empty` writes no `governance.yml`; the first `olf domain new` or
`olf product new` creates it. Only the scaffolding commands accept a project
without one; every check and deploy needs at least one product.

## Graph the lineage

```bash
olf project graph --project .
```

This prints the ownership chain the descriptors declare as Mermaid text:

| Edge | Read from |
| --- | --- |
| Source → Domain | `source.ref` of the domain's Floe entity files |
| Domain → Product | `products[].domain` |
| Product → Dashboard | the `<product>_gold` schema of the dashboard's dataset files |

Nodes and edges are sorted, so the same descriptors always print the same text.
Paste it into a `mermaid` code block on GitHub or into the
[Mermaid Live Editor](https://mermaid.live) to see it drawn. For the CRM/ERP
demo it prints:

```text
flowchart LR
    dashboard_sales_customer_health["sales_customer_health"]
    dashboard_sales_order_revenue["sales_order_revenue"]
    dashboard_supply_chain_inventory_reliability["supply_chain_inventory_reliability"]
    domain_sales["Sales"]
    domain_supply_chain["Supply Chain"]
    product_customer_health["Sales Customer Health"]
    product_inventory_reliability["Supply Chain Inventory Reliability"]
    product_order_revenue["Sales Order Revenue"]
    source_crm["CRM"]
    source_erp["ERP"]
    domain_sales --> product_customer_health
    domain_sales --> product_order_revenue
    domain_supply_chain --> product_inventory_reliability
    product_customer_health --> dashboard_sales_customer_health
    product_inventory_reliability --> dashboard_supply_chain_inventory_reliability
    product_order_revenue --> dashboard_sales_order_revenue
    source_crm --> domain_sales
    source_erp --> domain_supply_chain
```
