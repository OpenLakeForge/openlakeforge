# Terraform

Terraform will own environment assembly and reusable platform modules.

Implemented structure:

```text
infra/terraform/
├── foundations/
│   ├── azure-aks/
│   ├── aws-eks/
│   └── local-kind/
├── environments/
│   ├── azure-poc/
│   ├── aws-poc/
│   └── local/
└── modules/
    ├── storage/aws-s3/
    ├── storage/rds-postgresql/
    ├── storage/seaweedfs/
    ├── storage/postgresql/
    ├── catalog/aws-glue/
    ├── catalog/polaris/
    ├── query/trino/
    ├── analytics/superset/
    ├── orchestration/dagster/
    └── governance/openmetadata/
```

The local foundation root creates the kind cluster. The Azure foundation root
creates the AKS cluster and ACR registry. The AWS foundation root creates the
VPC, EKS cluster, node group, ECR registries, and EKS Pod Identity readiness.
Environment roots then deploy OpenLakeForge into the selected cluster through
the Kubernetes and Helm providers. Static, non-secret Helm chart values live in
`../helm/values/local`; Terraform modules overlay the dynamic contract values
and Secret references. Azure reuses the in-cluster service implementations while
AWS swaps storage, metadata PostgreSQL, and catalog dependencies for managed
services.

Each environment root normalizes its provider contracts in `contracts.tf`.
Those typed contract objects are the source of truth for storage, catalog,
metadata database, artifacts, secrets, identity, access, observability, query,
orchestration, reporting, and governance boundaries.

## Local workflow

```bash
uv run --project tools/olf --locked olf deploy --provider local --phase foundation
uv run --project tools/olf --locked olf deploy --provider local
uv run --project tools/olf --locked olf destroy --provider local
uv run --project tools/olf --locked olf destroy --provider local --phase foundation
```

`olf deploy --provider local --phase foundation` runs `terraform init` and
`terraform apply` in `infra/terraform/foundations/local-kind`. Terraform owns
the local kind cluster lifecycle while the cluster definition remains in
`infra/kind/local`.

`olf deploy --provider local` runs two platform phases:

```bash
uv run --project tools/olf --locked olf deploy --provider local --phase platform
uv run --project tools/olf --locked olf deploy --provider local --phase artifacts
```

`olf deploy --provider local --phase platform` runs `terraform init` and a
normal `terraform apply` in `infra/terraform/environments/local`. Terraform
owns:

- Kubernetes namespace creation
- SeaweedFS, Polaris, Trino, and Superset Helm releases
- Dagster Helm release
- dynamic Helm values passed to those releases
- local generated credentials
- Kubernetes Secrets used as service contracts
- SeaweedFS bucket creation jobs
- Polaris catalog and Trino principal bootstrap jobs
- Polaris Floe principal bootstrap credentials for manifest-driven Floe jobs
- shared local PostgreSQL for Dagster, OpenMetadata, and Superset metadata
- Dagster webserver, daemon, domain product code servers, and Kubernetes run launcher
- Superset webserver and worker
- OpenMetadata, Polaris service metadata, and catalog ingestion plumbing
- SeaweedFS S3, Filer, and Master services for local object storage and inspection

`olf deploy --provider local --phase artifacts` owns the local/CD artifacts:

- project-code image build/load
- domain Floe manifest generation and upload to the local ops bucket
- product Superset report import
- OpenMetadata domain, data-product, Bronze, Silver, and Gold metadata from domain YAML files
- Dagster rollout after dynamic artifacts are available

Terraform state is local and contains generated development credentials. Treat
state files as sensitive; they are gitignored.

No remote state backend, Keycloak, Vault, ingress/TLS, Lake Formation, or cloud
secret manager integration is implemented yet.

## Azure AKS POC workflow

