# Deploy to Azure

How to run your project on Azure: AKS for Kubernetes and ACR for images, with
the in-cluster platform services (SeaweedFS, Polaris, PostgreSQL) kept inside
the cluster. Azure is a preview. The `poc` in cluster names refers to the
sandbox used for validation, not to a reduced platform.

Your `lakehouse_code/` does not change between providers; only the Deployment
Profile and the provider do. You need an initialized project, `docker` and
`python3` (3.12 or newer) on your `PATH`, and permission to create an AKS
cluster and an ACR registry in a resource group. `olf` provisions its own
Terraform, Helm and kubectl, and keeps Terraform state locally, so run every
command from the same machine and project.

> [!WARNING]
> This creates billable Azure resources. It is a proof of concept, not a
> production-hardened platform. Run the teardown step when you are done.

## 1. Point the profile at Azure

In `openlakeforge.yaml`, set the provider type, keep only the DEV stage, remove
the local-only plain-HTTP setting, and add an identity block. Do not set
`region`: Azure takes its region from `location` in your tfvars file.

```yaml
spec:
  provider:
    type: azure
  stages:
    dev:
      enabled: true
    prod:
      enabled: false
  identity:
    issuer: external
    issuer_url: https://login.example.com/
    role_claim: groups
    role_mapping:
      platform-admin: [olf-admins]
```

A profile without `spec.identity` is accepted only for `provider: local`, so
keep the block. The Azure deployment does not use it for sign-in yet: its
services are reached only through `olf forward`. Delete any `access:` block:
`issuer: none` is for `provider: local` only.

```bash
olf profile validate --project .
```

## 2. Authenticate

```bash
olf auth login --provider azure
```

Azure Identity opens Microsoft Entra's hosted sign-in page, then prompts for a
subscription. On a headless machine add `--device-code`. An existing `az login`
session is reused when `az` is present, but the CLI is optional.

If your network intercepts TLS, point Azure's SDK at your CA bundle:
`export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem`.

## 3. Configure the resource group

Azure resource-group settings live in a local tfvars file so temporary sandbox
names are not committed. Copy the template:

```bash
cp "$(olf distribution path)/infra/terraform/foundations/azure-aks/sandbox.tfvars.example" sandbox.tfvars
export AZURE_TFVARS_FILE="$(pwd)/sandbox.tfvars"
```

Edit `sandbox.tfvars`: set `resource_group_name` and `location` to your sandbox
resource group and its region, and `node_vm_size` to an AKS VM size that the
subscription and region permit. Keep `create_resource_group = false` when your
company sandbox supplies the group. Set it to `true` when Terraform should
create it; removing `resource_group_name` then uses the default
`rg-openlakeforge-azure-poc`.

Other overrides are optional environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AZURE_TFVARS_FILE` | `sandbox.tfvars` in the foundation directory | Path to your tfvars |
| `AZURE_CLUSTER_NAME` | `aks-openlakeforge-poc` | AKS cluster name |
| `AZURE_NODE_COUNT` | `3` | Node count |
| `AZURE_ACR_NAME_PREFIX` | `openlakeforgepoc` | ACR name prefix; the registry name is globally unique |

The kubeconfig lives at `~/.openlakeforge/state/azure/kubeconfig.yaml` for an
installed `olf`; `--kubeconfig-path` on the commands moves it.

The cloud pushes your project image to the ACR registry it creates, so clear
local registry settings if this shell still has them:

```bash
unset PROJECT_CODE_IMAGE_REPOSITORY PROJECT_CODE_IMAGE_TAG
```

## 4. Deploy

```bash
olf deploy --provider azure
```

It runs the foundation (AKS, ACR, resource group resources), the platform and
the artifacts phases in order. To run them one at a time, add
`--phase foundation`, `--phase platform` or `--phase artifacts`. The cloud has no
public ingress or DNS: reach the services by port-forwarding them, and leave
this running in a second terminal:

```bash
olf forward --provider azure
```

## 5. Validate

With `olf forward` running, from the first terminal:

```bash
olf e2e run --env azure
```

It launches every product's job and verifies the Silver and Gold tables. When
the profile enables analytics and governance (the Full preset), it also checks
the dashboards and OpenMetadata assets. It ends with a line of the form
`<Env> OpenLakeForge <suite> e2e validation passed.`

## 6. Tear down

```bash
olf destroy --provider azure
```

This removes the platform, then the foundation (AKS, ACR and the resource group
resources). To go one phase at a time, run `--phase platform` first.
