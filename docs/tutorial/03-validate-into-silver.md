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
        ├── _domain.yml
        └── campaigns.yml
```

It also declares `marketing_platform` in `lakehouse_code/silver/sources.yml`,
the named Bronze sources every domain's entities read from.

## 2. Read the contract

The Floe contract of a domain is split in two. `_domain.yml` holds what every
entity of the domain shares:

```yaml
name: "marketing"
defaults:
  incremental_mode: "none"
  sink:
    write_mode: "overwrite"
    accepted:
      format: "iceberg"
      path: "marketing/{{entity.name}}"
      storage: "lakehouse_silver"
      iceberg: { catalog: "iceberg_catalog", namespace: "marketing_silver" }
    rejected: { format: "csv", path: "floe/rejected/marketing/{{entity.name}}", storage: "lakehouse_silver" }
  policy: { severity: "reject" }
  schema:
    normalize_columns: { enabled: true, strategy: "snake_case" }
```

Each other file in the folder is one entity. `campaigns.yml` names its Bronze
input and the columns inferred from the CSV you wrote in lesson 2:

```yaml
name: "campaigns"
source: { ref: "marketing_platform", resource: "campaigns" }
schema:
  primary_key: ["campaign_id"]
  columns:
    - { name: "campaign_id", type: "string", nullable: false }
    - { name: "campaign_date", type: "date", nullable: false }
    - { name: "channel", type: "string", nullable: false }
    - { name: "spend", type: "number", nullable: false }
    - { name: "conversions", type: "integer", nullable: false }
```

Four parts matter:

- **Source and cast mode.** `ref: "marketing_platform"` points at the source in
  `sources.yml`, which reads the CSV dlt wrote to Bronze and casts each value
  to its column type strictly.
- **Accepted sink.** Rows that satisfy the contract are written to the Iceberg
  table `marketing_silver.campaigns`. The namespace is named after the
  *domain*, not a product: every `marketing` product shares it.
- **Rejected sink.** With `severity: "reject"`, a row that breaks the contract
  goes to the CSV quarantine path `floe/rejected/marketing/campaigns` in the
  Silver bucket instead of into Silver. The run still succeeds: Floe reports
  it as a run with rejected rows, which is not a technical failure.
- **Columns.** Every column is `nullable: false`, because the scaffold cannot
  know which are optional. Loosen a column by hand when a value may be empty.

> [!NOTE]
> Nothing here names a bucket or a catalog endpoint. Dagster runs Floe, one
> Kubernetes Job per domain, with a profile it renders for the stage it is
> deployed to, so the same contract validates into DEV and PROD.

## Check it worked

```bash
olf check contracts
```

It checks that each domain writes its own `<domain>_silver` namespace through
the logical catalog. Among its lines:

```text
[PASS] floe_contract_structure: 19 Floe domain and entity file(s) validated
```

## What you learned

- `olf domain new` declares the Silver namespace and writes its Floe contract
  in one step -- domain defaults plus one file per entity -- inferring column
  types from the example CSV. The next deploy creates the namespace in the
  catalog.
- Rows that satisfy the contract reach `marketing_silver.campaigns`; rows that
  do not are quarantined, not dropped and not mixed into Silver.
- Silver is owned by the domain; products consume it.

## Next

[Model Gold with dbt](04-model-gold-with-dbt.md): build a business mart on top
of Silver.
