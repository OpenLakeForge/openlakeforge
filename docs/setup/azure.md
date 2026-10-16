# Deploy to Azure (AKS)

The Azure environment runs the same OpenLakeForge platform on **AKS and ACR**,
keeping the in-cluster platform services (SeaweedFS, Polaris, PostgreSQL). The
`poc` name refers to the sandbox used for validation, not to a reduced
platform. See [the Azure AKS POC architecture](../architecture/azure-aks-poc.md)
for what runs where.

Prerequisites, local Terraform state, and what stays out of git are shared with
AWS: see [Cloud POC setup](cloud-poc-setup.md#prerequisites).

Azure resource-group settings live in a local **tfvars file** so temporary
sandbox names are not committed to the repository.

## 1. Authenticate

```bash
olf auth login --provider azure
```

Azure Identity opens Microsoft Entra's hosted sign-in page, then prompts for a
subscription. On a headless machine use `olf auth login --provider azure
--device-code`. An existing `az login` session is reused when `az` is present,
but the CLI is optional.

If your network intercepts TLS, point Azure's SDK (its `requests` transport)
at your CA bundle: `export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem`.

## 2. Configure the resource group

Copy the tracked template and set the name and region of your sandbox resource
group, plus an AKS VM size permitted by that subscription and region. Keep
`create_resource_group = false` when the group is supplied by your company
sandbox. When Terraform should create the group, set it to `true`; removing
`resource_group_name` then uses the default `rg-openlakeforge-azure-poc`.

In a checkout, the template is a file on disk. Installed from PyPI, it is
part of the read-only payload `olf distribution path` prints, so the same
commands work either way:

```bash
cp "$(olf distribution path)/infra/terraform/foundations/azure-aks/sandbox.tfvars.example" sandbox.tfvars
export AZURE_TFVARS_FILE="$(pwd)/sandbox.tfvars"
```

## 3. Configure optional runtime overrides

All have defaults; override via environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AZURE_TFVARS_FILE` | `<foundation-dir>/sandbox.tfvars` | Path to your resource-group tfvars |
| `AZURE_CLUSTER_NAME` | `aks-openlakeforge-poc` | AKS cluster name |
| `AZURE_NODE_COUNT` | `3` | Node count |
| `AZURE_ACR_NAME_PREFIX` | `openlakeforgepoc` | ACR name prefix (globally unique) |
| `--kubeconfig-path` (CLI flag) | `.tmp/kubeconfigs/azure.yaml` in a checkout, `~/.openlakeforge/state/azure/kubeconfig.yaml` installed | Isolated AKS kubeconfig; not an environment variable |

## 4. Deploy, validate, tear down

```bash
olf deploy --provider azure --phase foundation
olf deploy --provider azure               # foundation + platform + artifacts
olf forward --provider azure
# ...
olf destroy --provider azure --phase platform     # platform services only
olf destroy --provider azure --phase foundation   # AKS, ACR, and resource group resources
olf destroy --provider azure                      # full teardown: platform, then foundation
```

After the deploy, validate it from a second terminal while `olf forward` runs:

```bash
olf e2e run --env azure
```
