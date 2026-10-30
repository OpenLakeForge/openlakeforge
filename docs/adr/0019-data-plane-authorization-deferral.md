# ADR 0019: Data-plane authorization — Dex identity, Polaris grants

## Status

Decided, **build step 1 of 8 landed**: this is the design for #367. Step 1
puts `data_access` in the role model, validated by `olf` and enforced by
nothing yet. Verification items 1 (SeaweedFS STS enforces the session policy),
2 (Polaris vends table-scoped credentials through it), 3 (Polaris trusts the
local CA for Dex) and 4 (Dex's GitHub connector issues the tokens the design
needs to `olf-cli`) passed. Item 3 found that Polaris activates **all** of a
principal's roles when a token maps to none; section 4, step 11 is corrected
for it. **Binding today:**
v0.5-alpha has service-level authorization only, and nothing in it restricts
which tables a signed-in user reads (Context). **Decided for the build:**
`olf query` stops running through `kubectl exec` as the stage's shared
principal. A person signs in with Dex, Trino authenticates the token, and the
person's canonical role reaches Polaris, whose grants decide which stage and
layer they read or write. Scope is the local provider with
`spec.identity.issuer: github`. AWS and Glue are out of scope by decision
(below). Superset, dbt and OpenMetadata keep their shared stage principals
(ADR 0016); per-user access through Superset stays deferred.

The design is **query-engine agnostic** (section 3): canonical roles exist only
as Polaris grants, and Polaris also issues the storage credentials a person's
query reads with. A query engine is an adapter that forwards the person's token
and uses those credentials. Trino is the first; DuckDB, run in the cluster as a
server, is the planned second.

Every claim below carries a mark. **V** is verified against the cited source
or documentation (read, not run). **T** is to test: derived or assumed, and
not run. Pins: Trino 480 and the Apache Iceberg 1.10.1 client it builds
against (`dep.iceberg.version` in Trino's `pom.xml` at tag 480), Polaris
1.4.0, Dex v2.46.0, SeaweedFS 4.48 (`release/component-catalog.yaml`).
Polaris sources are read at tag `apache-polaris-1.4.0`, Iceberg at
`apache-iceberg-1.10.1`, SeaweedFS at `4.48`. DuckDB is not pinned yet; its
claims are from the DuckDB Iceberg catalog documentation.

## Context

The canonical role decides which services a person reaches (ADR 0014). It does
not decide what they read once inside. Stated plainly for operators: **today a
user's role narrows what they can reach through Superset** (`analyst` and
`viewer` hold `schema_access` on the Gold schemas only; `platform-admin` and
`data-engineer` hold all-database access, `identity-service-permissions.md`),
**but nothing below Superset enforces it.** Trino sees one shared principal per
stage, so anything that bypasses Superset's checks reaches everything that
principal reads. Do not load data of differing sensitivity into a deployment
whose users should not all see it. That stays true for Superset after this
design is built.

`olf query` today (`olf/commands/query.py`, `olf/trino.py`) runs the Trino CLI
inside the coordinator pod with `kubectl exec`, as `olf-<stage>-runtime`, the
stage principal that also writes. Who may query is who holds `pods/exec` on the
coordinator; the person's role plays no part, and every caller is the same
Trino user.

The goal is a per-person query path: a person runs `olf query` from a
workstation with no kubeconfig, signs in through Dex, and their canonical role
decides what they read and write, per stage and per medallion layer.

### Why Polaris grants, not Trino rules

The previous version of this record chose Trino file-based rules keyed by
canonical role, because Trino is the one enforcement point Polaris and Glue
share (ADR 0015). This design reverses that for the person path:

| # | Finding | Mark | Reference |
| --- | --- | --- | --- |
| 1 | Trino 480 cannot read groups from a token: the `jwt` and `oauth2` authenticators' property lists have no groups field, and file-based rules match `group` only through a `file` or `ldap` group provider. A role in Trino would need a group file synced from GitHub membership | V | Trino 480 docs, JWT authentication, OAuth 2.0 authentication |
| 2 | Polaris reads roles from a claim: `quarkus.oidc.roles.role-claim-path` selects it, and the principal-roles mapper keeps the values matching `filter` and rewrites each through an ordered list of `regex` / `replacement` mappings | V | `DefaultPrincipalRolesMapper`, `OidcTenantConfiguration.PrincipalRolesMapper` |
| 3 | The roles active on a request are the token's mapped roles that the principal was also granted in Polaris; a role the token does not carry is not active. **Except when the token maps to no role at all:** the authenticated principal then has an empty role set, and the resolver activates every role the principal was granted. Section 4, step 11 keeps the set from ever being empty | V, run (item 3 proof) | `DefaultAuthenticator.resolvePrincipalRoles`; `Resolver.resolveCallerPrincipalAndPrincipalRoles` (`getRoles().isEmpty() ? resolveAllPrincipalRoles(...)`) at `apache-polaris-1.4.0` |
| 4 | Polaris catalogs are already per stage (`lakehouse_<stage>`, one per stage catalog in the bootstrap), so a grant on a catalog is a grant on a stage. The stage binding needs no rewritten Trino user name, which the previous version listed as an open prerequisite | V | `modules/catalog/polaris/templates/bootstrap.sh.tftpl` |
| 5 | Iceberg data files are read with Trino's own S3 key and vended credentials are off, so Polaris does not gate file reads directly. It gates the table metadata: `LOAD_TABLE` needs `TABLE_READ_PROPERTIES`, `UPDATE_TABLE` needs `TABLE_WRITE_PROPERTIES`, a direct create `TABLE_CREATE`. Without the metadata Trino has no file to read and no commit to make. Section 3 replaces that key with Polaris-issued credentials on the person path | V (operations); T (no read path in the Iceberg connector skips `loadTable`) | `PolarisAuthorizableOperation`; `modules/query/trino/main.tf` |

So the role check lives where the role claim can be read without a sync, and
the stage boundary is the catalog boundary that already exists. The cost is
the one ADR 0015 named: table policy written against Polaris does nothing on
Glue. **Accepted:** AWS gets no per-person query path from this design. AWS's
`olf query` already refuses the provider (`commands/query.py`), and Glue with
Lake Formation stays a separate adapter if it is ever wanted.

## Decision

### 1. The access matrix

