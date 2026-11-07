---
name: building-openlakeforge-pipelines
description: >-
  Build an end-to-end OpenLakeForge data product: ingest with dlt into Bronze,
  validate with Floe into Silver, model Gold with dbt-trino, orchestrate with
  Dagster, declare domains/products in governance.yml, and publish Superset
  dashboards. Use for creating or modifying ETL/ELT pipelines and data products
  in an existing OpenLakeForge lakehouse project.
license: Apache-2.0
---

# Building OpenLakeForge pipelines

Build **source → Bronze (dlt) → Silver (Floe) → Gold (dbt) → dashboard
(Superset)**. Dagster orchestrates the layers; `governance.yml` declares
business domains and data products, not tables. Start in a project containing
`openlakeforge.yaml` and `lakehouse_code/`. For a missing deployment, use
the `deploying-openlakeforge` skill; this skill changes project code.

## Rules

- Inspect existing `lakehouse_code/` assets and user requirements first.
  Reuse sources and domain tables rather than generating duplicates.
- **Scaffold with `olf`**, then edit generated files. Do not hand-create
  Terraform, Helm resources, catalog namespaces, or Dagster registrations.
- Never commit passwords, tokens, hosts, bucket names or credentials in
  `governance.yml` or `source.yaml`. Bind source runtime keys per stage.
- Keep DEV as the default for tests. Ask before a deploy, job launch or data
  rewrite, and always before PROD, cloud infrastructure or a full refresh.
  Do not launch every demo product merely to test the new one.
- Run one `olf` command at a time. Don't bypass identity/Polaris refusals
  through `olf infra query`. When a task needs detailed Floe rules, load
  `building-floe-contracts` from `OpenLakeForge/floe` if installed.

## 1. Scaffold the data path

For a new CSV source, **replace its example CSV with real sample columns
before scaffolding Silver** (the contract is inferred from the sample):

```bash
olf source new marketing_platform --resource campaigns
olf domain new marketing --input marketing_platform/campaigns
olf product new marketing/campaign_performance --silver-input campaigns --gold-table mart_campaign_performance --with-report
```

The domain step is optional: `olf product new ... --input
marketing_platform/campaigns` can create a missing domain and Silver entity
in one operation. For APIs/databases, scaffold the source using
`--kind api` or `--kind sql` with the actual `--cursor` and
`--primary-key` before implementing its dlt extraction logic.

## 2. Edit only the owning layer

| Layer | Files to edit | Contract |
| --- | --- | --- |
| Bronze | `bronze/<source>/source.yaml`, `bronze/<source>/dlt/` | Describe source resources and full/incremental load; implement the dlt extractor with declared runtime config/secret keys. |
| Silver | `silver/floe.yml`, `silver/sources.yml`, `silver/<domain>/contracts/floe/` | One entity per input; validate columns, nullability, keys, rejected-row policy and Iceberg sink. Generated `_domain.yml` supplies stage-neutral defaults. |
| Gold | `gold/<product>/dbt/models/` | Transform with dbt-trino; read `{{ source('silver', 'campaigns') }}`, add tests and descriptions in `models/gold/schema.yml`. |
| Orchestration | `pipelines/dagster/<product>.py` | The scaffolded `ProductDefinitionSpec(domain, product)` derives Gold marts from the dbt models, Silver inputs from the dbt sources and Bronze resources from the Floe entities; pass `gold_assets`, `silver_inputs` or `bronze_inputs` only to override. `definitions.py` discovers modules automatically. |
| Governance | `governance.yml` | Define domain `name` and, nested in its `products`, product `id`, plus business descriptions. Never list tables here or embed physical storage/catalog config. |
| Dashboard | `dashboards/superset/<dashboard>/` | Superset charts and datasets must read Gold schemas. A draft generated with `--with-report` is **not** a deployable dashboard until exported with `metadata.yaml`. |

Namespace conventions: `<domain>_silver`, `<product>_gold` (Bronze is raw
files in the stage Bronze bucket and has no catalog namespace); the Dagster job is `<product>_pipeline`.
Source identifiers, domains, products and table names use lowercase
underscore-separated identifiers.

For incremental sources, keep the three layers consistent: dlt's
`source.yaml` resource uses `load.mode: incremental` and a cursor;
Floe uses `incremental_mode: file` with `merge_scd1`,
`merge_scd2` or `append` (Iceberg merges need a primary key);
dbt may use incremental materialization with a unique key.
Do not append repeated full snapshots. Use OLF's generated stage bindings,
not a standalone Floe profile copied into the project.

## 3. Validate before deploying

```bash
olf check contracts
olf check project-code
olf check dbt
olf report validate
```

Fix contract errors, missing Dagster inputs, dbt compilation/test issues and
report bundle problems. The checks do **not** prove the pipeline's runtime
results are correct. After the person authorizes deployment, apply code
changes to an existing local platform:

```bash
olf infra deploy --provider local --phase artifacts
```

## 4. Execute and verify DEV

After explicit approval to launch the pipeline:

```bash
olf jobs list --stage dev --json
olf runs launch campaign_performance_pipeline --stage dev --json
olf runs list --stage dev --job campaign_performance_pipeline --json
olf query "SELECT * FROM campaign_performance_gold.mart_campaign_performance LIMIT 10" --stage dev --format json
```

Inspect one run using `olf runs show RUN_ID --stage dev --json`,
and use `olf logs --stage dev --limit 100 --json` when troubleshooting.
The person-facing `olf query` requires a supported GitHub identity sign-in;
use `olf login` with the person when needed, not service credentials.
Check row counts, expected grain, dbt tests, quality rejections, and Gold
values; a successful job alone does not establish data correctness.

## 5. Superset and governance

Superset and OpenMetadata require `spec.preset: full`; do not change the
preset or deploy services without approval. Create charts in Superset against
the Gold mart, save the dashboard and export it to the generated bundle:

```bash
olf report export campaign-summary lakehouse_code/dashboards/superset/campaign_performance --stage dev
olf report validate
```

Use the dashboard's actual UUID or slug instead of `campaign-summary`.
Then include its exported YAML bundle in the next approved artifacts deploy.
The Full preset publishes `governance.yml` domains/products to OpenMetadata;
Floe and dbt produce lineage after runs. Validate the relevant metadata and
dashboard in the UIs, not just the CLI result. Report what was validated,
what remains untested, and any stage-specific limitations.
