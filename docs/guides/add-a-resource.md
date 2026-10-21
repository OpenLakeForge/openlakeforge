# Add a resource to a source

How to add one more resource, such as a second CSV file, to a source that
already exists, and carry it into the Silver layer of a domain. A resource is
one table-like input of a source, listed in its `source.yaml`.

The example adds `ad_groups` to the source `marketing_platform` and to the
domain `marketing`. It assumes both exist. Run every command from the project
directory.

## 1. Declare the resource

Add it to `lakehouse_code/bronze/marketing_platform/source.yaml`:

```yaml
resources:
  - name: campaigns
    description: "Raw CSV campaigns."
  - name: ad_groups
    description: "Raw CSV ad groups."
```

## 2. Add its data

The loader reads `<resource>.csv` from the source's `examples/` directory:

```bash
cat > lakehouse_code/bronze/marketing_platform/examples/ad_groups.csv <<'CSV'
ad_group_id,campaign_id,name
ag_001,cmp_001,brand
ag_002,cmp_003,generic
CSV
```

## 3. List it in the loader

In `lakehouse_code/bronze/marketing_platform/dlt/marketing_platform.py`, add the
resource to the entity tuple:

```python
MARKETING_PLATFORM_ENTITIES = ("campaigns", "ad_groups")
```

The tuple lists what `load_all_entities_to_bronze` loads. The platform takes
the resources it runs from `source.yaml`, so keep the two in step.

## 4. Carry it into Silver

Silver is owned by the domain, so the new table is declared in the domain's
`lakehouse.yaml` block and validated by an entity in its Floe contract. Let
`olf` write both while it creates a product that reads the resource:

```bash
olf product new marketing/ad_group_report \
  --input marketing_platform/ad_groups --gold-table mart_ad_groups
```

`--input <source>/<resource>` adds the Silver table `ad_groups` to the existing
domain, appends an entity to `silver/marketing/contracts/floe/marketing.yml`
with columns inferred from `ad_groups.csv`, and creates the new product. The
command refuses to run if the domain already has a Silver table of that name,
or if the resource is not declared in `source.yaml` (step 1).

To let an existing product read the resource instead, make these edits by
hand, copying an entity from the same contract for the Floe part:

| File | Add |
| --- | --- |
| `lakehouse_code/lakehouse.yaml` | A `silver_tables` entry `{name: ad_groups, source: marketing_platform, resource: ad_groups}` under the domain, and `ad_groups` to the product's `silver_inputs` |
| `lakehouse_code/silver/marketing/contracts/floe/marketing.yml` | An `ad_groups` entity, with its columns, under `entities` |
| `lakehouse_code/gold/<product>/dbt/models/sources.yml` | `- name: ad_groups` under the `silver` source tables |
| `lakehouse_code/pipelines/dagster/<product>.py` | `"ad_groups"` in the Silver inputs tuple and `("marketing_platform", "ad_groups")` in `bronze_inputs` |
| `lakehouse_code/gold/<product>/dbt/models/gold/<mart>.sql` | A read of `{{ source('silver', 'ad_groups') }}` in the mart that should use it; until a model reads it, the product stages the table but its Gold output ignores it |

A domain has exactly one Floe contract, `<domain>.yml`. Never add a second one.

## 5. Deploy

```bash
olf deploy --provider local --phase artifacts
```

The artifacts phase reconciles the catalog namespaces, regenerates the Floe
manifests, rebuilds the project-code image and restarts the Dagster code
location. Never edit the generated manifests by hand.

## Check it worked

```bash
olf check contracts
```

It validates the descriptors. Then run the product's job in Dagster, and query
the new Silver table:

```bash
olf query "select * from marketing_silver.ad_groups"
```

It prints the rows of `ad_groups.csv`.
