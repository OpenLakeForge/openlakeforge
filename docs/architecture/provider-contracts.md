# Provider Contracts

OpenLakeForge platform modules exchange provider-neutral contracts instead of
allowing runtime code or project descriptors to select infrastructure directly.
The contract is the portability boundary: the same project can use Polaris and
SeaweedFS locally or on Azure, and Glue and S3 on AWS.

Terraform remains the source of physical provider values. `olf` parses those
values, validates them against the resolved Deployment Profile topology, and
derives the environment consumed by Floe, dbt, Dagster, Superset, and metadata
reconciliation. Product-owned descriptors contain logical identities only.

## Versions and deployment phases

Every Terraform root (local, AWS, Azure) now emits native `3.0.0` contracts.
`olf` retains a v2 adapter that lifts a flat `2.0.0` contract to a single
DEV-stage v3 view, but only to keep reading a contract from state a pre-v3
deploy already produced — no current root's Terraform emits that shape.
Unknown versions fail closed.

`3.0.0` is the binding stage-aware shape, represented by strict schema, typed
parser, and local, Azure, and AWS fixtures. A native v3 runtime must select a
stage explicitly. No profile or project descriptor receives a bucket, catalog,
provider adapter, credential, or generated endpoint.

`olf deploy` has three ordered phases:

1. Foundation creates the cluster and registry.
2. Platform creates static Terraform-managed services.
3. Artifacts deploys data-project-derived images, manifests, reports, and
   metadata.

The static/dynamic boundary is between platform and artifacts. A code commit
runs artifacts only; platform never waits for project artifacts.

`olf` reads the contract from the platform root's Terraform output. A job that
deploys a project revision without that state (a CI runner, say) uses a copy
instead: `olf platform contract -f openlakeforge.yaml > contract.json` prints
it where the platform was applied, and `OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE`
points `olf project build`, `deploy` and `status` at the copy. (`olf e2e run`
still reads the Dagster service and code-location names from the platform's
Terraform outputs, so it needs that state.) The contract names Secrets and
their keys, never their values. A file that is named but missing or malformed fails
the command rather than reading as "not applied yet".