```bash
uv run --project tools/olf --locked olf deploy --provider azure --phase foundation
uv run --project tools/olf --locked olf deploy --provider azure
uv run --project tools/olf --locked olf e2e run --env azure
uv run --project tools/olf --locked olf destroy --provider azure
uv run --project tools/olf --locked olf destroy --provider azure --phase foundation
```

`olf deploy --provider azure --phase foundation` runs Terraform in
`infra/terraform/foundations/azure-aks` to create the AKS cluster, ACR registry,
AKS-to-ACR `AcrPull` role assignment, and AKS OIDC / Workload Identity
readiness. The wrapper then runs `az aks get-credentials`.

Resource-group settings are required in a local, gitignored tfvars file. Copy
the template and configure the group supplied by your sandbox:

```bash
cd infra/terraform/foundations/azure-aks
cp sandbox.tfvars.example sandbox.tfvars
# Edit resource_group_name, create_resource_group, location, and node_vm_size.
cd ../../../..
uv run --project tools/olf --locked olf deploy --provider azure --phase foundation
```

With `create_resource_group = false`, Terraform reads the resource group as
data, creates AKS and ACR inside it, and leaves the group untouched during
foundation destroy. Set `AZURE_TFVARS_FILE` to use a file outside the default
foundation directory. With `create_resource_group = true`, Terraform creates
the group and uses `rg-openlakeforge-azure-poc` when `resource_group_name` is
omitted.

`olf deploy --provider azure` runs:

```bash
uv run --project tools/olf --locked olf deploy --provider azure --phase platform
uv run --project tools/olf --locked olf deploy --provider azure --phase artifacts
```

`olf deploy --provider azure --phase platform` builds and pushes the custom
Superset image to ACR before Terraform apply because the Superset Helm release
waits for pods during install.
`olf deploy --provider azure --phase artifacts` generates Floe manifests, builds and pushes the
project-code image, uploads manifests to the in-cluster SeaweedFS ops bucket,
imports Superset reports, deploys OpenMetadata metadata, and restarts Dagster.

The Azure POC keeps SeaweedFS, PostgreSQL, Polaris, and Kubernetes Secrets
in-cluster. Azure Blob/ADLS, Azure PostgreSQL Flexible Server, Key Vault,
managed identity-backed runtime auth, ingress, DNS, and TLS are future phases,
not active POC implementations.

## AWS EKS POC workflow

```bash
uv run --project tools/olf --locked olf deploy --provider aws --phase foundation
uv run --project tools/olf --locked olf deploy --provider aws
uv run --project tools/olf --locked olf e2e run --env aws
uv run --project tools/olf --locked olf destroy --provider aws
uv run --project tools/olf --locked olf destroy --provider aws --phase foundation
```

`olf deploy --provider aws --phase foundation` runs Terraform in
`infra/terraform/foundations/aws-eks` to create the VPC, EKS cluster, managed
node group, EKS add-ons, ECR repositories, and EKS Pod Identity add-on/roles.
The wrapper then runs `aws eks update-kubeconfig`.

`olf deploy --provider aws` runs:

```bash
uv run --project tools/olf --locked olf deploy --provider aws --phase platform
uv run --project tools/olf --locked olf deploy --provider aws --phase artifacts
```

`olf deploy --provider aws --phase platform` builds and pushes the custom
Superset image to ECR before Terraform apply. The AWS platform root creates S3
medallion and ops buckets, RDS PostgreSQL, product-layer Glue
databases/namespaces, Pod Identity workload access, and the shared Helm
services on EKS.

`olf deploy --provider aws --phase artifacts` generates Floe manifests, builds and pushes the
project-code image, uploads manifests directly to the S3 ops bucket, imports
Superset reports, deploys OpenMetadata metadata, patches Dagster images, and
restarts Dagster workloads.

The AWS POC keeps Trino as the query path. Athena, Secrets Manager, External
Secrets, Lake Formation, ingress, DNS, TLS, and remote state are future
adapters.
