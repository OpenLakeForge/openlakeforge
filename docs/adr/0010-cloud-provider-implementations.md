# ADR 0010: Cloud provider implementations (AWS, Azure)

| | |
| --- | --- |
| Status | Binding; both clouds are proof-of-concept maturity |
| Built | AWS ✔ (verified end to end against a live account) · Azure ✔ (`terraform validate`/`plan` only) |

## Context

Local and Azure use Polaris while AWS uses Glue. Shared DEV, optional UAT and
PROD stages run in one cluster (v0.3). Reusing one Glue catalog with the same
unqualified database names would collide across stages or force a
provider-specific SQL naming scheme. A native Glue catalog per stage is
unavailable: the account's Glue service rejects `CreateCatalog`
(`InvalidInputException: Create glue native catalog is not supported`), an
account- and region-level restriction confirmed directly against the account,
not a provisioning bug. AWS is to move to Polaris (#681, ADR 0019), which
makes the Glue design below transitional.

## Decision

**D1. Local and Azure keep a shared Polaris service with one physical catalog
per enabled stage.** Each is a real, separate catalog, so owner/layer
namespaces (for example `sales_silver`) need no further qualification.
Polaris uses its own persistent PostgreSQL role and database: catalog state
must survive a pod restart, and an in-memory metastore would lose catalogs,
namespaces, principals and tables. Polaris credentials reach workloads
through Kubernetes Secret references, never Terraform outputs or rendered
Helm values.

**D2. AWS provisions every stage against the account's one default Glue
catalog, separated by a namespace prefix.** The catalog ID is the bare
12-digit account ID with no suffix, and no `aws_glue_catalog` resource is
created. The logical SQL alias is still `lakehouse_<stage>` for every
provider, but on AWS it names a set of prefixed physical databases inside the
shared catalog: every physical Glue database/schema name is prefixed with
`<catalog_name>_` (for example `lakehouse_dev_sales_silver` instead of
`sales_silver`). `lakehouse_dev.sales_silver.orders` and
`lakehouse_prod.sales_silver.orders` therefore select the same Glue catalog
ID and differ only in which prefixed database Trino resolves `sales_silver`
to. Enforced by: `namespace_prefix` in `resolve_physical_names`
(`packages/core/openlakeforge_core/inventory.py`).

**D3. On AWS, stage isolation is the namespace prefix, enforced in two
layers.** IAM scopes each stage's Pod Identity role to `database/<prefix>_*`
and `table/<prefix>_*/*` Glue ARNs (with the bare `catalog` resource, since
the shared catalog cannot be scoped per stage). Trino's file-based access
control adds a `tables` rule granting a stage's identity full privileges only
on `<prefix>_.*` schemas and denying everything else; the `catalogs` rule
alone only gates which catalog a client may use, and every stage's schemas
are physically visible from every Glue stage's catalog. Enforced by:
`infra/terraform/environments/aws-poc/main.tf` (IAM) and
`infra/terraform/modules/query/trino/main.tf` (`tables`).

**D4. AWS stage contracts carry no credentials, and runtime access is
stage-scoped.** A contract carries the Glue region, the catalog ID (the bare
account ID, shared across stages) and the workload-identity reference. A
stage runtime receives only its storage, catalog alias, activation prefix and
identity reference.

**D5. AWS workload identity is EKS Pod Identity, not IRSA.** The validation
environment cannot create the IAM OIDC provider IRSA needs. The contract
stays `identity.aws_pod_identity`, and AWS storage and catalog authentication
use the stage's workload-identity reference rather than static keys. Azure
keeps AKS OIDC readiness; local keeps development-only Kubernetes Secrets.

**D6. AWS replaces contract implementations; Azure relocates hosting.** AWS
uses EKS, S3 for Bronze/Silver/Gold/ops, RDS PostgreSQL, ECR and Pod Identity
behind the existing capability interfaces. Azure uses AKS and ACR and keeps
SeaweedFS, Polaris and in-cluster PostgreSQL. Trino stays the query path;
Athena is deferred because it would change query cost, Superset
connectivity, validation and runtime contracts.

**D7. The cloud roots stay at proof-of-concept scope.** Account-specific
tags, ownership, region and cluster naming stay in ignored `sandbox.tfvars`
files or environment variables. The roots keep local Terraform state,
Kubernetes Secrets, port-forward access, no ingress/TLS, no external secret
manager and no Lake Formation. Secure state, private networking, least
privilege and external secrets belong to the secure beta profile.

## Consequences

- No new Iceberg catalog technology: Polaris for local and Azure, Glue for
  AWS.
- Physical Glue and S3 names stay out of Deployment Profiles and
  `lakehouse_code` descriptors, but every consumer that resolves a physical
  Glue database/schema name (Floe's per-domain profile rendering, `olf infra
  catalog sync-namespaces`, dbt profile and source schema config) must apply
  the same `namespace_prefix`, or it silently resolves to the wrong stage's
  database, or IAM denies.
- External secrets, ingress, network policy and live cross-stage isolation
  probing (proven for local) are not wired for AWS or Azure.

## Boundaries

| Not decided here | Where |
| --- | --- |
| The provider contracts the clouds implement | ADR 0003, `docs/architecture/provider-contracts.md` |
| Ingress and TLS on the local root | ADR 0013 |
| AWS moving from Glue to Polaris | ADR 0019 (#681) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| A native Glue catalog per stage | The account's Glue service rejects `CreateCatalog` |
| One Glue catalog with unqualified database names | Stages collide, or SQL needs a provider-specific naming scheme |

## History

- 2026-08-28: Rewritten for v3 stages: a custom Glue catalog per stage
  replaced the single AWS Glue catalog mapping.
- 2026-08-31 (#114): D2 and D3 replace the catalog-per-stage design, which
  the account's Glue service does not support.
- 2026-11-22 (#1384): Rewritten in the standard form; no decision changed.
