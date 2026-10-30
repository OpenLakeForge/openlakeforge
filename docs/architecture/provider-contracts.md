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

`olf infra deploy` has three ordered phases:

1. Foundation creates the cluster and registry.
2. Platform creates static Terraform-managed services.
3. Artifacts deploys data-project-derived images, manifests, reports, and
   metadata.

The static/dynamic boundary is between platform and artifacts. A code commit
runs artifacts only; platform never waits for project artifacts.

`olf` reads the contract from the platform root's Terraform output. A job that
deploys a project revision without that state (a CI runner, say) uses a copy
instead: `olf infra platform contract -f openlakeforge.yaml > contract.json` prints
it where the platform was applied, and `OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE`
points `olf infra project build`, `deploy` and `status` at the copy. (`olf infra e2e run`
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
    "stage/dev/orchestration": {"url": "https://dagster-dev.olf.localhost", "enabled": true, "exposure": "user-facing"},
    "shared/portal": {"url": "https://olf.localhost", "enabled": true, "exposure": "user-facing"},
    "shared/catalog_service": {"url": "https://polaris.olf.localhost", "enabled": true, "exposure": "internal"}
  }
}
```

| Rule | Why |
| --- | --- |
| `base_domain` and `issuer` equal the profile's `spec.access` | contract and profile cannot diverge |
| a route key is an existing shared or enabled-stage service ref | a disabled capability exposes no route |
| `user-facing` only for orchestration, reporting, governance, query, identity, portal | databases, storage admin, catalog, registry stay internal; code servers have no ref |
| `url` is `https://<host>.<base_domain>`, unique across routes; the portal's is `https://<base_domain>` | one host, one service |
| `tls_mode` is `ingress-terminated` | the only mode an adapter implements |
| optional `perimeter` is `{implementation, enabled, protected_routes, callback_url}` and needs the ingress fields | what sits behind authentication (ADR 0014 seam 3) |
| `protected_routes` is exactly the enabled user-facing routes except `shared/identity` (empty when `enabled` is false) | a user-facing route left out is an open door, so `olf` rejects it |
| `callback_url` is `https://<host>.<base_domain>/oauth2/callback` | the one redirect URI of the `perimeter` client |

