# ADR 0010: Cloud provider implementations (AWS, Azure)

## Status

Binding. Both are proof-of-concept maturity; secure identity, ingress, and
network policy remain outside the active profile.

## Context

Local and Azure use Polaris while AWS uses Glue. v0.3 adds shared DEV, optional
UAT, and PROD stages in one cluster. Reusing a default Glue catalog with the
same unqualified database names would collide across stages or force a
provider-specific SQL naming scheme.

A native Glue catalog per stage (this decision's original design) turned out
to be unavailable: this account's Glue service rejects `CreateCatalog`
(`InvalidInputException: Create glue native catalog is not supported`),
confirmed directly against the account, independent of any Terraform
configuration. That is an account/region-level restriction, not a
provisioning bug, so the decision below replaces the custom-catalog design
with a shared-catalog fallback instead.

## Decision

Local and Azure retain a shared Polaris service with one physical catalog per
enabled stage — a real, separate physical catalog, so their owner/layer
namespaces (e.g. `sales_silver`) need no further qualification.

AWS instead provisions every stage against the account's one default Glue
catalog (bare 12-digit account ID, no catalog-name suffix); no
`aws_glue_catalog` resource is created. The logical SQL alias is still
`lakehouse_<stage>` for every provider, but on AWS it names a set of
namespace-prefixed physical databases inside that one shared catalog rather
than a distinct catalog: `resolve_physical_names` prefixes every physical Glue
database/schema name with `<catalog_name>_` (e.g. `lakehouse_dev_sales_silver`
instead of bare `sales_silver`), keeping every stage's databases
collision-free in the shared catalog. `lakehouse_dev.sales_silver.orders` and
`lakehouse_prod.sales_silver.orders` therefore select the *same* Glue catalog
ID and differ only in which prefixed database Trino resolves the bare
`sales_silver` namespace to.

Isolation between stages moves from the catalog boundary to this
namespace_prefix, enforced in two layers:
- IAM scopes each stage's Pod Identity role to `database/<prefix>_*` and
  `table/<prefix>_*/*` Glue ARNs (bare `catalog` resource, since the catalog
  itself is shared and cannot be scoped per stage).
- Trino's file-based access control adds a second, defense-in-depth layer: a
  `tables` rule grants a stage's identity full privileges only on
  `<prefix>_.*`-matching schemas within the shared catalog, denying
  everything else — necessary because the `catalogs` rule alone only gates
  which catalog a client may use, not which schemas it can see once inside a
  Glue-provider stage's catalog (every stage's schemas are physically visible
  from every other Glue-provider stage's catalog, since they all resolve to
  the same underlying Glue catalog ID).

AWS stage contracts carry Glue region, catalog ID (the bare account ID, shared
across stages), and workload-identity reference, never credentials. Runtime
access is stage-scoped: a stage runtime receives only its storage, catalog
alias, activation prefix, and identity reference.

EKS Pod Identity remains the AWS workload identity mechanism. Azure retains
AKS OIDC readiness, and local retains development-only Kubernetes Secrets.

### Existing provider substitutions remain binding

AWS continues to use EKS, S3 for Bronze/Silver/Gold/ops, RDS PostgreSQL, ECR,
and Pod Identity behind the existing capability interfaces. Azure continues to
use AKS and ACR while retaining SeaweedFS, Polaris, and in-cluster PostgreSQL.
Trino remains the shared query path; Athena is deferred because it would change
query cost, Superset connectivity, validation, and runtime contracts.

Polaris on local and Azure uses its dedicated persistent PostgreSQL role and
database. Its catalog state must survive a pod restart; an in-memory metastore
would lose catalogs, namespaces, principals, and tables. Polaris credentials
reach workloads through Kubernetes Secret references, never Terraform outputs
or rendered Helm values.

AWS Pod Identity replaces IRSA because the validation environment cannot create
the required IAM OIDC provider. Its contract remains
`identity.aws_pod_identity`; AWS storage and catalog authentication use the
stage workload-identity reference rather than static keys.

Account-specific tags, ownership, region, and cluster naming remain in ignored
`sandbox.tfvars` files or environment variables. All providers retain local
Terraform state, Kubernetes Secrets, port-forward access, no ingress/TLS, no
external secret manager, and no Lake Formation. Secure-state, private-network,
least-privilege, and external-secret work belongs to the secure beta profile.

## Consequences

The catalog technology remains Polaris for local/Azure and Glue for AWS; no
new Iceberg catalog technology is introduced. Physical Glue and S3 names stay
out of Deployment Profiles and `lakehouse_code` descriptors, but AWS's shared
catalog means every consumer that resolves a physical Glue database/schema
name (Floe's per-domain profile rendering, `olf infra catalog sync-namespaces`, dbt
profile/source schema config) must apply the same `namespace_prefix`, or it
silently resolves to the wrong stage's database, or one IAM denies.

External Secrets, ingress, network policy, and live cross-stage isolation
probing (proven for local; not yet wired for AWS/Azure) remain follow-on work.

## History

2026-08-31: Reverted the custom-Glue-catalog-per-stage design: this account's
Glue service does not support `CreateCatalog`, confirmed directly against the
account. AWS now provisions every stage against the account's one default
catalog, with a `namespace_prefix` on physical database/schema names carrying
the isolation the catalog boundary can no longer provide, backed by a second,
defense-in-depth Trino `tables` access-control layer on top of IAM's
`database/<prefix>_*` scoping. Verified end-to-end (deploy, full e2e across
three product pipelines, destroy) against a live AWS account; the code that
implemented this same v3 stage-aware provisioning for Azure remains
`terraform validate`/`plan`-verified only (#114's AWS/Azure completion).

2026-08-28: Rewritten for v3 stages. The former single AWS Glue catalog mapping
is replaced by a custom Glue catalog per stage so canonical SQL remains
portable.
