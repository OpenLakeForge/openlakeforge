# Add a source

How to add a Bronze source to a project with `olf source new`, give it data,
and check that its descriptor is valid. A source is one system your data comes
from, owned by its own directory under `lakehouse_code/bronze/`.

You need an initialized project (`olf init`). The commands run from the
project directory.

> [!NOTE]
> This guide uses a CSV file because it is the fastest example, and a fixed
> CSV gives the same result on every run, which is why the demo products use
> them as test seeds. CSV is not a limit of the platform: dlt can extract from
> APIs and databases, and Floe reads CSV, TSV, JSON, Parquet, ORC, Avro, XLSX,
> XML and fixed-width files into Iceberg. Sources of those kinds are being
> added in [#110](https://github.com/OpenLakeForge/openlakeforge/issues/110),
> and this guide will cover them once they land.

## 1. Scaffold the source

```bash
olf source new marketing_platform --resource campaigns
```

```text
Created source 'marketing_platform' with 1 resource(s): campaigns.
Added 'marketing_platform' to lakehouse_code/lakehouse.yaml sources.
```

Repeat `--resource` for each resource the source exposes, and add
`--display-name` to set a title other than the title-cased name. The source
name must match `^[a-z][a-z0-9_]*$`: `marketing_platform`, not
`marketing-platform`. The command validates its result before it writes
anything, and writes nothing if validation fails. It creates:

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

## 2. Read the descriptor

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
any other provider; OpenLakeForge derives the physical names. Its `name` must
match its directory.

## 3. Add the data

The scaffold writes a placeholder CSV with a single column. Replace it with
your data:

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

Do this before you create a domain or a product that reads the resource: the
validation contract for Silver is inferred from this file when it is created.

## 4. Know the loader

`dlt/marketing_platform.py` reads each resource's CSV from `examples/` through
dlt and writes it to the Bronze bucket under the `marketing_platform` prefix.
Its core is one call:

```python
return load_entities_to_bronze(
    entities=entities,
    raw_dir=raw_dir or _RAW_DIR,
    bronze_prefix=_BRONZE_PREFIX,
)
```

Loading is keyed by source, so every product that needs
`marketing_platform.campaigns` calls this one loader. Nothing loads yet:
Dagster runs the loader as the first step of a product job.

## Check it worked

```bash
olf check contracts
```

It validates `lakehouse_code/lakehouse.yaml` and every `source.yaml` against
the project schemas, and prints one `[PASS]` line per check, including
`descriptor_schema_conformance`.
