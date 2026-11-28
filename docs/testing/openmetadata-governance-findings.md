# OpenMetadata 1.13.6 governance probe findings

What the pinned OpenMetadata 1.13.6 accepts, ignores and keeps for the
governance work of v0.7.5 (#918, #920, #1464, #1465), measured on a real DEV
stage by [`openmetadata_governance_probe.py`](openmetadata_governance_probe.py).
Payloads below are the ones that were accepted; they come from the probe's
JSON artifact, not from the schema.

## How it was measured

- Stack: the conformance profile, DEV only, deployed by
  `local-full-e2e.yml` with `governance_probe=true` after the core e2e suite
  (runs 46069630392, 46074376100, 46078646784 and 46083198183).
- OpenMetadata identity: the automation bot (`olf-automation`), the token
  `olf infra e2e run` uses. Trino identity: the stage processing principal.
  The crawl and the profiler ran as the OLF-owned Job of ADR 0007 D15 (the
  pinned `ingestion-base` image, the ingestion bot, the `openmetadata`
  Polaris principal).
- Every write is a JSON Patch (`application/json-patch+json`) unless a
  `POST` or `PUT` is named. Probe entities are named `olfprobe_*` and are
  removed afterwards; the two Gold tables it patched are restored.
- Gold tables reach OpenMetadata only through a crawl that runs after the
  pipelines. The hourly CronJob is the only crawl besides `deploy-metadata`,
  so a deploy followed at once by code that reads Gold tables from
  OpenMetadata must run the crawl itself (the probe does).

## Verdicts

| # | Question | Verdict |
| --- | --- | --- |
| 1 | PATCH `DataProduct.lifecycleStage` | Ignored: 200, value unchanged |
| 2a | PATCH `dataProductType` | Accepted |
| 2b | PATCH `sla` | Accepted |
| 2c | PATCH `consumesFrom` | Accepted |
| 2d | PATCH `experts`, `reviewers` | Accepted |
| 2e | PATCH `owners` | Accepted for a Group team or user; refused for the Organization team |
| 2f | PATCH `Domain.domainType` | Accepted |
| 3 | Create a `Metric` with expression, type, owners, domain, data product, glossary tag, extension | Accepted |
| 3a | Provision an extension property on `Metric` | Accepted |
| 3b | Lineage edge table → metric | Accepted |
| 4 | `Table.tableConstraints` survive the crawl | Survive two crawls |
| 5 | Iceberg comments reach OpenMetadata | First crawl only; `overrideMetadata` refreshes them |
| 6 | Glossary tag on a column survives the crawl | Survives |
| 7 | `metadata profile` | Accepted: table and column statistics stored, no sample data |

## 1. `lifecycleStage` is not writable

The schema says the field is "driven by the governance workflow, not
directly user-editable". On 1.13.6:

- `PATCH [{"op":"add","path":"/lifecycleStage","value":"PRODUCTION"}]`
  returns 200, and so do `replace` to `TESTING` and `add` to `DEPRECATED`.
  Every read afterwards returns `DEVELOPMENT`, the schema default, at once,
  after 20 seconds and after the second patch.
- `POST /api/v1/dataProducts` with `lifecycleStage` returns 400 `Invalid
  request format`: `CreateDataProductRequest` has no such property.
- The server defines three governance workflows (`AutoPilotWorkflow`,
  `GlossaryTermApprovalWorkflow`, `RecognizerFeedbackReviewWorkflow`); none
  rewrote the value, which is consistent with the field being ignored on
  write.

Fallback for #918: store the lifecycle in an extension custom property on
the data product (provisioned like 3a) and say in the docs that the native
field stays at `DEVELOPMENT`. A 200 from the PATCH must not be read as
success; read the value back.

## 2. Native data product and domain fields

All accepted by the bot and read back as written.

| Field | Accepted payload |
| --- | --- |
| `dataProductType` | `{"op":"add","path":"/dataProductType","value":"DATASET"}`. The 1.13.6 enum: `RAW_DATA`, `DERIVED_DATA`, `DATASET`, `REPORTS`, `ANALYTIC_VIEW`, `VISUALISATION_3D`, `ALGORITHM`, `DECISION_SUPPORT`, `AUTOMATED_DECISION_MAKING`, `DATA_ENHANCED_PRODUCT`, `DATA_DRIVEN_SERVICE`, `DATA_ENABLED_PERFORMANCE`, `BI_DIRECTIONAL`. |
| `sla` | `{"op":"add","path":"/sla","value":{"tier":"GOLD","availability":99.9,"dataFreshness":1440,"dataQuality":95.0,"responseTime":2000}}`. `tier` is `GOLD`, `SILVER`, `BRONZE` or `CUSTOM` (`Gold` is rejected with HTTP 500); `availability` and `dataQuality` are 0 to 100; `dataFreshness` is minutes; `responseTime` is milliseconds. Unknown keys are rejected with HTTP 500 listing the five known ones. |
| `consumesFrom` | `{"op":"add","path":"/consumesFrom","value":[{"id":"<data product id>","type":"dataProduct"}]}` |
| `experts` | `{"op":"add","path":"/experts","value":[{"id":"<user id>","type":"user"}]}` |
| `reviewers` | the same shape as `experts` |
| `owners` | `{"op":"add","path":"/owners","value":[{"id":"<team id>","type":"team"}]}` with a team of `teamType` `Group` |
| `Domain.domainType` | `{"op":"add","path":"/domainType","value":"Consumer-aligned"}`; the other values are `Source-aligned` and `Aggregate` |

Constraints found on the way:

- The built-in `Organization` team cannot own entities: HTTP 400 `Team of
  type Organization can't own entities. Only Team of type Group can own
  entities.` An unresolved or non-Group owner must fail the deploy with
  that in mind.
- `GET` returns the relationship fields (`owners`, `experts`, `reviewers`,
  `domains`, `consumesFrom`, `providesTo`, `tags`, `extension`) only when
  named in `fields`. A `fields` list naming one the entity lacks (for
  example `dataProducts` on a data product) is refused as a whole, so a
  client that reads back must ask per entity kind.
- `DataProduct.providesTo` on the upstream product was not returned after
  the `consumesFrom` edit in the probe's read (`fields=...providesTo`); do
  not rely on the reciprocal edge being visible until it is read on its own.
  This was not investigated further.

## 3. Metrics

One `POST /api/v1/metrics` was accepted with every field the #920 mapping
uses:

```json
{
  "name": "net_revenue",
  "displayName": "Net revenue",
  "description": "...",
  "metricExpression": {"language": "SQL", "code": "SUM(CASE WHEN order_status = 'fulfilled' THEN net_amount END)"},
  "metricType": "SUM",
  "owners": [{"id": "<group team id>", "type": "team"}],
  "domains": ["<domain fqn>"],
  "dataProducts": ["<data product fqn>"],
  "tags": [{"tagFQN": "<glossary>.<term>", "source": "Glossary", "labelType": "Manual", "state": "Confirmed"}],
  "extension": {"olfProbeNote": "hello"}
}
```

Everything read back, including the expression, `metricType` and the
extension. `metricType` values on 1.13.6: `COUNT`, `SUM`, `AVERAGE`,
`RATIO`, `PERCENTAGE`, `MIN`, `MAX`, `MEDIAN`, `MODE`, `STANDARD_DEVIATION`,
`VARIANCE`, `OTHER`; `metricExpression.language`: `SQL`, `Java`,
`JavaScript`, `Python`, `External`. `PATCH` of `description` and
`metricExpression` returned 200.

- A metric's `dataProducts` must belong to its `domains`: without the
  domain the create fails with 400 `Rule [Data Product Domain Validation]
  validation failed`.
- The extension key must exist as a custom property first. Provisioning
  (3a), accepted: `PUT /api/v1/metadata/types/<metric type id>` with
  `{"name":"olfProbeNote","description":"...","propertyType":{"id":"<string
  type id>","type":"type"}}`; the ids come from `GET
  /api/v1/metadata/types/name/metric` and `.../name/string`.
- Lineage (3b), accepted: `PUT /api/v1/lineage` with
  `{"edge":{"fromEntity":{"id":"<table id>","type":"table"},"toEntity":{"id":"<metric id>","type":"metric"},"lineageDetails":{"source":"Manual","description":"computed from"}}}`.
  `GET /api/v1/lineage/metric/<id>?upstreamDepth=1` returns the edge, with
  `createdBy` the automation bot.

No fallback is needed for #920's Metric mapping.

## 4. Table constraints survive the crawl

`PATCH` on a crawled Gold table, accepted and unchanged after two crawls
(the OLF crawl Job, and an `overrideMetadata` crawl):

```json
[{"op": "add", "path": "/tableConstraints", "value": [
  {"constraintType": "PRIMARY_KEY", "columns": ["segment"]},
  {"constraintType": "FOREIGN_KEY", "columns": ["region"],
   "referredColumns": ["<referred table fqn>.<column>"],
   "relationshipType": "MANY_TO_ONE"}]}]
```

`UNIQUE` on a second table survived as well. `referredColumns` holds the
column's fully qualified name. `constraintType` values: `UNIQUE`,
`PRIMARY_KEY`, `FOREIGN_KEY`, `SORT_KEY`, `DIST_KEY`, `CLUSTER_KEY`;
`relationshipType`: `ONE_TO_ONE`, `ONE_TO_MANY`, `MANY_TO_ONE`,
`MANY_TO_MANY`. No fallback is needed. The probe did not test a table that
the crawl sees recreated with different columns.

## 5. Comments through Trino, Iceberg and Polaris

On the stage catalog, as the processing principal, all of these ran:

```sql
COMMENT ON TABLE lakehouse_dev.<schema>.<table> IS '...';
COMMENT ON COLUMN lakehouse_dev.<schema>.<table>.id IS '...';
CREATE OR REPLACE TABLE lakehouse_dev.<schema>.<table> AS SELECT ...;
```

- `DESCRIBE` shows the column comment; `system.metadata.table_comments`
  shows the table comment.
- `CREATE OR REPLACE TABLE` (what dbt-trino's `on_table_exists=replace`
  emits) drops the column comment; `DESCRIBE` showed none afterwards (the
  table comment was not read at that point). The comment is back after a
  new `COMMENT ON`, which is the statement `persist_docs` runs after the
  build, so the order is replace, then comment.
- The first crawl that sees a table copies the table and column comments
  into OpenMetadata descriptions.
- A plain re-crawl does not refresh a description that is already set:
  after the Trino comments changed to v2 the table description stayed v1.
  A description a human edited stays too.
- A crawl whose source config sets `overrideMetadata: true` (accepted by
  1.13.6) refreshed both descriptions to v2 and replaced the human's column
  description with the Trino comment. It left a glossary tag on a column, a
  human description on another column and the table constraints untouched.

Consequences for #1464: with the current crawl, a dbt description change
reaches OpenMetadata only for a table the crawl has not seen. To keep
descriptions current the crawl needs `overrideMetadata: true`, at the price
that Trino's comment then wins over a human edit of that description;
nothing else the probe set was overwritten. Without it, the fallback is to
write the descriptions through the OpenMetadata API from the dbt manifest.
The `overrideMetadata` crawl in the measured runs was catalog-wide (46
records updated), so its effect on other tables was not recorded beyond the
three kinds above; the probe now restricts it to the tables it can restore
(`tableFilterPattern` with `useFqnForFiltering`). A catalog-wide
`overrideMetadata` crawl in a real deployment would replace every human
description whose Trino comment differs.

## 6. Glossary tags on columns

`PATCH [{"op":"add","path":"/columns/0/tags","value":[{"tagFQN":"<glossary>.<term>","source":"Glossary","labelType":"Manual","state":"Confirmed"}]}]`
was accepted and the tag was still on the column after both crawls and the
`overrideMetadata` crawl. A human column description survived them too,
apart from the case in 5. No fallback is needed.

## 7. `metadata profile`

Run as the crawl's own Job with `metadata profile -c`, using the crawl's
Trino connection and a `Profiler` source config, with a processor
`{"type": "orm-profiler", "config": {}}`. The Job completed and the
profiler workflow reported success in under three seconds.

Options 1.13.6 accepts under `sourceConfig.config` (the properties of
`DatabaseServiceProfilerPipeline`): `type`, `classificationFilterPattern`,
`computeColumnMetrics`, `computeTableMetrics`, `databaseFilterPattern`,
`schemaFilterPattern`, `tableFilterPattern`, `includeViews`, `metrics`,
`processingEngine`, `profileSampleConfig`, `randomizedSample`,
`threadCount`, `timeoutSeconds`, `useFqnForFiltering`, `useStatistics`.
There is no `generateSampleData`, `profileSample`, `profileSampleType` or
`processPiiSensitive` in the model, so the probe did not send them;
sampling is `profileSampleConfig`, whose shape was not tested. `metrics` takes any of: `mean`, `valuesCount`, `countInSet`,
`columnCount`, `distinctCount`, `distinctProportion`, `iLikeCount`,
`likeCount`, `notLikeCount`, `regexCount`, `notRegexCount`, `max`,
`maxLength`, `min`, `minLength`, `nullCount`, `rowCount`, `stddev`, `sum`,
`uniqueCount`, `uniqueProportion`, `columnNames`, `duplicateCount`,
`iLikeRatio`, `likeRatio`, `nullProportion`, `interQuartileRange`,
`nonParametricSkew`, `median`, `firstQuartile`, `thirdQuartile`, `system`,
`histogram`, `cardinalityDistribution`, `valueRank`, `nullMissingCount`.

Accepted config:

```yaml
sourceConfig:
  config:
    type: Profiler
    computeTableMetrics: true
    computeColumnMetrics: true
    metrics: [rowCount, valuesCount, nullCount, nullProportion, distinctCount, min, max, mean]
    threadCount: 1
    timeoutSeconds: 900
    includeViews: false
    databaseFilterPattern: {includes: ["^lakehouse_dev$"]}
    schemaFilterPattern: {includes: ["^order_revenue_gold$"]}
    tableFilterPattern: {includes: ["^<table>$"]}
processor: {type: orm-profiler, config: {}}
```

What it stored, on a probe table with a `decimal(18,2)`, a `date`, a
`varchar` and a `bigint` column and four rows (run 46083198183):

- Table level, `GET /api/v1/tables/<table fqn>/tableProfile?startTs=<ms>&endTs=<ms>`:
  `{"timestamp": ..., "columnCount": 4.0, "rowCount": 4.0, "profileSampleType": "PERCENTAGE"}`.
- Column level, `GET /api/v1/tables/<column fqn>/columnProfile?startTs=<ms>&endTs=<ms>`
  (the path takes the column's fully qualified name, not the table's):
  `valuesCount`, `nullCount`, `nullProportion`, `distinctCount`, `min`,
  `max`, `mean` and nothing else, so the `metrics` list limits what is
  stored. Values returned:

| Column | Type | `nullCount` | `distinctCount` | `min` | `max` | `mean` |
| --- | --- | --- | --- | --- | --- | --- |
| `amount` | `decimal(18,2)` | 1 | 3 | 10.5 | 40.0 | 23.58 |
| `order_date` | `date` | 0 | 4 | `"2026-01-01"` | `"2026-04-01"` | not stored |
| `name` | `varchar` | 1 | 2 | 1 | 1 | 1.0 |
| `id` | `bigint` | 0 | 4 | 1 | 4 | 2.5 |

`min` and `max` are the real values for numeric and date columns, which is
why #1465 must leave them out of the `metrics` list for Confidential
products. For a `varchar` column they are string lengths (the four-row
sample had one-character strings), not values; `mean` is the mean length.

Where it does not show: `GET /api/v1/tables/name/<fqn>?fields=columns,profile`
returns no profile at all, and `columns[].profile` is empty on every column,
so a client reading statistics must use the two endpoints above.

No sample data was stored: neither the table response nor `GET
/api/v1/tables/<id>/sampleData` carried any (the 1.13.6 profiler model has no
sample-data option to turn off). The log has one warning, `No implementation
found for trino`, which appears at the start of the profiler run, before
the metrics; its cause was not investigated, and the statistics above were
stored regardless. Only the explicit metric list was run; the default list
was not needed.

## Out of scope and untested

- Classification and Tier tags on data products and tables (#918).
- Domain `owners`, `experts`, glossary-term writes through the API.
- Re-crawl of a table whose columns changed.
- PROD, and any table other than the two DEV Gold tables patched.