The file replaces the platform contract only. On AWS and Azure, `olf` also
reads the foundation's Terraform outputs (cluster, region, registry), so until
remote state lands (#132) a job there must run where the foundation state is
available, for example a self-hosted runner or a restored `OLF_HOME/state`.

## v3 contract shape

Every v3 document has these top-level fields:

- `deployment`: logical profile name, provider, and region, verified against
  `DeploymentTopology`.
- `shared`: foundation, cluster, metadata database, query, optional catalog or
  governance service, registry, ops storage, secrets, identity/access, and
  observability.
- `stages`: one entry for every enabled DEV/UAT/PROD stage, and no disabled
  stages.

Each stage owns its namespace, distinct Bronze/Silver/Gold storage bindings,
catalog and query bindings, orchestration, capability-dependent reporting and
governance bindings, runtime identity, activation prefix, and endpoint
references. Stage-scoped bindings (storage identity, orchestration, reporting,
governance endpoints, and the stage's own catalog endpoint) may reference only
`shared/*` or the issuing stage's own `stage/<name>/*`; a DEV binding cannot
reference PROD's. Trino query connectivity is the one deliberate exception:
`shared.query` is a single service used by every stage, isolated by catalog
rather than by endpoint, so `stages.<name>.query.endpoint` and `service_ref`
are not stage-scoped.

`stages.<name>.orchestration.code_locations` carries the stage's Dagster
user-code deployments as `{name, definitions_module}` entries — the same list
every root passes to the Dagster module, which renders the webserver's
`workspace.yaml` from it. Because `dagster-user-deployments` names each Service
after its deployment, a code-location name is also an in-cluster host, so the
parser requires an RFC 1035 label -- a Service name must start with a letter,
which DNS-1123 does not require -- and a dotted Python module path, and rejects a
name declared twice within one stage. Two stages may share a name; they run in
different namespaces.

The field is optional. A `3.0.0` contract emitted before it existed resolves to
the single merged location ADR 0006 documents, which is what such a platform's
Dagster release is running; requiring it would make a code commit demand a
platform apply, across the lifecycle boundary ADR 0002 draws.

### `shared.access`

The AWS and Azure roots emit `{ref, implementation: "access.kubectl_port_forward"}`.
The local root emits the ingress shape (ADR 0013), whose four extra fields are
all present or all absent:

```json
"access": {
  "ref": "shared/access",
  "implementation": "access.ingress",
  "base_domain": "olf.localhost",
  "issuer": "local-ca",
  "tls_mode": "ingress-terminated",
  "routes": {
    "stage/dev/orchestration": {"url": "https://dagster.dev.olf.localhost", "enabled": true, "exposure": "user-facing"},
    "shared/catalog_service": {"url": "https://polaris.olf.localhost", "enabled": true, "exposure": "internal"}
  }
}
```

| Rule | Why |
| --- | --- |
| `base_domain` and `issuer` equal the profile's `spec.access` | contract and profile cannot diverge |
| a route key is an existing shared or enabled-stage service ref | a disabled capability exposes no route |
| `user-facing` only for orchestration, reporting, governance, query, identity | databases, storage admin, catalog, registry stay internal; code servers have no ref |
| `url` is `https://<host>.<base_domain>`, unique across routes | one host, one service |
| `tls_mode` is `ingress-terminated` | the only mode an adapter implements |

Ops artifacts are shared storage with a stage-specific activation prefix,
`activations/<stage>`. They do not define the revision manifest or promotion
workflow, which belong to #154 and #115.

## Logical and physical identity

Logical SQL is invariant across providers:

```text
lakehouse_<stage>.<owner>_<layer>.<table>
```

For example, every provider exposes DEV sales orders as
`lakehouse_dev.sales_silver.orders`. Business asset keys and descriptors do not
gain a stage prefix.

- Local: shared Polaris service, one physical Polaris catalog per stage, and
  distinct SeaweedFS Bronze, Silver, and Gold buckets per stage.
- Azure: shared Polaris service on AKS, one physical Polaris catalog per
  stage, and distinct SeaweedFS-on-AKS buckets per stage.
- AWS: every stage shares the account's one default Glue catalog (this
  account's Glue service rejects `CreateCatalog`, confirmed directly against
  the account — a native catalog per stage is not available), exposed through
  Trino's native Glue adapter, and distinct S3 buckets per stage.

Because every AWS stage shares one Glue catalog, `lakehouse_<stage>` is a
*logical* alias only there, not a distinct physical catalog: Trino's stage
catalog ID is the bare 12-digit AWS account ID, identical across stages.
Physical isolation instead comes from a `namespace_prefix` on every physical
Glue database/schema name —
`resolve_physical_names(namespace_prefix=f"{catalog_name}_")` renders
`sales_silver` as `lakehouse_dev_sales_silver` for DEV and
`lakehouse_prod_sales_silver` for PROD, so both stages' physical databases
coexist in the one shared catalog without colliding. Every consumer that
resolves a physical Glue name (Floe's per-domain profile rendering, `olf
catalog sync-namespaces`, dbt profile/source schema config) must apply this
same prefix — IAM scopes each stage's role to `database/<prefix>_*` regardless,
so an unprefixed name is simply denied rather than silently reading the wrong
stage's data. Trino's file-based access control adds a second, defense-in-depth
layer on top of that IAM scoping: a `tables` rule grants a stage's identity
full privileges only on its own `<prefix>_.*`-matching schemas within the
shared catalog, since the `catalogs` rule alone only gates which catalog a
client may use, not which schemas it can see once inside one.

`query.catalog_name` doubles as the name of the Trino catalog properties file
Terraform renders. v2 kept this separate from the Iceberg catalog name as
`trino_catalog_name`, deployed as the fixed value `iceberg` before #114. v3
collapses the two into one field, requiring it to equal `lakehouse_<stage>`;
every root now provisions the Trino catalog as `lakehouse_<stage>` rather than
`iceberg`.

The AWS adapter keeps native Glue rather than introducing another catalog
technology. It supplies the (shared) catalog ID, region, and
workload-identity reference; it never supplies credentials.

## Validation and migration

`tools/olf/tests/test_provider_contracts.py` validates the local, Azure, and
AWS fixture contracts against the published v3 JSON Schema and the typed
parser on every test run (`olf check contracts` covers the Terraform HCL
surface and rendered profile/Floe output; it does not re-run the fixture
suite). The parser rejects unknown fields and versions, missing or extra
topology stages, incomplete capability bindings, cross-stage storage/catalog
endpoint/identity references, duplicate stage storage/catalog/identity values,
unknown or mismatched catalog type/provider pairs, and provider/region/topology
mismatches.

The v2 adapter is intentionally temporary. It lifts a flat deployed contract
to one DEV stage without changing current v0.2 environment output. A v3
contract is not permitted to choose DEV implicitly: callers must select the
stage they are configuring.

## Stable capability interfaces

Consumers depend on fields such as storage URI and bucket, catalog type and
logical name, query endpoint, Secret reference, identity reference, and
artifact activation prefix. They do not depend on SeaweedFS, S3, Polaris, Glue,
kind, AKS, EKS, or a physical catalog name. See ADR 0003 for the binding
decision and ADR 0010 for the provider mappings.
