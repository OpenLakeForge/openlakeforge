# Azure AKS POC

The first Azure target is an AKS-based proof of concept, not an Azure-native
platform rewrite. It keeps the existing in-cluster OpenLakeForge services:
SeaweedFS, Polaris, PostgreSQL, Trino, Dagster, OpenMetadata, and Superset.
Only the local-kind mechanics are replaced:

- AKS provides Kubernetes.
- ACR distributes the Superset and project-code images.
- Access remains `kubectl port-forward`; there is no public ingress, DNS, TLS,
  Keycloak, or Vault.

This POC is not production hardened. It still uses in-cluster storage,
databases, generated Terraform state credentials, and Kubernetes Secrets. Future
Azure phases can replace those pieces with Azure Blob or ADLS Gen2, Azure
PostgreSQL Flexible Server, Key Vault with External Secrets, managed identity,
private networking, and managed observability.

## Prerequisites

The shell running the Azure workflow needs:

- Azure authentication established with `olf auth login --provider azure`.
- The intended subscription selected during that login.
- Permissions to create resource groups, AKS clusters, ACR registries, and role
  assignments.
- Terraform, Helm, kubectl, Docker, and Python 3.
- Docker access to build local images before pushing to ACR.

Foundation configuration:

- Resource group name, ownership, region, and permitted node VM size: local `sandbox.tfvars`
- Default Terraform-owned resource group: `rg-openlakeforge-azure-poc`
- AKS cluster: `aks-openlakeforge-poc`
- Node pool size: 3 nodes
- ACR name: `openlakeforgepoc<random_suffix>`

Copy the tracked template before the first deployment. The resulting file is
gitignored so a temporary company sandbox resource-group name stays local:

```bash
cd infra/terraform/foundations/azure-aks
cp sandbox.tfvars.example sandbox.tfvars
# Edit resource_group_name, create_resource_group, location, and node_vm_size.
cd ../../../..
olf deploy --provider azure --var-file infra/terraform/foundations/azure-aks/sandbox.tfvars
```

Some corporate sandboxes only allow resource creation inside a pre-created
resource group. In that restricted case, keep the resource group outside the
foundation state and set `create_resource_group = false` in `sandbox.tfvars`.
Terraform will read the existing resource group and still create the POC-owned
resources inside it: AKS, ACR, and the AKS-to-ACR pull role assignment.

The same tfvars file is used by `olf deploy`, `olf destroy`, and
`olf destroy --phase foundation`. With `create_resource_group = false`, foundation
destroy removes the AKS and ACR resources owned by this stack but leaves the
external resource group in place.

## Workflow

Create AKS and ACR:

```bash
olf deploy --provider azure --phase foundation
```

Deploy the platform:

```bash
olf deploy --provider azure
```

`olf deploy --provider azure` runs three phases:

1. `--phase foundation` creates or refreshes the AKS and ACR foundation.
2. `--phase platform` builds and pushes the Superset image to ACR, then runs
   Terraform in `infra/terraform/environments/azure-poc`. Superset is pushed
   before Terraform because the Helm release waits for Superset pods.
3. `--phase artifacts` generates Floe manifests, builds and pushes the
   project-code image to ACR, uploads Floe manifests through the SeaweedFS
   S3-compatible API via port-forward, imports Superset report bundles, deploys
   OpenMetadata metadata, and restarts Dagster deployments.

Runtime images are pushed as:

```text
<acr_login_server>/openlakeforge/project-code:<tag>
<acr_login_server>/openlakeforge/superset:<tag>
```

The default tag is `azure-<git_sha>`, falling back to a UTC timestamp when the
repository is unavailable.

Forward service UIs to localhost:

```bash
olf forward --provider azure
```

Ports match the local stack:

- Dagster: `http://localhost:3000`
- Superset: `http://localhost:8088`
- OpenMetadata: `http://localhost:8585`
- Trino: `http://localhost:8080`
- Polaris: `http://localhost:8181`
- SeaweedFS S3: `http://localhost:9000`

## Validation

Static checks include the Azure roots and contracts:

```bash
olf check structure
olf check contracts
olf check infra
```

After `olf deploy --provider azure`, run:

```bash
olf e2e run --env azure
```

`olf e2e run --env azure` defaults to the full suite. The e2e check:

- Confirms pods are `Running` or completed bootstrap pods are `Succeeded`.
- Launches and polls these Dagster jobs to `SUCCESS`:
  `order_revenue_pipeline`, `customer_health_pipeline`, and
  `inventory_reliability_pipeline`.
- Confirms Trino exposes the `iceberg` catalog.
- Confirms Silver has 15 tables and Gold has 9 marts.
- Confirms each Gold mart has at least one row.
- Confirms the three Superset dashboard slugs are imported.
- Confirms OpenMetadata contains the `sales` and `supply_chain` domains and the
  three data products.
- Confirms the ops artifact bucket contains product manifests, Floe reports,
  dbt run artifacts, Dagster compute logs, and a fresh Kubernetes log archive.

## Teardown

Destroy only the platform:

```bash
olf destroy --provider azure --phase platform
```

Then destroy AKS, ACR, and the resource group resources:

```bash
olf destroy --provider azure --phase foundation
```

For a full teardown, run:

```bash
olf destroy --provider azure
```

The foundation destroy phase refuses to run while the `lakehouse` namespace
exists unless `--force` is passed.
