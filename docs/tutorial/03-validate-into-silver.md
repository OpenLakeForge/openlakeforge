# Validate into Silver

This lesson creates the `marketing` domain with `olf domain new`, which writes
the Floe contract that turns the Bronze CSV into a validated Silver Iceberg
table. It explains what the contract checks and where rows that fail it go.

**You will** create a domain and read its validation contract, in about 10
minutes.

**Start from** the project of [lesson 2](02-ingest-a-source.md): the
`marketing_platform` source exists and `campaigns.csv` holds the five campaign
rows.

## 1. Create the domain

```bash
olf domain new marketing --input marketing_platform/campaigns
```

```text
Created domain 'marketing' with 1 Silver table(s): campaigns.
Domain 'marketing' has no products yet; run `olf product new marketing/<product>` to add one.
```

A domain groups the Silver tables a business area owns. `--input
<source>/<resource>` names a Bronze resource to validate into one of them. The
command adds the domain to `lakehouse_code/lakehouse.yaml`, with the Silver
table `campaigns` mapped to `marketing_platform/campaigns`, and creates:

```text
lakehouse_code/silver/marketing/
├── __init__.py
├── README.md
└── contracts/
    └── floe/
        └── marketing.yml
```

## 2. Read the contract

`marketing.yml` is the Floe contract of the domain. Besides the storage
definitions, it holds one entity per Silver table. This is the `campaigns`
entity, with its columns inferred from the CSV you wrote in lesson 2:

```yaml
entities:
  - name: "campaigns"
    incremental_mode: "none"
    source:
      format: "csv"
      path: "marketing_platform/campaigns"
      storage: "lakehouse_bronze"
      cast_mode: "strict"
    sink:
      write_mode: "overwrite"
      accepted:
        format: "iceberg"
        path: "marketing/campaigns"
        storage: "lakehouse_silver"
        iceberg: { catalog: "iceberg_catalog", namespace: "marketing_silver", table: "campaigns" }
      rejected: { format: "csv", path: "floe/rejected/marketing/campaigns", storage: "lakehouse_silver" }
    policy: { severity: "reject" }
    schema:
      primary_key: ["campaign_id"]
      columns:
        - { name: "campaign_id", type: "string", nullable: false }
        - { name: "campaign_date", type: "date", nullable: false }
        - { name: "channel", type: "string", nullable: false }
        - { name: "spend", type: "number", nullable: false }
        - { name: "conversions", type: "integer", nullable: false }
```

The entity file also sets CSV read options and snake_case column
normalization, omitted here. Four parts matter:

- **Source and cast mode.** Floe reads the CSV that dlt wrote to Bronze and
  casts each value to its column type strictly.
- **Accepted sink.** Rows that satisfy the contract are written to the Iceberg
  table `marketing_silver.campaigns`. The namespace is named after the
  *domain*, not a product: every `marketing` product shares it, and a domain
  has exactly one Floe contract.
- **Rejected sink.** With `severity: "reject"`, a row that breaks the contract
  goes to the CSV quarantine path `floe/rejected/marketing/campaigns` in the
  Silver bucket instead of into Silver. The run still succeeds: Floe reports
  it as a run with rejected rows, which is not a technical failure.
- **Columns.** Every column is `nullable: false`, because the scaffold cannot
  know which are optional. Loosen a column by hand when a value may be empty.

> [!NOTE]
> Floe turns this contract into a runner manifest during `olf deploy`. The
> manifest is generated: never edit it by hand.

## Check it worked

```bash
olf floe generate-manifests
```

It validates each domain's contract and generates its manifest. It prints one
`Generated` line per domain, including:

```text
==> Generated .../manifests/marketing/marketing.manifest.json
```

## What you learned

- `olf domain new` declares the Silver namespace and writes its Floe contract
  in one step, inferring column types from the example CSV. The next deploy
  creates the namespace in the catalog.
- Rows that satisfy the contract reach `marketing_silver.campaigns`; rows that
  do not are quarantined, not dropped and not mixed into Silver.
- Silver is owned by the domain; products consume it.

## Next

[Model Gold with dbt](04-model-gold-with-dbt.md): build a business mart on top
of Silver.
