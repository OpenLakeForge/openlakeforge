# Ownership identities

How the names in `lakehouse_code/` line up, and which platform names
OpenLakeForge derives from them. Read it before you add a source, domain or
product, so each piece lands where `olf` looks for it.

Ownership follows the medallion layer: a source owns Bronze, a domain owns
Silver and a product owns Gold. `lakehouse_code/lakehouse.yaml` is the central
inventory: it declares the sources, domains, products and dashboards, and
`olf` uses it to decide what to deploy. One exception: Dagster loads every
module under `lakehouse_code/pipelines/dagster/`, declared or not, so delete a
product's module when you remove the product.

## Keep these names aligned

```text
lakehouse.yaml sources entry
        └── bronze/<source>/source.yaml (name) and its directory

lakehouse.yaml domain name
        └── silver/<domain>/contracts/floe/<domain>.yml

lakehouse.yaml product id
        ├── gold/<product>/dbt/            the dbt project directory
        └── pipelines/dagster/<product>.py the Dagster module
```

| Identity | Where it must match |
| --- | --- |
| Source name | The directory `lakehouse_code/bronze/<source>/` and the `name` in its `source.yaml` |
| Domain name | The directory `lakehouse_code/silver/<domain>/` and its one Floe contract `<domain>.yml` |
| Product id | The directory `lakehouse_code/gold/<product>/` and the Dagster module `<product>.py` |
| Dashboard name | The bundle directory `lakehouse_code/dashboards/superset/<dashboard>/` and its `dashboards:` entry |

Every identifier matches `^[a-z][a-z0-9_]*$`: lowercase letters, digits and
underscores, starting with a letter. A product id is unique across the whole
lakehouse. A domain has exactly one Floe contract; never add a
product-specific one under it. `olf source new`, `olf domain new` and
`olf product new` create these pieces already aligned. `olf check contracts`
rejects a descriptor with an invalid identifier or a duplicate product. It
reads each descriptor on its own, so a Silver table naming a source that does
not exist passes it; `olf check project-code` loads the whole inventory and
catches that.

## What OpenLakeForge derives

For the source `marketing_platform`, the domain `marketing` and the product
`campaign_performance`:

| Derived name | Value | Owner |
| --- | --- | --- |
| Bronze namespace | `marketing_platform_bronze` | The source |
| Silver namespace | `marketing_silver` | The domain, shared by every `marketing` product |
| Gold namespace | `campaign_performance_gold` | The product |
| Dagster job | `campaign_performance_pipeline` | The product |
| Dagster module | `lakehouse_code.pipelines.dagster.campaign_performance` | The product |

You never create the namespaces and you never edit Terraform to register a
product. Provider contracts resolve these logical names into the storage and
catalog of the active provider, which is why the same project runs on local
Kubernetes, AWS and Azure.

## Descriptors stay provider-neutral

`lakehouse.yaml` and `source.yaml` never name Polaris, Glue, S3, SeaweedFS,
endpoints or Kubernetes resources. Physical names are derived from the logical
identities above, never written into the descriptors.

## Check it worked

```bash
olf check contracts
```

It prints one `[PASS]` line per check. An invalid or inconsistent identity in
the descriptors fails with the file and field named. It does not compare any
directory, module or file name with the identity it should carry (source,
contract or product), so keep those aligned as shown above.
