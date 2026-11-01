# Quickstart

Deploy OpenLakeForge on your machine, run an example data product from
Bronze to Gold, and check the result. You need Docker and about the time of
one first deploy, which takes several minutes while images download.

The local platform runs in a [kind](https://kind.sigs.k8s.io/) cluster on your
Docker engine. It does not touch your own `~/.kube/config`.

## Before you start

| You need | Check |
| --- | --- |
| A running Docker engine | `docker ps` succeeds |
| [uv](https://docs.astral.sh/uv/) | `uv --version` |
| Ports 80 and 443 free on `127.0.0.1` | Nothing else serves `http://localhost` |
| 4 CPUs and 16 GB of memory available to Docker | `docker info` shows `CPUs` and `Total Memory` |
| Free disk for Docker's images and volumes | 100 GB covers the Full preset with two stages ([details](../guides/local-installation.md#hardware)) |

Terraform, Helm, kubectl and kind are not prerequisites: `olf` provisions
pinned copies under `~/.openlakeforge`.

## 1. Install `olf` and create a project

```bash
uv tool install "openlakeforge==0.5.1a1" --python 3.12

mkdir my-lakehouse && cd my-lakehouse
olf init
```

`olf init` writes two things you own:

```text
my-lakehouse/
├── openlakeforge.yaml   # Deployment Profile: provider, stages, preset, access
└── lakehouse_code/      # sources, contracts, dbt projects, Dagster jobs
```

The profile is a single DEV stage, the Slim preset, plain HTTP and no
sign-in. `lakehouse_code/` holds three example data products:
`sales/order_revenue`, `sales/customer_health` and
`supply_chain/inventory_reliability`.

## 2. Deploy

```bash
olf infra deploy --provider local
```

`olf infra deploy` creates the kind cluster, deploys the platform services, builds
the project-code image from `lakehouse_code/`, and registers the data products
with Dagster.

## 3. Open the portal

```bash
olf infra status --provider local
```

Its `URLs` section lists every service. Open the portal:

```text
http://olf.localhost
```

It links to each service the profile enables. Browsers resolve every
`*.localhost` name to your machine, so no hosts-file entry is needed. There is
no sign-in: authentication stays off until you add `spec.identity`.

> [!NOTE]
> A profile without `spec.identity` publishes nothing beyond your machine:
> locally, only on `*.localhost`. With the Full preset and sign-in off, Superset opens as Admin
> and OpenMetadata keeps its `admin@open-metadata.org` login, with the password
> in Secret `openmetadata-admin`, as `olf infra status` notes. To serve HTTPS, see
> [Serve HTTPS](../guides/local-installation.md#serve-https); to sign in with
> GitHub, see [Connect GitHub](../guides/identity/connect-github.md).

## 4. Run a data product

Open Dagster from the portal, or directly:

```text
http://dagster-dev.olf.localhost
```

Open **Jobs**, select `order_revenue_pipeline` and launch it. The run loads the
CRM CSV files into Bronze with dlt, validates them into Silver Iceberg tables
with Floe, and builds four Gold marts with dbt-trino, such as
`mart_order_revenue_by_day`. The asset graph shows each step as it
materializes.

## 5. Check it worked

```bash
olf infra e2e run --env local --suite full
```

The suite launches every example product's job and checks that each Silver
and Gold table exists, then queries every Gold mart through Trino and expects
rows. It ends without an error when the platform works end to end.

To see the Gold rows yourself, query them through Trino:

```bash
olf infra query "SELECT * FROM order_revenue_gold.mart_order_revenue_by_day LIMIT 10"
```

## Tear down

```bash
olf infra destroy --provider local
```

## Next

- [Tutorial: Discover OpenLakeForge](../tutorial/01-tour-the-platform.md):
  tour the platform, then add a source, a contract and a Gold model of your
  own.
- [Concepts](concepts.md): presets, stages, project revisions and roles.
- [Install and configure a local deployment](../guides/local-installation.md):
  pip installs, presets, HTTPS and teardown.
- Deploy for real: [AWS](../guides/cloud/deploy-to-aws.md) or
  [Azure](../guides/cloud/deploy-to-azure.md).
