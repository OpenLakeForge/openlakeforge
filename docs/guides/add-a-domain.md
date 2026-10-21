# Add a domain

How to create a domain with `olf domain new`: the Silver namespace for a
business area and the Floe contract that validates Bronze data into it. A
domain groups the Silver tables its products consume.

The example creates `marketing` from the Bronze resource
`marketing_platform/campaigns`, so that source must exist and its
`examples/campaigns.csv` must hold your real columns: the contract's columns
are inferred from that file when the domain is created. Run every command from
the project directory.

## 1. Create the domain

```bash
olf domain new marketing --input marketing_platform/campaigns
```

```text
Created domain 'marketing' with 1 Silver table(s): campaigns.
Domain 'marketing' has no products yet; run `olf product new marketing/<product>` to add one.
```

`--input <source>/<resource>` names a Bronze resource to validate into a Silver
table of the same name. Repeat it for more tables. `--display-name` sets a
title other than the title-cased name. The name must match
`^[a-z][a-z0-9_]*$`. The command adds the domain to
`lakehouse_code/lakehouse.yaml`, with no products, and creates:

```text
lakehouse_code/silver/marketing/
├── __init__.py
├── README.md
└── contracts/
    └── floe/
        └── marketing.yml
```

`olf product new` also creates the domain when you give it `--input` for a
domain that does not exist yet, so you can skip this command and go straight
to a product.

## 2. Read the contract

`marketing.yml` holds the storage definitions and one entity per Silver table.
This is the `campaigns` entity:

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

- **Accepted sink.** Rows that satisfy the contract go to the Iceberg table
  `marketing_silver.campaigns`. The namespace is named after the domain, so
  every `marketing` product shares it, and a domain has exactly one contract.
- **Rejected sink.** With `severity: "reject"`, a row that breaks the contract
  goes to a CSV quarantine path in the Silver bucket instead. The run still
  succeeds.
- **Columns.** Every column is `nullable: false`, because the scaffold cannot
  know which are optional. Loosen a column by hand when a value may be empty.

> [!NOTE]
> Floe turns the contract into a runner manifest during `olf deploy`. The
> manifest is generated: never edit it by hand.

## Check it worked

With the platform deployed, generate the manifest:

```bash
olf floe generate-manifests
```

It validates each domain's contract and prints one `Generated` line per
domain, including one ending in `manifests/marketing/marketing.manifest.json`.
The next `olf deploy --provider local --phase artifacts` creates the
`marketing_silver` namespace in the catalog.