The local root emits `perimeter` once the perimeter adapter lands (#176); the
AWS and Azure roots do not. When public DNS and ACME exposure is added, `olf`
must refuse a contract that exposes routes publicly without an enabled
perimeter. That refusal is not implemented, because no contract field says a
route is public yet.

`olf` port-forwards to every route in `protected_routes` of an enabled
perimeter instead of using its URL, because the ingress answers a request with
no session by redirecting to login.

### `shared.identity`

Every root emits `{ref, implementation, roles}`. `implementation` is whatever
the provider already used; `roles` is the canonical role model (ADR 0014),
identical on every provider because each root renders it with
`yamldecode(file("<repo>/release/identity-roles.yaml"))`:

```json
"identity": {
  "ref": "shared/identity",
  "implementation": "identity.oidc",
  "roles": {
    "precedence": ["platform-admin", "data-engineer", "analyst", "viewer"],
    "grants": {
      "reporting": {"platform-admin": "Admin", "data-engineer": "Alpha", "analyst": "Gamma", "viewer": "read-only"},
      "query": {"platform-admin": "yes", "data-engineer": "yes"}
    },
    "data_access": {
      "analyst": {"dev": {"gold": "read"}, "uat": {"gold": "read"}, "prod": {"gold": "read"}},
      "viewer": {"uat": {"gold": "read"}, "prod": {"gold": "read"}}
    }
  }
}
```

(`grants` and `data_access` are abbreviated here; the full matrices are in the
ADRs and the release file.)

| Rule | Why |
| --- | --- |
| `roles` is present and equals `release/identity-roles.yaml` | one source for the matrix; a contract rendered from another file is rejected |
| every role named in `grants` is in `precedence` | the role vocabulary is closed |
| every `grants` key is a service a route can publish (`orchestration`, `reporting`, `governance_service`, `query`) | the perimeter resolves route ref, to service, to roles |
| grants are monotonic across `precedence` | multi-role union equals the highest role |
| `data_access` names only roles in `precedence`, stages `dev`, `uat`, `prod`, layers `bronze`, `silver`, `gold`, and `read` or `write` | the same closed vocabulary; an absent role, stage or layer is no access |
| each `data_access` stage row is every layer `write`, every layer `read`, or `gold: read` alone | the only rows Polaris catalog roles express ([ADR 0019](../adr/0019-data-plane-authorization-deferral.md), section 5) |
| `data_access` is monotonic across `precedence` on every stage and layer | as for `grants`; nothing enforces it yet: Polaris grants arrive in a later build step |

With `implementation: identity.oidc` (ADR 0014 seam 2, #24) the binding also
carries the issuer. No field is issuer-specific; everything is keyed by a
canonical role or a consumer, and a credential is only ever a Secret
reference:

```json
"identity": {
  "ref": "shared/identity",
  "implementation": "identity.oidc",
  "roles": {"...": "as above"},
  "issuer_url": "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_Example",
  "role_claim": "cognito:groups",
  "role_mapping": {"platform-admin": ["olf-admins"], "viewer": ["olf-viewers"]},
  "clients": {
    "perimeter": {"client_id": "olf-perimeter", "secret_ref": {"name": "oidc-perimeter-client", "key": "client-secret"}}
  }
}
```

(`clients` must name all of `perimeter`, `superset`, `openmetadata`, `trino`, and `olf-cli`, which has a `client_id` and no `secret_ref`.)

| Rule | Why |
| --- | --- |
| the four issuer fields appear together, only with `identity.oidc` | other implementations stay unchanged |
| `issuer_url` is https with a host and no userinfo, query or fragment (the schema only checks `^https://`; the Python check is the authority) | tokens and redirects must not cross plaintext |
| `adapter` is `dex` when the profile selects `github` | provenance for consumers, but the validator needs it to correlate the profile with its binding; external profiles omit it |
| `role_mapping` keys are canonical roles; each value is a non-empty list of claim values | an unknown role fails closed; the claim values are the issuer's vocabulary (GitHub `<org>:<team>` values, Cognito groups, Entra object IDs) |
| each client is `client_id` plus `secret_ref{name,key}` | a literal secret is an unsupported field; the #181 secret-value check also covers it |

The `identity.oidc` contract is parsed and schema-checked here. The local root
emits it (adapter `dex`, `role_claim: groups`, the
`role_mapping` of each role to its `<org>:<team-slug>` values); the AWS and Azure
roots do not yet. With `spec.identity.issuer: external` the local root emits
the profile's issuer fields and names the client Secrets
`oidc-client-<consumer>` (key `client-secret`) for the operator to provide. The optional `admin` block (seam 4)
is not in the schema until #331. The perimeter fields are in `shared.access`.

The local root also emits an optional `shared.portal` binding
(`{ref: "shared/portal", implementation: "portal.static_page"}`), which is what
the `shared/portal` route resolves to.

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
resolves a physical Glue name (`olf infra catalog sync-namespaces`, dbt
profile/source schema config, and Floe once Silver runs on Glue) must apply this
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

`tools/olf/tests/common/test_provider_contracts.py` validates the local, Azure, and
AWS fixture contracts against the published v3 JSON Schema and the typed
parser on every test run (`olf check contracts` covers the Terraform HCL
surface and rendered profile/Floe output; it does not re-run the fixture
suite). The parser rejects unknown fields and versions, missing or extra
topology stages, incomplete capability bindings, cross-stage storage/catalog
endpoint/identity references, duplicate stage storage/catalog/identity values,
unknown or mismatched catalog type/provider pairs, and provider/region/topology
mismatches.

Contracts carry Secret names and keys, never values (#181). `olf check
contracts` rejects, without needing applied state, a sensitive or unwrapped
output in any root `.tf` file and a secret-named field in a root's
`contracts.tf` that holds a literal value or reads a generated password. It
applies the same field rule to an applied contract, and the fixture suite
applies it to every fixture.

The v2 adapter is intentionally temporary. It lifts a flat deployed contract
to one DEV stage without changing current v0.2 environment output. A v3
contract is not permitted to choose DEV implicitly: callers must select the
stage they are configuring.

## Where stage bindings reach a workload

A project revision and its image name no stage (ADR 0012). Each workload reads
its stage's values from its own pod's environment, set when that stage is
activated.

| Workload | Receives | From | Missing value |
| --- | --- | --- | --- |
| Code-location server | The contract values the runtime reads (`_RUNTIME_ENV` in `tools/olf/olf/infra/deployment/activation.py`), `OPENLAKEFORGE_STAGE`, and the storage, Floe-catalog and lineage Secrets | The stage's provider contract, through the activation's Helm release | The value is absent. The allowlist test fails when runtime code reads a name nothing sets. |
| Run pod | Everything the server has (`includeConfigInLaunchedRuns`), plus the source bindings of its job | The release, plus the job's `dagster-k8s/config` tag | Kubernetes does not start a pod whose ConfigMap or Secret key is absent |
| Floe Job | The contract values the run pod forwards to it | dagster-floe | `libs/floe_profile.py` raises naming the variable |
| dbt in a run pod | `OPENLAKEFORGE_QUERY_TRINO_*`, `OPENLAKEFORGE_DBT_TRINO_*`, `OPENLAKEFORGE_CATALOG_NAME` and `_SCHEMA_PREFIX` | The run pod's env, through the profile's `env_var()` | dbt fails to render the profile |
| dlt loader in a run pod | `SOURCES__<ID>__<KEY>` for each key in `source.yaml` `runtime`, and the stage's Bronze bucket and S3 settings | ConfigMap `olf-source-<id>` and Secret `olf-source-<id>-credentials` in the stage namespace | dlt raises its missing-config error; `bronze_pipeline` raises if the stage or bucket is unset |

**Precedence.** A source binding is the only source of its `SOURCES__*` name.
The project and the image set none. A loader's code default never applies
either, because the key is required (`dlt.config.value` / `dlt.secrets.value`).
For dlt's S3 credentials, a key the stage sets wins. An unset key falls back to
the pod's default AWS credential chain, which is how a cloud workload identity
is picked up.

**Fail closed.** `olf infra deploy` refuses to activate a revision whose sources the
stage has not bound. It names each missing ConfigMap, Secret or key, and
reads Secret key names only, never values. A binding removed after
activation stops the next run pod from starting. No workload falls back to
another stage's value.

**Rotation.** Each run pod resolves its ConfigMap and Secret references when
it starts. Editing a source binding therefore applies to the next run, with
no rebuild and no redeploy. Changing a provider-contract value, such as a
bucket or catalog, means activating the same revision again: the digest is
unchanged, and the release carries the new value.

**Limits.**

- A run pod hosts every step of its run, so each loader in a job sees every
  source binding that job carries.
- A Bronze asset materialized outside its product job gets no source
  binding.
- Per-source isolation within one job would need a pod per step, and is not
  enforced.
- Run-pod sizing is not a stage binding yet (#323, #325).

## Stable capability interfaces

Consumers depend on fields such as storage URI and bucket, catalog type and
logical name, query endpoint, Secret reference, identity reference, and
artifact activation prefix. They do not depend on SeaweedFS, S3, Polaris, Glue,
kind, AKS, EKS, or a physical catalog name. See ADR 0003 for the binding
decision and ADR 0010 for the provider mappings.
