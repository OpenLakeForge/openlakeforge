# Ingest a source

This lesson adds a new source to the project with `olf source new`, gives it
data, and explains the dlt loader that lands that data in Bronze. The source
is a marketing platform that exports campaign performance as CSV.

**You will** scaffold a Bronze source and give it real data, in about 10
minutes.

**Start from** the project of [lesson 1](01-tour-the-platform.md): deployed,
with the three demo data products. Run every command from the project
directory.

> [!NOTE]
> The lesson uses a CSV file because it is the fastest example, and a fixed
> CSV gives the same result on every run, which is why the demo products use
> them as test seeds. CSV is not a limit of the platform: dlt can extract from
> APIs and databases, and Floe reads CSV, TSV, JSON, Parquet, ORC, Avro, XLSX,
> XML and fixed-width files into Iceberg. Sources of those kinds are being
> added in [#110](https://github.com/OpenLakeForge/openlakeforge/issues/110).

## How the project is organized

Your code lives under `lakehouse_code/`, and each medallion layer has its own
owner (see
[ADR 0004](../adr/0004-medallion-layout-and-catalog-namespaces.md)):

| Layer | Aligned to | Directory |
| --- | --- | --- |
| Bronze | A source | `lakehouse_code/bronze/<source>/` |
| Silver | A domain | `lakehouse_code/silver/<domain>/` |
| Gold | A data product | `lakehouse_code/gold/<product>/` |

Across the next lessons you add the source `marketing_platform`, the domain
`marketing` and the product `campaign_performance`. OpenLakeForge derives
every platform name from those three: the Bronze namespace
`marketing_platform_bronze`, the Silver namespace `marketing_silver`, the Gold
namespace `campaign_performance_gold` and the Dagster job
`campaign_performance_pipeline`. You never create these by hand and you never
edit Terraform to register a product.

## 1. Scaffold the source

```bash
olf source new marketing_platform --resource campaigns
```

```text
Created source 'marketing_platform' with 1 resource(s): campaigns.
```

The command validates its result before it writes anything, and writes
nothing if validation fails. It creates:

```text
lakehouse_code/bronze/marketing_platform/
├── source.yaml
├── README.md
├── examples/
│   └── campaigns.csv
└── dlt/
    ├── __init__.py
    └── marketing_platform.py
```

## 2. Read the source descriptor

`source.yaml` is the logical description of the source:

```yaml
apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: marketing_platform
displayName: "Marketing Platform"
description: "Marketing Platform source system."
status: planned
resources:
  - name: campaigns
    description: "Raw CSV campaigns."
```

It lists the resources the source exposes and never names Polaris, Glue, S3 or
any other provider: OpenLakeForge derives the physical names from it. Its
`name` must match its directory.

## 3. Add the data

The scaffold wrote a placeholder file with a single column. Replace it with
campaign data:

```bash
cat > lakehouse_code/bronze/marketing_platform/examples/campaigns.csv <<'CSV'
campaign_id,campaign_date,channel,spend,conversions
cmp_001,2026-08-01,search,120.50,18
cmp_002,2026-08-01,social,80.00,9
cmp_003,2026-08-02,search,150.00,24
cmp_004,2026-08-02,email,30.00,12
cmp_005,2026-08-03,social,95.50,11
CSV
```

Do this before the next lesson: the validation contract for Silver is
inferred from this file when you create the domain.

## 4. Read the loader

`dlt/marketing_platform.py` is the Bronze loader. It reads each resource's CSV
from `examples/` through dlt, writes it to the Bronze bucket under the
`marketing_platform` prefix, and returns the row count, the `bronze_uri` and
the `source_file` to Dagster, which shows them on the asset. Its core is one
call:

```python
return load_entities_to_bronze(
    entities=entities,
    raw_dir=raw_dir or _RAW_DIR,
    bronze_prefix=_BRONZE_PREFIX,
)
```

Loading is keyed by source, so every product that needs
`marketing_platform.campaigns` calls this one loader. Nothing loads yet:
Dagster runs the loader as the first step of a product job, which you create
in lesson 4.

## Check it worked

```bash
olf check contracts
```

It validates `lakehouse_code/governance.yml` and every `source.yaml` against
the project schemas. It prints one `[PASS]` line per check, including:

```text
[PASS] descriptor_schema_conformance: 4 descriptor(s) validated
```

## What you learned

- `olf source new` scaffolds a source-owned Bronze package: a descriptor, a
  dlt loader and example data.
- Descriptors stay provider-neutral; OpenLakeForge derives physical names.
- Bronze fills when a product job runs.

## Next

[Validate into Silver](03-validate-into-silver.md): declare what a valid row
looks like.