| Role | DEV Bronze, Silver | DEV Gold | PROD Bronze, Silver | PROD Gold |
| --- | --- | --- | --- | --- |
| `platform-admin` | read, write | read, write | read, write | read, write |
| `data-engineer` | read, write | read, write | read | read |
| `analyst` | — | read | — | read |
| `viewer` | — | — | — | read |

"Write" is DML and DDL on tables, views and namespaces, which is Polaris
`CATALOG_MANAGE_CONTENT`. It is not grant management: no role administers
Polaris access through SQL, and the matrix changes only by a deploy.
`platform-admin`'s "full access" is this row; administering grants stays the
bootstrap's (section 5).

The matrix is monotonic, as ADR 0014 requires for every grant: each role
reaches everything the role below it reaches, so a person in several mapped
teams holds the union, which is the highest role's row. `viewer` was asked as
"read-only PROD"; it is PROD Gold only, which keeps that rule (maintainer
decision while drafting this record).

`uat` takes the PROD column for every role, `viewer` included: `viewer` reads
`uat` Gold (maintainer decision).

### 2. Where the matrix lives

It is product-level and fixed, like the service grants, so it is a section of
`release/identity-roles.yaml` keyed by canonical role: the "fifth mapping keyed
by canonical role" ADR 0014 reserves.

```yaml
data_access:  # role -> stage -> layer -> read | write; absent is none
  platform-admin:
    dev:  {bronze: write, silver: write, gold: write}
    uat:  {bronze: write, silver: write, gold: write}
    prod: {bronze: write, silver: write, gold: write}
  data-engineer:
    dev:  {bronze: write, silver: write, gold: write}
    uat:  {bronze: read, silver: read, gold: read}
    prod: {bronze: read, silver: read, gold: read}
  analyst:
    dev:  {gold: read}
    uat:  {gold: read}
    prod: {gold: read}
  viewer:
    uat:  {gold: read}
    prod: {gold: read}
```

`olf` validates it with the rules it applies to `grants`: roles inside
`precedence`, stages inside the stage enum, layers `bronze`, `silver`, `gold`,
and monotonic in both senses (a higher role holds at least the access of a
lower one on every stage and layer). The provider contract carries it in
`shared.identity.roles` like the rest of the file, and the published schema
gains the section.

Polaris can express only three shapes per stage catalog, so validation also
rejects a row that is not one of them: all layers `write`, all layers `read`,
or `gold: read` alone (section 5). A fourth shape is a new catalog role, added
when a role needs it.

### 3. Engine neutrality

Polaris is the only authority for a person's data access, so that a query
engine can be added or replaced without moving any policy. Two rules bind every
engine adapter:

1. **No engine carries roles.** Canonical roles and the access matrix exist only
   as Polaris grants (sections 2 and 5). An engine's own configuration may tell
   people apart from workloads (Trino's rules in step 6 of section 4); it never
   names a canonical role, a team or a layer.
2. **No engine holds a storage key for a person's query.** Polaris decides the
   table metadata, and today the engine reads the files with its own key
   (finding 5). That is safe only while one server holds the key and its SQL
   cannot address arbitrary paths. It does not hold as a rule for every engine,
   so on the person path the engine asks Polaris for **vended credentials**:
   short-lived storage credentials limited to the table's location, issued only
   when the active roles hold the data privilege.

| # | Finding | Mark | Reference |
| --- | --- | --- | --- |
| 1 | Loading a table with read delegation needs `TABLE_READ_DATA`, with write delegation `TABLE_WRITE_DATA`. `CATALOG_MANAGE_CONTENT` implies both, `TABLE_READ_DATA` implies only the read. The grants of section 5 therefore already decide which credentials are vended | V | `PolarisAuthorizableOperation` (`LOAD_TABLE_WITH_READ_DELEGATION`, `..._WRITE_DELEGATION`); `PolarisAuthorizerImpl` super-privileges |
| 2 | Polaris vends S3 credentials through STS `AssumeRole` with a session policy scoped to the table, signing the call with its own static storage key. A catalog's S3 storage configuration names the STS endpoint (`stsEndpoint`), so a non-AWS STS works. Credentials last `STORAGE_CREDENTIAL_DURATION_SECONDS` (default one hour) and are cached for `STORAGE_CREDENTIAL_CACHE_DURATION_SECONDS` (default 30 minutes). The bootstrap creates every stage catalog with `stsUnavailable: true`, so nothing is vended today | V, run (item 2 proof below) | `AwsCredentialsStorageIntegration`, `FeatureConfiguration` at `apache-polaris-1.4.0`; `modules/catalog/polaris/templates/bootstrap.sh.tftpl` |
| 3 | SeaweedFS 4.48 implements STS `AssumeRole` and accepts a session `Policy` of at most 2048 characters, with durations from 15 minutes to 12 hours. A request signed with the issued credentials is allowed only when the role's attached policies **and** the session policy both allow it; a request without a session token keeps the legacy authorization of its static identity | V, run (proof below) | `weed/s3api/s3api_sts.go`, `weed/iam/integration/iam_manager.go`, `weed/s3api/auth_credentials.go` (`VerifyActionPermission`, `authorizationRoute`) at tag `4.48` |
| 4 | Trino 480 asks for vended credentials per REST catalog with `iceberg.rest-catalog.vended-credentials-enabled`, which sends `X-Iceberg-Access-Delegation: vended-credentials` | V (property and header); T (the people catalog reads and writes with no static S3 key) | `IcebergRestCatalogConfig`, `TrinoIcebergRestCatalogFactory` |
| 5 | DuckDB attaches an Iceberg REST catalog with a bearer token (`CREATE SECRET (TYPE iceberg, TOKEN ...)`) and requests vended credentials with `ACCESS_DELEGATION_MODE 'vended_credentials'`; Polaris is the documented example | V (documentation); T (against this Polaris) | DuckDB Iceberg extension, "Iceberg REST catalogs" |

Vended credentials are a property of the request, not of the catalog. Workloads
reach the same Polaris catalogs without asking for delegation (Trino's
`lakehouse_<stage>` catalogs keep `vended-credentials-enabled=false` and dbt
writes through them, Floe keeps its storage key), so pipelines are unchanged and still do not depend
on STS or Dex (T: a catalog with STS available still serves a request without
delegation).

| Engine | Person's token to Polaris | Data files | Status |
| --- | --- | --- | --- |
| Trino 480 | The people catalog, `session=USER`, extra credential `token` (section 4, steps 7-9) | Vended, `vended-credentials-enabled=true`, no static key in the people catalog | This design |
| DuckDB, a server in the cluster | Attached per person with their token as the Iceberg secret | Vended, `ACCESS_DELEGATION_MODE 'vended_credentials'` | Planned. DuckDB has no catalog rules or roles of its own, so it needs no policy; its adapter must authenticate the person and keep each person's attached catalog and secret in a session no other person shares. Designed when DuckDB is added |

### 4. The request path

```mermaid
sequenceDiagram
    actor P as Person
    participant O as olf query
    participant D as Dex
    participant X as Perimeter (query_api)
    participant T as Trino
    participant C as Polaris
    participant S as SeaweedFS
    P->>O: olf query --stage prod "SELECT ..."
    O->>D: device authorization, client olf-cli
    D-->>P: verification URL and user code (GitHub sign-in)
    D-->>O: access token (email, groups, federated_claims), refresh token
    O->>X: POST /v1/statement, Bearer token, extra credential token
    X->>T: forwarded after token and role-floor check
    T->>T: JWT authenticator: user = email; rules allow people catalogs only
    T->>C: loadTable on lakehouse_prod, Bearer = the person's token
    C->>C: mixed auth: Dex JWKS, principal github, roles from groups
    C->>S: STS AssumeRole, session policy = this table's location
    C-->>T: table metadata and vended credentials, or 403 for a role without the grant
    T->>S: read the data files with the vended credentials
    T-->>O: rows, or the Polaris denial
```

| Step | Piece | Detail | Mark |
| --- | --- | --- | --- |
| 1 | Dex client | A new public static client `olf-cli` with the device authorization grant. Scopes `openid email groups federated:id offline_access`: `email` names the Trino user, `groups` carries the roles, `federated:id` the connector id (step 11), `offline_access` a refresh token. Registered as a static client with `public: true` and the one redirect URI `/device/callback` | V, run (item 4 proof) |
| 2 | Token cache | `<OLF_HOME>/auth/oidc-<base_domain>.json`, mode 0600, beside the existing `olf/auth/state.py` provider state. Refreshed when the access token has under 60 seconds left; Dex's session policy bounds it (ADR 0014: tokens 5 min, refresh idle 30 min, absolute 10 h) | — |
| 3 | Transport | `olf` speaks Trino's HTTP protocol with `requests` (already a dependency): `POST https://trino.<base_domain>/v1/statement`, then each `nextUri`. Every request carries `Authorization: Bearer` with a token refreshed if due; the submission also carries `X-Trino-Extra-Credential: token=<the same token>` and `X-Trino-Catalog` set to the stage's people catalog. Issuer and Trino URLs come from the profile's `spec.access.base_domain`, so no kubeconfig is needed. The local CA is trusted the way `olf access trust` already prints | T (protocol use) |
| 4 | Perimeter | A route `query_api`: host `trino.<base_domain>`, path prefix `/v1`, behind a perimeter instance that accepts a bearer JWT for audience `olf-cli` (oauth2-proxy `--skip-jwt-bearer-tokens`) at the `viewer` floor. The existing `query` route (the Web UI) keeps the `data-engineer` floor. The service passwords (ADR 0016) then cannot be used through the ingress without also holding a member's Dex token | T (oauth2-proxy applies `--allowed-group` to bearer sessions) |
| 5 | Trino authentication | `http-server.authentication.type=PASSWORD,JWT`. JWT: `key-file=https://auth.<base_domain>/keys`, `required-issuer`, `required-audience=olf-cli`, `principal-field=email`. A token minted for the perimeter, Superset, OpenMetadata or the Trino UI client carries another audience and is refused | V (properties); T (Dex's `aud` is the requesting client id, read in Dex's scope documentation; JWKS fetch through the CoreDNS rewrite) |
| 6 | Trino rules | A user containing `@` (an e-mail) may use the people catalogs only, with `allow: all` (Polaris decides). Service principals keep their catalogs. A `principals` rule lets an e-mail principal become only itself, before ADR 0016's final deny. A `queries` rule lets a person view and kill only their own queries: today `rules.json` has no `queries` section, so every user may view every query | V (no `queries` rules means all users view all queries: Trino 480 file-based access control); T (rules) |
| 7 | People catalog | One more Trino catalog per stage on the same Polaris warehouse, named `<stage>` (`dev`, `uat`, `prod`): `iceberg.rest-catalog.session=USER`, `iceberg.rest-catalog.session-timeout=0s`, `iceberg.rest-catalog.vended-credentials-enabled=true` with no static S3 key (section 3), and an OAuth2 credential of a Polaris principal `trino-people-<stage>` that holds no grant. `olf query` makes it the session default, so the name shows only in fully qualified SQL (`prod.order_revenue_gold.mart_order_revenue_by_day`). It is Trino connection plumbing, not policy: it exists because `session=USER` is all-or-nothing per catalog (rejected alternatives) | V (properties) |
| 8 | Token to Polaris | With `session=USER` Trino passes the user's extra credentials to the Iceberg client and adds an unsigned JWT of its own as `JWT_TOKEN_TYPE`. The Iceberg client uses a `token` credential as the bearer **before** trying any token exchange. So the person's Dex token reaches Polaris; a request without it falls through to exchanging Trino's unsigned JWT, which Polaris does not accept (ADR 0015), and fails closed | V (`TrinoRestCatalog.convert`; `OAuth2Manager.maybeCreateChildSession`); T (Polaris's refusal of the exchange) |
| 9 | Session cache | The Iceberg session is cached per `<user>-<source>` with `expireAfterAccess(session-timeout)`, the default one hour. A person querying more often than that would keep presenting their first, expired token, so the people catalog sets the timeout to zero and builds the session from the current token each time. Wrapping a bearer token makes no network call | V (`AuthSessionCache`, `IcebergRestCatalogConfig`: `@MinDuration("0ms")`); T (zero disables reuse) |
| 10 | Polaris authentication | `mixed`: Polaris's own tokens first, then Quarkus OIDC. `quarkus.oidc.auth-server-url` `https://auth.<base_domain>`, `discovery-enabled=false` with `jwks-path=keys` (as Trino's UI does, because Dex is served by Traefik), the local CA through a named TLS configuration (`quarkus.oidc.tls.tls-configuration-name`, `quarkus.tls.<name>.trust-store.pem.certs`), `quarkus.oidc.token.audience=olf-cli`. Principal mapper `name-claim-path: federated_claims/connector_id`; roles from `groups` and `federated_claims/connector_id` (step 11) | V, run (item 3 proof) |
| 11 | Polaris principal | Every Dex token from the GitHub connector names the principal `github` (the Dex module's connector id). The bootstrap creates it and grants it the four role principal roles and `olf-signed-in`, a principal role with no catalog role. The token's `groups` decide which canonical roles are active (finding 3). Its connector id is also read as a role and mapped to `olf-signed-in`, so the active set is never empty: without that, a token whose groups map to nothing, including one requested without the `groups` scope, which the person chooses in their own sign-in, would activate all four roles, platform-admin included (item 3 proof, o3 and o3c). Revocation is Dex's: a person removed from a team is refused at their next refresh, within 5 minutes, with no Polaris change | V, run (item 3 proof; item 4 proof, g5 and g12; connector id `github` in `modules/identity/dex/main.tf`) |

### 5. Polaris objects

Names carry the `olf-` prefix so they never collide with the workload
principals and roles the bootstrap already creates.

| Object | Scope | Content | Created by, phase |
| --- | --- | --- | --- |
| Principal `github` | realm | Granted the four principal roles below and `olf-signed-in` | Polaris bootstrap Job, platform |
| Principal role `olf-signed-in` | realm | No catalog role. Active on every person's request, so the active role set is never empty (section 4, step 11) | bootstrap, platform |
| Principal roles `olf-platform-admin`, `olf-data-engineer`, `olf-analyst`, `olf-viewer` | realm | Each assigned the catalog roles its `data_access` row needs, per stage catalog | bootstrap, platform |
| Catalog role `olf-write` | each `lakehouse_<stage>` | `CATALOG_MANAGE_CONTENT` on the catalog | bootstrap, platform |
| Catalog role `olf-read` | each `lakehouse_<stage>` | On the catalog: `TABLE_READ_DATA`, `NAMESPACE_READ_PROPERTIES`, `VIEW_READ_PROPERTIES` | bootstrap, platform |
| Catalog role `olf-gold-read` | each `lakehouse_<stage>` | `NAMESPACE_LIST` on the catalog; on **each Gold namespace**: `TABLE_READ_DATA`, `NAMESPACE_READ_PROPERTIES`, `VIEW_READ_PROPERTIES` | catalog grant: bootstrap, platform; namespace grants: `olf catalog sync-namespaces`, artifacts |
| Principal `trino-people-<stage>` | realm | No catalog role. The people catalog's own credential, used for the configuration call | bootstrap, platform |
| Principal-roles mapper | Polaris chart values | `quarkus.oidc.roles.role-claim-path` `groups,federated_claims/connector_id`; `filter` matching every claim value in `role_mapping` and the connector id; one mapping per claim value, `^<quoted value>$` to `PRINCIPAL_ROLE:olf-<role>`, plus `^github$` to `PRINCIPAL_ROLE:olf-signed-in`, rendered by the local root from `role_mapping`, so keyed by canonical role | Terraform, platform |

`TABLE_READ_DATA` implies `TABLE_READ_PROPERTIES` and `TABLE_LIST` in Polaris's
privilege hierarchy (V, `PolarisAuthorizerImpl` super-privileges). It is
chosen over `TABLE_READ_PROPERTIES` because read delegation needs it (section 3,
finding 1): a read grant yields read-only credentials, and only
`CATALOG_MANAGE_CONTENT` yields write credentials. Grants on a catalog apply to
everything in it (T).

Gold namespaces come from the descriptors, not from a list in shared code:
`PhysicalProductNames.gold_namespace` in the domain model, resolved by the same
inventory `sync-namespaces` already uses. Granting on a namespace the artifacts
phase creates keeps rule 3 of `AGENTS.md`: the platform apply never waits for
user code. A pruned namespace takes its grants with it.

**Accepted cost:** the stage deployer principal gains `CATALOG_MANAGE_ACCESS` on
its own stage catalog to make those grants. It already holds
`CATALOG_MANAGE_METADATA`, which can drop any table there; the added power is
granting within that one catalog, including to itself. The alternative, a
nested `gold` parent namespace with one grant, means re-laying every Gold
namespace and is not worth it for this.

`NAMESPACE_LIST` at catalog level lets `analyst` and `viewer` see the names of
Bronze and Silver namespaces in `SHOW SCHEMAS`, not their tables. Without it a
Gold-only role cannot list schemas at all (T). Accepted.

### 6. `olf query`

| Behaviour | Detail |
| --- | --- |
| Default | With `spec.identity.issuer: github` and `spec.access`, `olf query` uses the person path above. It signs in on first use or when the refresh token has expired, printing Dex's verification URL and code. A person GitHub admits to no mapped team is refused in the browser, but the device code itself stays `authorization_pending` until it expires (item 4 proof, g8), so `olf` stops polling after a bounded wait and says the sign-in was not completed or refused |
| `--as-workload` | Today's path, kept: `kubectl exec` as `olf-<stage>-runtime`. It is the break-glass path when Dex is down (ADR 0014 lists Trino through its internal Service) and the one `olf e2e` uses. Who may use it is still who holds `pods/exec`, a platform-admin in practice |
| No issuer | With no `spec.identity`, or `issuer: external`, `olf query` keeps the workload path: there is no Dex client to sign in with. An external issuer would need its own principal mapping and is not designed here |
| Denial | Polaris's 403 reaches the person as a Trino error. `olf` names the role it read from the token, the stage and the schema, and says which role grants the access, from `data_access` |
| Docstring | "It is not per-user access" becomes false on the person path and is rewritten with the build |

### 7. Limits accepted

| Limit | Why it is accepted |
| --- | --- |
| A write that commits after the token expires fails at the commit. Polaris receives the token captured when the query was submitted; `olf` refreshes before submitting, so a statement has about 5 minutes to reach its commit (T) | `olf query` is for inspection and small changes. Loads belong to pipelines, which keep their workload credentials. A longer Dex token lifetime would loosen the ADR 0014 session bound for every client |
| Polaris records the principal `github` and the active roles, not the person | Trino's query log records the person (user = e-mail). Per-person principals would need GitHub membership synced into Polaris (rejected below) |
| The extra credential is the bearer: a client that holds another person's token gets that person's grants | Possession of a bearer token is the authority, as on every OIDC path. Trino keeps extra credentials out of the session it serialises into query information (V, `SessionRepresentation`), so the token is not shown in the UI or `system.runtime.queries` |
| Gold-only roles see Bronze and Silver namespace names | Section 5 |
| Superset users keep the shared stage principal | The Context paragraph stays true for Superset. A later step can point a Superset database at the people catalog with Superset's per-database OAuth2 (Superset table below, claims 6 and 7) |
| Vended credentials outlive the 5-minute Dex token: Polaris issues them for one hour by default (item 2 proof), and SeaweedFS will not issue less than 15 minutes. A person removed from a team keeps reading the tables already loaded until they expire. Build step 2 sets `STORAGE_CREDENTIAL_DURATION_SECONDS` to 900 and the credential cache below it (T) | Revocation still ends new table loads within 5 minutes; the credentials cover only tables the person could read when they were issued |
| Nothing for AWS or Azure | AWS uses Glue, out of scope by decision. Azure runs Polaris but has no Dex adapter yet (ADR 0014); it gains the path when it does |

### 8. What changes in other records when this is built

Each lands with the change that makes it true (`AGENTS.md`, "One change per
pull request").

| Record | Change |
| --- | --- |
| ADR 0014 | `data_access` beside `grants`; the `query_api` service row (all four roles); `olf-cli` as a fifth Dex client |
| ADR 0015 | The deferral ends for one caller: `mixed` with OIDC for people tokens. Every workload keeps Polaris's own credentials, so pipelines still do not depend on the issuer. Stage catalogs are created with STS available, and its "Data files" row changes for the person path |
| ADR 0016 | `/v1` accepts Dex bearer tokens for audience `olf-cli`, which its Web UI section rejected while no caller needed it; the people catalogs and their rules |
| `identity-callers.md`, `identity-classes.md` | `olf query` becomes a class-1 (human) caller from the workstation through the ingress; the workload path stays class 2 |
| `docs/reference/roles-and-permissions.md` | The data-access matrix |

## Build sequence

Split along the seams `AGENTS.md` names, each with its tests:

1. Role model: `data_access` in `release/identity-roles.yaml`, the schema,
   `olf` validation (monotonic, three shapes). Contract only; nothing reads it.
2. Storage: an STS role in SeaweedFS for Polaris, and stage catalogs created
   with STS available (`stsUnavailable: false`, `roleArn`, `stsEndpoint`,
   `endpoint`, `pathStyleAccess`), with the credential duration of section 7.
   Verification items 1-2 passed.
3. Polaris: `mixed` authentication, the rendered mapper, and the bootstrap
   objects of section 5 except the namespace grants, with `olf-signed-in`.
   Items 3 and 4 passed; settle items 5-6 first.
4. `olf catalog sync-namespaces` grants `olf-gold-read` on Gold namespaces;
   the deployer's `CATALOG_MANAGE_ACCESS`.
5. Trino: `PASSWORD,JWT`, the people catalogs with vended credentials, the
   rules of section 4, step 6.
6. Dex `olf-cli` client and the `query_api` route and perimeter instance.
7. `olf query` person path, `--as-workload`, the token cache, and `olf e2e`
   cases: per role through the stub GitHub users the e2e already runs, an
   `analyst` denied DEV Silver, a `viewer` denied DEV Gold, a
   `data-engineer` writing DEV and denied writing PROD, a `platform-admin`
   writing PROD; with Dex scaled to zero, the person path fails closed and
   `--as-workload` still answers; a vended read credential cannot write, nor
   read another table's location.
8. Docs of section 8 not already landed.

## Verification before building

The **T** items that could change the design, to run on kind first:

1. **Passed** (proof below). SeaweedFS 4.48 limits S3 requests signed with
   `AssumeRole` credentials to the session policy (section 3, finding 3).
2. **Passed** (proof below). Polaris 1.4.0 vends credentials through
   SeaweedFS's STS endpoint for a catalog with STS available, and still serves
   a request without delegation.
3. **Passed** (proof below). Polaris 1.4.0 trusts the local CA for the Dex
   JWKS (ADR 0015 to-verify), with discovery off.
4. **Passed** (proof below). Dex v2.46.0 access tokens carry `email`,
   `groups` and `federated_claims` for the scopes of step 1, with `aud` equal to
   `olf-cli`, from the GitHub connector.
5. Polaris refuses the token exchange of Trino's unsigned JWT (step 8), so a
   missing extra credential fails closed rather than falling back to
   `trino-people-<stage>`.
6. Catalog-level grants apply to every namespace and table, and a Gold-only
   role can run `SHOW SCHEMAS` with `NAMESPACE_LIST`.
7. `session-timeout=0s` builds a fresh session per request with a refreshed
   token (step 9).
8. Trino 480's people catalog reads and commits with vended credentials and
   no static S3 key (section 3, finding 4).
9. oauth2-proxy applies `--allowed-group` to a bearer session (step 4).
10. A device-flow sign-in can be driven against the e2e's stub GitHub.
    **Shown locally** (item 4 proof drives the device flow through the stub
    unchanged); wiring it into `olf e2e` remains.

With vended credentials, the earlier question of whether some Trino read path
skips `loadTable` no longer decides access: without a table's credentials no
file of it can be read.

### Proof: SeaweedFS STS session policy (item 1)

A throwaway script, run locally and not kept in the repository, downloaded the
SeaweedFS image pinned in `release/component-catalog.yaml` by digest, checked
the index, the linux/amd64 manifest and every layer against their sha256, and
ran its `/usr/bin/weed` as one `weed server` process with an S3 identity file
and an advanced IAM file (`-s3.iam.config`). No cluster, no Docker. Any
registry serving the pinned digest is equivalent under that check; Docker Hub
rate-limited the anonymous layer downloads, so the 4.48 run fetched it from
Google's Docker Hub mirror (`mirror.gcr.io`). The IAM file defined one role, `polaris-vending`, with
`s3:*` on the bucket, trusted only by the identity `polaris`
(`arn:aws:iam::000000000000:user/polaris`, SeaweedFS's fixed account id). The
script called `AssumeRole` as `polaris` with the session policy Polaris 1.4.0
builds for a table (`AwsCredentialsStorageIntegration.policyString`: `GetObject`
and `GetObjectVersion` on `<table>/*`, `ListBucket` with an `s3:prefix`
condition, `GetBucketLocation`, and `PutObject`/`DeleteObject` for a write
location; 405 characters for one table).

Run on 2026-10-30 against `chrislusf/seaweedfs:4.48` (version `30GB 4.48
530be3e37`, fetched through `mirror.gcr.io`): exit 0, all 16 cases passing. The
same 16 cases had passed on the earlier pin, 4.23 (`30GB 4.23 73fc9e383`). Each case expects one exact
result (allowed, SeaweedFS's `AccessDenied`, or `InvalidAccessKeyId` for the
tampered token), so an unrelated error such as `NoSuchKey` fails it rather
than passing as a denial.

| Case | Request | Result |
| --- | --- | --- |
| r1 | read credential, GET an object of its table | allowed |
| r2 | read credential, GET another namespace's table | denied |
| r3 | read credential, GET `sales_gold/orders_archive`, a sibling whose path starts with the table's | denied |
| r4 | read credential, PUT in its table | denied |
| r5, r6, r7 | read credential, LIST its table's prefix; another table's prefix; the whole bucket | allowed; denied; denied |
| w1, w2 | write credential, PUT and DELETE in its table | allowed |
| w3, w4 | write credential, PUT in and GET from another table | denied |
| c1 | control: credential with no session policy, GET another table | allowed (the role allows it; the session policy is what restricts) |
| c2 | read credential with a tampered session token | `InvalidAccessKeyId` |
| a1 | `AssumeRole` by an identity neither trusted by the role nor granted `sts:AssumeRole` | denied |
| s1, s2 | a static identity with the legacy `Read` action, with STS enabled: GET any table; PUT | allowed; denied |

What the run settles for build step 2: STS needs SeaweedFS's advanced IAM file
beside the identity file, holding the STS signing key, the role, its trust
policy and its attached policy, so the signing key is one more in-cluster
Secret. The caller needs `sts:AssumeRole`; the proof gives `polaris` the legacy
`Admin` action, and a narrower grant for the platform's Polaris identity is
for step 2 to settle (T). The workloads' static keys are unaffected (s1, s2).

**Not proved:** the platform's SeaweedFS Helm deployment (separate master,
volume, filer and S3 components) with this configuration; credential expiry,
since the shortest duration is 15 minutes. Polaris calling the STS endpoint is
item 2, below.

### Proof: Polaris vends through SeaweedFS STS (item 2)

Run locally on 2026-10-30, not kept in the repository. It started SeaweedFS
4.48 configured as in item 1, and the Polaris 1.4.0 application taken from the
pinned `apache/polaris:1.4.0` image (every manifest and layer checked against
its digest), run on the host's Java 21 with in-memory persistence. Polaris's
static storage key was the SeaweedFS identity `polaris`, which the role trusts.
The root principal created catalog `lakehouse_dev` with S3 storage at
`s3://gold` (`roleArn` of the item 1 role, `endpoint` and `stsEndpoint` both
the SeaweedFS S3 port, `pathStyleAccess: true`, `stsUnavailable: false`), and
tables `sales_gold.orders` and `hr_gold.salaries`, whose metadata Polaris wrote
itself. Three principals each held one grant on the `sales_gold` namespace,
with `NAMESPACE_READ_PROPERTIES`. A delegated load sends
`X-Iceberg-Access-Delegation: vended-credentials`. All 12 cases passed:

| Case | Request | Result |
| --- | --- | --- |
| p1 | `TABLE_READ_DATA`: delegated load of `sales_gold.orders` | 200 with `s3.access-key-id`, `s3.secret-access-key`, `s3.session-token`, `s3.session-token-expires-at-ms` (60 minutes), `s3.endpoint`, `s3.path-style-access` |
| p0 | that key compared with Polaris's own static key | different |
| p2, p3, p4 | that credential: GET its table's data file; GET `hr_gold.salaries`'s; PUT in its table | allowed; `AccessDenied`; `AccessDenied` |
| p5 | `TABLE_READ_DATA`: load without delegation, as the workload catalogs do | 200 with no credentials |
| p6 | `TABLE_READ_DATA`: delegated load of `hr_gold.salaries`, which it has no grant on | 403 |
| p7, p8 | `TABLE_READ_PROPERTIES` only: delegated load; load without delegation | 403; 200 |
| p9 | `TABLE_WRITE_DATA`: delegated load | 200 with credentials |
| p10, p11 | that credential: PUT in its table; PUT in `hr_gold.salaries` | allowed; `AccessDenied` |

What the run settles: a catalog with STS available still answers a load
without delegation (p5, p8), so the workload catalogs keep working when step 2
turns STS on; the grant decides whether credentials are vended at all (p7);
and the vended credential is a SeaweedFS STS credential scoped to the table
(p0, p3, p11), not Polaris's own key. The default one-hour lifetime is longer
than this design assumed; section 7 records the setting that shortens it.

**Not proved:** Polaris on the platform's PostgreSQL persistence and Helm
values; an engine (Trino or DuckDB) reading and committing with the vended
credentials (item 8); the shorter `STORAGE_CREDENTIAL_DURATION_SECONDS`
accepted end to end.

### Proof: Polaris trusts the local CA for Dex (item 3)

Run locally on 2026-10-30, not kept in the repository. Dex v2.46.0 and the
Polaris 1.4.0 application, both from their pinned images checked against their
digests, ran on the host. Dex served HTTPS on `localhost` with a certificate
from a throwaway CA, with its local password connector and two static users
whose `groups` stand in for GitHub teams: alice in `acme:analysts`, mapped to
`olf-analyst`, and bob in `acme:interns`, mapped to nothing. Tokens came from
Dex's password grant to client `olf-cli`. Three Polaris instances in `mixed`
mode used the configuration of section 4, step 10: **as designed** (roles from
`groups` only), **fixed** (step 11 as corrected here), and a **control** with no
CA. In each, the root principal created principal `local` (the password
connector's id) holding `olf-analyst` (`NAMESPACE_LIST` on `lakehouse_dev`),
`olf-platform-admin` (`CATALOG_MANAGE_CONTENT`) and `olf-signed-in` (nothing).

Alice's access token carried `aud: olf-cli`, `email`, `groups:
[acme:analysts]` and `federated_claims: {connector_id: local, ...}`. 16 cases;
the two marked **escalation** are the as-designed configuration failing as
expected once the fault was found:

| Case | Request | Result |
| --- | --- | --- |
| o1 | Polaris starts in `mixed` against Dex on HTTPS, discovery off, CA from the named TLS configuration | ready |
| o2, o2b | as designed, alice: list namespaces; create one (needs `olf-platform-admin`) | 200; 403 |
| o3 | as designed, bob (group maps to no role): create a namespace | 200, **escalation** |
| o3c | as designed, alice's token requested without the `groups` scope: create a namespace | 200, **escalation** |
| f2, f2b | fixed, alice: list namespaces; create one | 200; 403 |
| f3, f3b, f3c | fixed, bob: list; create; alice without `groups`: create | 403; 403; 403 |
| o4 | token Dex issued to another client (`aud` not `olf-cli`) | 401 |
| o5 | alice's token with a tampered signature | 401 |
| o6 | Polaris's own root token in `mixed` | 200 |
| o7 | control (no CA), alice | 500 |
| o8 | Dex stopped: Polaris's root token | 200 |
| o9 | control's log | names a certificate-path (`PKIX`) failure |

Polaris's authentication log resolved, as designed, `roles=[olf-analyst]` for
alice and `roles=[]` for bob, after which the resolver activated every role
`local` holds. Fixed, it resolved `[olf-signed-in, olf-analyst]` and
`[olf-signed-in]`.

What the run settles: the named TLS configuration is what lets Polaris fetch
Dex's keys (o2 against o7, o9), with discovery off; the audience check refuses
tokens minted for other clients (o4); Polaris's own credentials keep working
with Dex down (o8), so pipelines stay off the issuer's availability path; and
the shared-principal design needs `olf-signed-in` (section 4, step 11).
Without the CA, Polaris answers 500 rather than 401: it still refuses.

**Not proved:** `auth.<base_domain>` reached through the cluster's DNS
rewrite; Polaris's JVM trust in a pod with the CA mounted from the
`olf-local-ca` ConfigMap. The GitHub connector is item 4, below.

### Proof: Dex's GitHub connector tokens for `olf-cli` (item 4)

Run locally on 2026-10-30, not kept in the repository. The repository's own
stub GitHub Enterprise (`tools/olf/olf/e2e/github_stub.py`, its handler
imported unchanged) served HTTPS with a throwaway CA. The pinned Dex v2.46.0
(its image checked against the digest) used the GitHub connector exactly as
`modules/identity/dex/main.tf` renders it (`orgs` with the mapped team slugs,
`teamNameField: slug`, `loadAllGroups: false`, `useLoginAsID: false`,
`hostName` and `rootCA`, the module's expiry settings), and stored its state in
PostgreSQL, as the platform does. The client was step 1's `olf-cli`: public,
device authorization grant, scopes `openid email groups federated:id
offline_access`. The browser side answered the stub's authorize redirect with
`code=<login>`, as the e2e's `StubAuthorize` does. GitHub users: alice in
`acme/analysts` (mapped); carol in `acme/data-eng` (mapped) and
`acme/book-club` (not); bob in `acme/interns` (not). All 13 cases passed:

| Case | Request | Result |
| --- | --- | --- |
| g1-g6 | alice, device flow | tokens; access token `aud: olf-cli`, `email`, `groups: [acme:analysts]`, `federated_claims.connector_id: github`; a refresh token |
| g7 | carol, device flow | `groups: [acme:data-eng]`: an unmapped team is left out |
| g8 | bob, device flow | browser refused (Dex's `/callback`, 500, "not in required orgs or teams"); the device code stays `authorization_pending`, no token |
| g9, g10 | alice without the `groups` scope | no `groups` claim; `federated_claims.connector_id` still `github` (the input item 3's `olf-signed-in` relies on) |
| g11 | alice refreshes | `groups` re-read from GitHub: `[acme:analysts]` |
| g12 | alice removed from `analysts` at GitHub, next refresh past the 3-second reuse interval | refused (`invalid_request`) after Dex re-read her user, membership and teams |
| g13 | refresh with a token Dex returned inside the reuse interval, after it | answered 200 |

What the run settles: the device flow works for a public `olf-cli`, every claim
the design reads is present and named as assumed, only mapped teams reach
`groups`, and team removal ends the session at the next refresh. A refused
person's device code never resolves, which `olf query` has to time out on
(section 6). Inside the 3-second reuse interval a refresh returns the previous
answer, groups included: removal takes effect from the first refresh after it,
which the perimeter's 4-minute cadence always is.

Run first with Dex's `memory` and then `sqlite3` storage, a second chained
refresh hung with nothing logged and no call to GitHub; on PostgreSQL the same
sequence answered. The platform runs Dex on PostgreSQL, so this is recorded
only as a constraint on local reproductions; its cause was not traced.

**Not proved:** real github.com (the stub stands in for GitHub Enterprise);
GitHub's own rules on team visibility and nested teams.

## Superset 6.1.0: per-user propagation (carried forward)

Not part of this decision; kept as the record for the Superset follow-up.

| # | Claim | Mark | Reference |
| --- | --- | --- | --- |
| 1 | A database has an `impersonate_user` flag; the imported Trino connections set it `false` | V | `superset/models/core.py`; `lakehouse_code/dashboards/superset/*/databases/openlakeforge_trino.yaml` |
| 2 | When set, the Trino engine spec puts the effective username in the driver's `user` connect argument | V | `superset/db_engine_specs/trino.py`, `impersonate_user` |
| 3 | The effective user is the logged-in user's name from the request context, not client input; with no request context it falls back to the connection URL's user, the shared principal | V | `core.py`, `Database.get_effective_user` |
| 4 | `IMPERSONATE_WITH_EMAIL_PREFIX` swaps the name for the part of the e-mail before `@` | V | `core.py`, `_get_sqla_engine` |
| 5 | The `trino` driver sends its `user` argument as `X-Trino-User` | T | driver not read |
| 6 | The Trino engine spec sets `supports_oauth2`. With a per-database OAuth2 configuration, the user's token for that database is sent as `Authorization: Bearer` alongside the user name | V | `trino.py`, `impersonate_user`; `core.py`, `get_oauth2_access_token` |
| 7 | That token comes from a flow configured on the database (`oauth2_client_info`), not from the SSO session | T | `core.py` read; no deployment |
| 8 | Result caching keys on the user only when the database impersonates **and** `CACHE_IMPERSONATION` is on, or `CACHE_QUERY_BY_USER` is on, or the database sets `per_user_caching`. `CACHE_IMPERSONATION` defaults to `False` | V | `superset/common/query_object.py`; `superset/config.py` |

Turning on per-user access in Superset without per-user caching serves one
user's cached result to another (claim 8). Cache warm-up, alerts and reports
run as an executor identity (`CACHE_WARMUP_EXECUTORS`, `ALERT_REPORTS_EXECUTORS`)
whose Trino identity is not established. Both must be settled by that
follow-up.

## Rejected alternatives

| Option | Why not |
| --- | --- |
| Trino file-based rules keyed by canonical role (this record's previous decision) | Trino 480 reads no groups from a token (finding 1), so roles would reach it only through a group file synced from GitHub, and a stage-scoped user name would be needed to keep DEV and PROD apart. Policy in one engine would also have to be rewritten for every other engine (section 3); DuckDB has no rules to write it in. Its one advantage, working on Glue too, is out of scope |
| The engine reads the person's tables with its own storage key | Polaris would then decide only metadata, and the guarantee would depend on each engine never exposing a path to its key. Acceptable for one locked-down server, not as the rule every engine must meet (section 3) |
| `session=USER` on the existing `lakehouse_<stage>` catalogs | Every workload caller would lose its catalog access: with no `token` credential the Iceberg client exchanges Trino's unsigned JWT, which Polaris does not accept (step 8) |
| One Polaris principal per person | Needs a job copying GitHub team membership into Polaris principals, with revocation as late as that job runs. The token's `groups` already carry the role, and Polaris intersects them with the grants, provided the role set is never empty (finding 3, `olf-signed-in`) |
| One Polaris principal per role, chosen by Trino | A Trino catalog has one catalog credential and cannot pick one per user |
| Polaris `external` authentication | Disables Polaris's own token endpoint, which the bootstrap Job and every workload use (ADR 0015) |
| A workload principal impersonating the person (`X-Trino-User`) | The workstation would hold a service password, and Trino would trust the client's claim of who the person is |
| Keep `kubectl exec`, mapping Kubernetes RBAC to roles | Kubernetes RBAC is not the role model and needs a kubeconfig per person (ADR 0014's boundary between roles and infrastructure access) |

## Consequences

- No code, Terraform or contract changes in this record; the build sequence
  above makes them.
- Once built, a person's role is enforced below Superset for `olf query`, on the
  local provider with the GitHub issuer, and nowhere else.
- Adding a query engine adds an adapter (token forwarding and vended
  credentials), never a policy.
- Human queries depend on Dex; pipelines, dashboards and the
  `--as-workload` path do not.
- The ADR 0016 `principals` rules stay, with one rule added for e-mail
  principals, until they are replaced by `impersonation` rules.

## History

New record (#179): the deferral, the Trino 480 and Superset 6.1.0 assessments,
the foreclosure review and the prerequisites.

Rewritten as the design for #367. The decision to enforce per-user policy in
Trino with rules keyed by canonical role is replaced by Polaris grants reached
through Dex tokens, because Trino 480 reads no groups from a token and Polaris
does; AWS and Glue are left out by decision. Absorbed: the Trino 480
impersonation analysis (now a rejected alternative), the foreclosure review
(its accepted costs are in "Limits accepted"), and the nine prerequisites (now
the build sequence and the verification list). The Superset assessment is
carried forward unchanged.

Engine neutrality (section 3): the design must hold for DuckDB, planned as a
second engine run in the cluster as a server, which has no catalog rules or
roles. Added the rule that no engine carries roles, and vended credentials on
the person path so Polaris also decides file access; this adds SeaweedFS STS
and two verification items. Settled the open questions: `uat` takes the PROD
column for every role, `viewer` included, and the Trino people catalogs are
named `<stage>`.

Build step 1: `data_access` in `release/identity-roles.yaml`, rendered into
`shared.identity.roles` by every root and validated by `olf` (vocabulary,
monotonic on every stage and layer, the three catalog-role shapes); the
provider-contract schema gains the section.

SeaweedFS pin moved from 4.23 to 4.48 for the S3 gateway's read-buffer
retention fix. Finding 3 was re-read at tag `4.48`: `AssumeRole` still takes a
`Policy` through `sts.NormalizeSessionPolicy` and still allows 15 minutes to 12
hours unless an STS `MaxSessionLength` is configured, which this stack does not.

Verification item 1 run against the pinned SeaweedFS binary, 16 cases passing
on 4.23 and again on 4.48; section 3, finding 3 is now verified by a run, and
the proof records what build step 2 must configure. The script that ran it was
removed from the repository afterwards: proofs for this design are run
locally and only their results are recorded here.

Verification item 2 run locally against the pinned Polaris 1.4.0 and SeaweedFS
4.48, 12 cases passing; section 3, finding 2 is now verified by a run. It found
that Polaris vends for one hour by default, which section 7 now records with
the setting build step 2 lowers it with.

Verification item 3 run locally against the pinned Dex v2.46.0 and Polaris
1.4.0: Polaris trusts a local CA for Dex's keys through a named TLS
configuration with discovery off. The run found that Polaris activates every
role of a principal whose token maps to none, which made the shared `github`
principal grant platform-admin to a token without a mapped group, including one
requested without the `groups` scope. Finding 3 and section 4, step 11 now add
`olf-signed-in`, read from the always-present connector id, and the fix was run
in the same proof.

Verification item 4 run locally against the pinned Dex v2.46.0 with its GitHub
connector configured as the module renders it, against the repository's stub
GitHub: `olf-cli` as a public device-flow client receives `aud`, `email`,
`groups` and `federated_claims` as the design reads them, unmapped teams stay
out of `groups`, and a removed team member is refused at the next refresh. The
device flow never resolves for a refused person, which section 6 now handles.
