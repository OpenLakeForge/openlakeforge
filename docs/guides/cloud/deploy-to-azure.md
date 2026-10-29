# Deploy to Azure

How to run your project in your own Azure subscription: AKS for Kubernetes and
ACR for images, with the same in-cluster storage (SeaweedFS), catalog
(Polaris) and PostgreSQL as the local platform. Your `lakehouse_code/` does
not change; only the Deployment Profile does.

> [!WARNING]
> Azure is a preview. It runs the same platform modules as local, but its
> latest changes are checked with `terraform validate` and `plan` only, not
> yet deployed to a live subscription. It creates billable resources. Read
> [Limitations and costs](#limitations-and-costs) first, and tear it down when
> you are done.

## Prerequisites

- An initialized project (`olf init`) and `olf` installed as in
  [Install and configure a local deployment](../local-installation.md).
- A running Docker engine: the deploy builds and pushes images.
- An Azure identity allowed to create a resource group, an AKS cluster, an
  ACR registry and a role assignment in the subscription. `Owner` on the
  subscription is the simplest choice; `Contributor` cannot create the role
  assignment that lets AKS pull from ACR.

You do not need the Azure CLI, Terraform, Helm or kubectl: `olf` uses the
Azure SDK and its own pinned tools.

## 1. Authenticate

`olf` reads credentials through the Azure SDK, from the first source it finds:

| Source | How |
| --- | --- |
| A service principal or workload identity | `ARM_CLIENT_ID`, `ARM_TENANT_ID` and `ARM_CLIENT_SECRET` (or a certificate or OIDC token), the `AZURE_*` equivalents, or a managed identity |
| An existing `az login` session | `olf auth login --provider azure` reuses it when the `az` CLI is installed |
| Browser sign-in | `olf auth login --provider azure`, then pick the subscription |

On a machine without a browser, add `--device-code`. `--tenant-id` and
`--subscription-id` skip the prompts. Check which identity `olf` will use:

```bash
olf auth status --provider azure
```

## 2. Point the profile at Azure

Edit `openlakeforge.yaml`:

```yaml
spec:
  provider:
    type: azure
  stages:
    dev:
      enabled: true
```

- Do not set `region`: Azure takes its region from `location` in the variables
  file below.
- Delete any `access:` block: the cloud publishes no routes, and an `access:`
  block would require `spec.identity`.
- Leave out `spec.identity`. The cloud deployment publishes no routes, so
  there is nothing to sign in to: you reach the services with `olf forward`
  from your machine, and authentication stays off.
- Keep DEV as the only stage: the cloud guides are validated with one stage.

```bash
olf profile validate --project .
```

## 3. Configure the resource group and nodes

Azure needs a Terraform variables file. Create one outside git and export its
path:

```bash
cat > azure.tfvars <<'HCL'
create_resource_group = true
resource_group_name   = "rg-openlakeforge"
location              = "westeurope"
node_vm_size          = "Standard_D4s_v5"
HCL
export AZURE_TFVARS_FILE="$(pwd)/azure.tfvars"
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `node_vm_size` | yes | A VM size your subscription and region offer |
| `location` | no, default `westeurope` | The Azure region |
| `create_resource_group` | no, default `true` | `true` lets Terraform create and later delete the group; `false` deploys into an existing group you manage |
| `resource_group_name` | no, default `rg-openlakeforge-azure-poc` | The group to create, or the existing one to use |

Keep `AZURE_TFVARS_FILE` exported for every later command, including
`olf destroy`: the foundation cannot be read without it.

Other settings are environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AZURE_CLUSTER_NAME` | `aks-openlakeforge-poc` | AKS cluster name |
| `AZURE_NODE_COUNT` | `3` | Node count |
| `AZURE_ACR_NAME_PREFIX` | `openlakeforgepoc` | ACR name prefix; a random suffix makes it globally unique |

## 4. Deploy

```bash
olf deploy --provider azure
```

It runs three phases, which you can also run one at a time with `--phase`:

| Phase | Creates |
| --- | --- |
| `foundation` | The resource group (when `create_resource_group = true`), AKS, ACR and the AKS-to-ACR pull role |
| `platform` | Deploys SeaweedFS, Polaris, PostgreSQL, Trino and Dagster; Superset and OpenMetadata with the Full preset |
| `artifacts` | Builds and pushes the project-code image to ACR and deploys your data products |

If this shell still exports `PROJECT_CODE_IMAGE_REPOSITORY` from a local
promotion, unset it: the cloud pushes to its own ACR.

## 5. Open and validate the platform

The cloud deployment has no public endpoint. Port-forward the services to
your machine, on the same ports as locally (Dagster on
`http://localhost:3000`, Trino on `8080`, Polaris on `8181`, SeaweedFS S3 on
`9000`, and on Full Superset on `8088` and OpenMetadata on `8585`), and stop
with Ctrl-C:

```bash
olf forward --provider azure
```

Run the end-to-end suite after you stop `olf forward`: `olf` runs one command
at a time, and the suite opens its own port-forwards.

```bash
olf e2e run --env azure
```

It launches every product's job and checks the Silver and Gold tables, and on
Full also the dashboards and OpenMetadata. It ends with
`... e2e validation passed.`

## 6. Tear down

```bash
olf destroy --provider azure
```

It removes the platform, then AKS and ACR. With
`create_resource_group = true` the resource group goes too; with `false` it is
left in place. Run it from the same machine and project as the deploy, with the
same `AZURE_TFVARS_FILE`. With `--phase`, destroy `platform` before
`foundation`.

## Limitations and costs

| Area | Today |
| --- | --- |
| Access | No ingress, DNS, TLS or sign-in. Services are reached with `olf forward` only, by whoever has credentials to the cluster, with no OpenLakeForge sign-in in front of them. |
| Stages | Validated with DEV only. |
| Services | SeaweedFS, Polaris and PostgreSQL run inside the cluster on its disks, with no backups or high availability. Azure Blob Storage and managed PostgreSQL are not used. |
| State | Terraform state stays on your machine under `~/.openlakeforge`. There is no remote backend. |
| Secrets | Generated service passwords live in Kubernetes Secrets and in the local Terraform state. |

The main running costs are the AKS nodes (three of `node_vm_size`), their
disks and ACR, plus data transfer. They accrue until `olf destroy` finishes.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Azure is not authenticated` | Run `olf auth login --provider azure`, then `olf auth status --provider azure`. |
| `Azure foundation configuration not found` | Export `AZURE_TFVARS_FILE` pointing at your variables file. |
| A VM size or quota error | Pick a `node_vm_size` your region offers and your subscription has quota for. |
| A policy forbids creating resource groups | Ask for a group, then set `create_resource_group = false` and its `resource_group_name` and `location`. |
| `certificate verify failed` from the SDK | A proxy intercepts TLS: `export REQUESTS_CA_BUNDLE=/path/to/your-ca-bundle.pem`. |
