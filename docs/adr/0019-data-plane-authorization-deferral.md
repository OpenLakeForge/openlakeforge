# ADR 0019: Data-plane authorization — Dex identity, Polaris grants

## Status

Decided, **build steps 1 to 4, 6 and 7 (`olf query`) of 8 landed** (tracked in #674): this is the
design for #367. Step 1 puts `data_access` in the role model, validated by
`olf`. Step 2 gives SeaweedFS an STS role for Polaris and creates new stage
catalogs with STS available. Step 3 makes Polaris accept people's Dex tokens
and creates the principal, roles and catalog grants of section 5, except the
Gold namespace grants. Step 4 makes `olf infra catalog sync-namespaces` grant
those, with the deployer's added privilege. Step 6 routes Trino's `/v1` to the
`viewer` floor, and step 7 adds `olf query`; Trino accepts a person's token
from step 5. Verification items 1 (SeaweedFS STS enforces the session policy),
2 (Polaris vends table-scoped credentials through it), 3 (Polaris trusts the
local CA for Dex) and 4 (Dex's GitHub connector issues the tokens the design
needs to `olf-cli`) passed. Item 3 found that Polaris activates **all** of a
principal's roles when a token maps to none; section 4, step 11 is corrected
for it. **Binding today:**
v0.5-alpha has service-level authorization only, and nothing in it restricts
which tables a signed-in user reads (Context). **Decided for the build:**
`olf infra query` stops running through `kubectl exec` as the stage's shared
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

`olf infra query` today (`olf/infra/commands/query.py`, `olf/infra/trino.py`) runs the Trino CLI
inside the coordinator pod with `kubectl exec`, as `olf-<stage>-runtime`, the
stage principal that also writes. Who may query is who holds `pods/exec` on the
coordinator; the person's role plays no part, and every caller is the same
Trino user.

The goal is a per-person query path: a person runs `olf infra query` from a
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
| 3 | The roles active on a request are the token's mapped roles that the principal was also granted in Polaris; a role the token does not carry is not active. **Except when the token maps to no role at all:** the authenticated principal then has an empty role set, and the resolver activates every role the principal was granted. Section 4, step 11 keeps the set from ever being empty | V, run (verification item 3) | `DefaultAuthenticator.resolvePrincipalRoles`; `Resolver.resolveCallerPrincipalAndPrincipalRoles` (`getRoles().isEmpty() ? resolveAllPrincipalRoles(...)`) at `apache-polaris-1.4.0` |
| 4 | Polaris catalogs are already per stage (`lakehouse_<stage>`, one per stage catalog in the bootstrap), so a grant on a catalog is a grant on a stage. The stage binding needs no rewritten Trino user name, which the previous version listed as an open prerequisite | V | `modules/catalog/polaris/templates/bootstrap.sh.tftpl` |
| 5 | Iceberg data files are read with Trino's own S3 key and vended credentials are off, so Polaris does not gate file reads directly. It gates the table metadata: `LOAD_TABLE` needs `TABLE_READ_PROPERTIES`, `UPDATE_TABLE` needs `TABLE_WRITE_PROPERTIES`, a direct create `TABLE_CREATE`. Without the metadata Trino has no file to read and no commit to make. Section 3 replaces that key with Polaris-issued credentials on the person path | V (operations); T (no read path in the Iceberg connector skips `loadTable`) | `PolarisAuthorizableOperation`; `modules/query/trino/main.tf` |

So the role check lives where the role claim can be read without a sync, and
the stage boundary is the catalog boundary that already exists. The cost is
the one ADR 0015 named: table policy written against Polaris does nothing on
Glue. **Accepted:** AWS gets no per-person query path from this design. AWS's
`olf infra query` already refuses the provider (`commands/query.py`), and Glue with
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
| 2 | Polaris vends S3 credentials through STS `AssumeRole` with a session policy scoped to the table, signing the call with its own static storage key. A catalog's S3 storage configuration names the STS endpoint (`stsEndpoint`), so a non-AWS STS works. Credentials last `STORAGE_CREDENTIAL_DURATION_SECONDS` (default one hour) and are cached for `STORAGE_CREDENTIAL_CACHE_DURATION_SECONDS` (default 30 minutes). The bootstrap creates every stage catalog with `stsUnavailable: true`, so nothing is vended today | V, run (verification item 2) | `AwsCredentialsStorageIntegration`, `FeatureConfiguration` at `apache-polaris-1.4.0`; `modules/catalog/polaris/templates/bootstrap.sh.tftpl` |
| 3 | SeaweedFS 4.48 implements STS `AssumeRole` and accepts a session `Policy` of at most 2048 characters, with durations from 15 minutes to 12 hours. A request signed with the issued credentials is allowed only when the role's attached policies **and** the session policy both allow it; a request without a session token keeps the legacy authorization of its static identity | V, run (verification item 1) | `weed/s3api/s3api_sts.go`, `weed/iam/integration/iam_manager.go`, `weed/s3api/auth_credentials.go` (`VerifyActionPermission`, `authorizationRoute`) at tag `4.48` |
| 4 | Trino 480 asks for vended credentials per REST catalog with `iceberg.rest-catalog.vended-credentials-enabled`, which sends `X-Iceberg-Access-Delegation: vended-credentials` | V (property and header); V, run (verification item 8) | `IcebergRestCatalogConfig`, `TrinoIcebergRestCatalogFactory` |
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
    participant O as olf infra query
    participant D as Dex
    participant X as Perimeter (query_api)
    participant T as Trino
    participant C as Polaris
    participant S as SeaweedFS
    P->>O: olf infra query --stage prod "SELECT ..."
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
| 1 | Dex client | A new public static client `olf-cli` with the device authorization grant. Scopes `openid email groups federated:id offline_access`: `email` names the Trino user, `groups` carries the roles, `federated:id` the connector id (step 11), `offline_access` a refresh token. Registered as a static client with `public: true` and the one redirect URI `/device/callback` | V, run (verification item 4) |
| 2 | Token cache | `<OLF_HOME>/auth/oidc-<base_domain>.json`, mode 0600, beside the existing `olf/infra/auth/state.py` provider state. Refreshed when the access token has under 60 seconds left; Dex's session policy bounds it (ADR 0014: tokens 5 min, refresh idle 30 min, absolute 10 h) | — |
| 3 | Transport | `olf` speaks Trino's HTTP protocol with `requests` (already a dependency): `POST https://trino.<base_domain>/v1/statement`, then each `nextUri`. Every request carries `Authorization: Bearer` with a token refreshed if due; the submission also carries `X-Trino-Extra-Credential: token=<the same token>` and `X-Trino-Catalog` set to the stage's people catalog. Issuer and Trino URLs come from the profile's `spec.access.base_domain`, so no kubeconfig is needed. The local CA is trusted the way `olf infra access trust` already prints | T (protocol use) |
| 4 | Perimeter | A route `query_api`: host `trino.<base_domain>`, path prefix `/v1`, behind a perimeter instance that accepts a bearer JWT for audience `olf-cli` (oauth2-proxy `--skip-jwt-bearer-tokens` with `--oidc-extra-audience=olf-cli`) at the `viewer` floor. The existing `query` route (the Web UI) keeps the `data-engineer` floor. The service passwords (ADR 0016) then cannot be used through the ingress without also holding a member's Dex token | V, run (verification item 9) |
| 5 | Trino authentication | `http-server.authentication.type=PASSWORD,JWT`. JWT: `key-file=https://auth.<base_domain>/keys`, `required-issuer`, `required-audience=olf-cli`, `principal-field=email`. A token minted for the perimeter, Superset, OpenMetadata or the Trino UI client carries another audience and is refused | V (properties); T (Dex's `aud` is the requesting client id, read in Dex's scope documentation; JWKS fetch through the CoreDNS rewrite) |
| 6 | Trino rules | A user containing `@` (an e-mail) may use the people catalogs only, with `allow: all` (Polaris decides). Service principals keep their catalogs. A `principals` rule lets an e-mail principal become only itself, before ADR 0016's final deny. A `queries` rule lets a person view and kill only their own queries: today `rules.json` has no `queries` section, so every user may view every query | V (no `queries` rules means all users view all queries: Trino 480 file-based access control); T (rules) |
| 7 | People catalog | One more Trino catalog per stage on the same Polaris warehouse, named `<stage>` (`dev`, `uat`, `prod`): `iceberg.rest-catalog.session=USER`, `iceberg.rest-catalog.session-timeout=0s`, `iceberg.rest-catalog.vended-credentials-enabled=true` with no static S3 key (section 3), and an OAuth2 credential of a Polaris principal `trino-people-<stage>` that holds no grant. `olf infra query` makes it the session default, so the name shows only in fully qualified SQL (`prod.order_revenue_gold.mart_order_revenue_by_day`). It is Trino connection plumbing, not policy: it exists because `session=USER` is all-or-nothing per catalog (rejected alternatives) | V (properties) |
| 8 | Token to Polaris | With `session=USER` Trino passes the user's extra credentials to the Iceberg client and adds an unsigned JWT of its own as `JWT_TOKEN_TYPE`. The Iceberg client uses a `token` credential as the bearer **before** trying any token exchange. So the person's Dex token reaches Polaris; a request without it falls through to exchanging Trino's unsigned JWT, which Polaris does not accept (ADR 0015), and fails closed | V (`TrinoRestCatalog.convert`; `OAuth2Manager.maybeCreateChildSession`); V, run (verification item 5) |
| 9 | Session cache | The Iceberg session is cached per `<user>-<source>` with `expireAfterAccess(session-timeout)`, the default one hour. A person querying more often than that would keep presenting their first, expired token, so the people catalog sets the timeout to zero and builds the session from the current token each time. Wrapping a bearer token makes no network call | V (`AuthSessionCache`, `IcebergRestCatalogConfig`: `@MinDuration("0ms")`); V, run (verification item 7) |
| 10 | Polaris authentication | `mixed`: Polaris's own tokens first, then Quarkus OIDC. `quarkus.oidc.auth-server-url` `https://auth.<base_domain>`, `discovery-enabled=false` with `jwks-path=keys` (as Trino's UI does, because Dex is served by Traefik), the local CA through a named TLS configuration (`quarkus.oidc.tls.tls-configuration-name`, `quarkus.tls.<name>.trust-store.pem.certs`), `quarkus.oidc.token.audience=olf-cli`. Principal mapper `name-claim-path: federated_claims/connector_id`; roles from `groups` and `federated_claims/connector_id` (step 11) | V, run (verification item 3) |
| 11 | Polaris principal | Every Dex token from the GitHub connector names the principal `github` (the Dex module's connector id). The bootstrap creates it and grants it the four role principal roles and `olf-signed-in`, a principal role with no catalog role. The token's `groups` decide which canonical roles are active (finding 3). Its connector id is also read as a role and mapped to `olf-signed-in`, so the active set is never empty: without that, a token whose groups map to nothing, including one requested without the `groups` scope, which the person chooses in their own sign-in, would activate all four roles, platform-admin included (verification item 3). Revocation is Dex's: a person removed from a team is refused at their next refresh, within 5 minutes, with no Polaris change | V, run (verification items 3 and 4; connector id `github` in `modules/identity/dex/main.tf`) |

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
| Catalog role `olf-gold-read` | each `lakehouse_<stage>` | `NAMESPACE_LIST` on the catalog; on **each Gold namespace**: `TABLE_READ_DATA`, `NAMESPACE_READ_PROPERTIES`, `VIEW_READ_PROPERTIES` | catalog grant: bootstrap, platform; namespace grants: `olf infra catalog sync-namespaces`, artifacts |
| Principal `trino-people-<stage>` | realm | No catalog role. The people catalog's own credential, used for the configuration call | bootstrap, platform |
| Principal-roles mapper | Polaris chart values | `quarkus.oidc.roles.role-claim-path` `groups,federated_claims/connector_id`; `filter` matching every claim value in `role_mapping` and the connector id; one mapping per claim value, `^<quoted value>$` to `PRINCIPAL_ROLE:olf-<role>`, plus `^github$` to `PRINCIPAL_ROLE:olf-signed-in`, rendered by the local root from `role_mapping`, so keyed by canonical role | Terraform, platform |

`TABLE_READ_DATA` implies `TABLE_READ_PROPERTIES` and `TABLE_LIST` in Polaris's
privilege hierarchy (V, `PolarisAuthorizerImpl` super-privileges). It is
chosen over `TABLE_READ_PROPERTIES` because read delegation needs it (section 3,
finding 1): a read grant yields read-only credentials, and only
`CATALOG_MANAGE_CONTENT` yields write credentials. Grants on a catalog apply to
everything in it, including namespaces and tables created after the grant
(verification item 6).

Gold namespaces come from the descriptors, not from a list in shared code:
`PhysicalProductNames.gold_namespace` in the domain model, resolved by the same
inventory `sync-namespaces` already uses. Granting on a namespace the artifacts
phase creates keeps rule 3 of `AGENTS.md`: the platform apply never waits for
user code. A pruned namespace takes its grants with it: Polaris's `dropEntity`
deletes every grant record on the dropped entity (V,
`AtomicOperationMetaStoreManager` at tag `apache-polaris-1.4.0`; V, run), so
`sync-namespaces --prune` revokes nothing itself. Polaris on PostgreSQL answers
a repeated grant with HTTP 500, so the command reads the role's grants and
writes only the missing ones. A stack without
`spec.identity.issuer: github` has no `olf-gold-read`; the command finds its
grants answer 404 and skips them.

**Accepted cost:** the stage deployer principal gains `CATALOG_MANAGE_ACCESS` on
its own stage catalog to make those grants. It already holds
`CATALOG_MANAGE_METADATA`, which can drop any table there; the added power is
granting within that one catalog, including to itself. The bootstrap grants it
only when people sign in (`person_access`), the one case where the role exists;
a stack without it keeps the deployer's earlier privilege. The alternative, a
nested `gold` parent namespace with one grant, means re-laying every Gold
namespace and is not worth it for this.

`NAMESPACE_LIST` at catalog level lets `analyst` and `viewer` see the names of
Bronze and Silver namespaces in `SHOW SCHEMAS`, not their tables. Without it a
Gold-only role cannot list schemas at all. Accepted. A query on
`information_schema.tables` across the whole catalog fails for these roles,
because Trino lists the tables of every schema and Polaris refuses Bronze and
Silver; filtered to a Gold schema it works (verification item 6). `olf-read`
needs no `NAMESPACE_LIST`: its grants already let it list namespaces.

### 6. `olf query`

ADR 0020 places a person's own queries on the platform surface: `olf query`,
which reaches Trino's `query_api` route and nothing in the cluster. `olf infra
query` keeps the workload path for operators; it is what this record first
called `--as-workload`.

| Behaviour | Detail |
| --- | --- |
| Sign-in | `olf login`, the platform session (ADR 0020): Authorization Code with PKCE in a browser, a mode-0600 session under `OLF_HOME`, refreshed when the ID token is due. For the Dex adapter it also asks for `federated:id`, which Dex does not advertise. A session from before `olf query` lacks `federated_claims`, and `olf query` asks for `olf login` again. A person in no mapped team is refused in the browser; the loopback wait ends after 5 minutes |
| Request | `POST https://trino.<base_domain>/v1/statement`, then each `nextUri`, with the ID token as `Authorization: Bearer` and as `X-Trino-Extra-Credential: token=`, `X-Trino-User` the token's e-mail and `X-Trino-Catalog` the stage's people catalog. Output: `table`, `csv` or `json` |
| `olf infra query` | `kubectl exec` as `olf-<stage>-runtime`, unchanged. It is the break-glass path when Dex is down (ADR 0014 lists Trino through its internal Service) and the one `olf e2e` uses for pipelines. Who may use it is still who holds `pods/exec`, a platform-admin in practice |
| No GitHub sign-in | With no `spec.identity`, or `issuer: external`, `olf query` refuses and names `olf infra query`: there is no principal mapping for an external issuer |
| Denial | Trino's message is generic ("Failed to list namespaces"); the reason is in the cause chain. `olf query` names it: a Polaris 403 ("your role does not reach it", pointing to `data_access`), an S3 `Access Denied` on a write ("your role may only read"), a catalog other than the people catalogs, a 401 (`olf login`), or a 403 at the perimeter (no role). Run against the pinned stack: each message comes from the real error |

### 7. Limits accepted

| Limit | Why it is accepted |
| --- | --- |
| A write that commits after the token expires fails at the commit. Polaris receives the token captured when the query was submitted; `olf` refreshes before submitting, so a statement has about 5 minutes to reach its commit (T) | `olf query` is for inspection and small changes. Loads belong to pipelines, which keep their workload credentials. A longer Dex token lifetime would loosen the ADR 0014 session bound for every client |
| Polaris records the principal `github` and the active roles, not the person | Trino's query log records the person (user = e-mail). Per-person principals would need GitHub membership synced into Polaris (rejected below) |
| The extra credential is the bearer: a client that holds another person's token gets that person's grants | Possession of a bearer token is the authority, as on every OIDC path. Trino keeps extra credentials out of the session it serialises into query information (V, `SessionRepresentation`), so the token is not shown in the UI or `system.runtime.queries` |
| Gold-only roles see Bronze and Silver namespace names | Section 5 |
| Superset users keep the shared stage principal | The Context paragraph stays true for Superset. A later step can point a Superset database at the people catalog with Superset's per-database OAuth2 (Superset table below, claims 6 and 7) |
| Vended credentials outlive the 5-minute Dex token: Polaris issues them for one hour by default (verification item 2), and SeaweedFS will not issue less than 15 minutes. A person removed from a team keeps reading the tables already loaded until they expire. Build step 2 sets `STORAGE_CREDENTIAL_DURATION_SECONDS` to 900 and the cache to 300, so a cached credential has at least 10 minutes left; Polaris 1.4.0 then vends 15-minute credentials (V, run) | Revocation still ends new table loads within 5 minutes; the credentials cover only tables the person could read when they were issued |
| A stage catalog created before build step 2 vends nothing. Polaris accepts a role ARN only when the catalog is created: an update that adds one is refused (`Cannot modify Role ARN`, V, run against Polaris 1.4.0) | The bootstrap leaves such a catalog as it is and says so; the person path needs the stack redeployed from scratch. No migration is built |
| Nothing for AWS or Azure | AWS uses Glue, out of scope by decision. Azure runs Polaris but has no Dex adapter yet (ADR 0014); it gains the path when it does |

### 8. What changes in other records when this is built

Each lands with the change that makes it true (`AGENTS.md`, "One change per
pull request").

| Record | Change |
| --- | --- |
| ADR 0014 | `data_access` beside `grants`; the `query_api` service row (all four roles); `olf-cli` as a fifth Dex client |
| ADR 0015 | The deferral ends for one caller: `mixed` with OIDC for people tokens. Every workload keeps Polaris's own credentials, so pipelines still do not depend on the issuer. Stage catalogs are created with STS available, and its "Data files" row changes for the person path |
| ADR 0016 | `/v1` accepts Dex bearer tokens for audience `olf-cli`, which its Web UI section rejected while no caller needed it; the people catalogs and their rules |
| `identity-callers.md`, `identity-classes.md` | `olf infra query` becomes a class-1 (human) caller from the workstation through the ingress; the workload path stays class 2 |
| `docs/reference/roles-and-permissions.md` | The data-access matrix |

## Build sequence

Split along the seams `AGENTS.md` names, each with its tests:

1. Role model: `data_access` in `release/identity-roles.yaml`, the schema,
   `olf` validation (monotonic, three shapes). Contract only; nothing reads it.
2. Storage: an STS role in SeaweedFS for Polaris, and stage catalogs created
   with STS available (`stsUnavailable: false`, `roleArn`, `stsEndpoint`,
   `endpoint`, `pathStyleAccess`), with the credential duration of section 7.
   Verification items 1-2 passed. **Landed:** the role trusts the platform
   identity Polaris signs with and allows the stage medallion buckets; the
   STS signing key is minted in the cluster with the S3 keys. Catalogs that
   already exist keep `stsUnavailable: true` (section 7).
3. Polaris: `mixed` authentication, the rendered mapper, and the bootstrap
   objects of section 5 except the namespace grants, with `olf-signed-in`.
   Items 3 and 4 passed; settle items 5-6 first. **Landed:** the bootstrap
   also revokes a catalog role a narrowed `data_access` row no longer names.
   `trino-people-<stage>` moves to step 5, where its catalog uses it.
4. `olf infra catalog sync-namespaces` grants `olf-gold-read` on Gold namespaces;
   the deployer's `CATALOG_MANAGE_ACCESS`. **Landed:** the grants are made after
   the namespaces are reconciled, on every run, so a stack that enables people's
   access later gets them on its next artifacts phase. The deployer privilege is
   added by the bootstrap and is not removed if people's access is switched off
   again.
5. Trino: `PASSWORD,JWT`, the people catalogs with vended credentials, the
   rules of section 4, step 6.
6. Dex `olf-cli` client and the `query_api` route and perimeter instance.
   **Landed:** the `olf-cli` client and the perimeter's acceptance of its
   bearer tokens already existed (`olf platform`); the step adds the
   `query_api` grant row and an ingress-only route `trino.<base_domain>/v1` at
   the `viewer` floor. It is not a contract route: a contract route is one host
   per service, so the perimeter takes it as a path ref, outside
   `protected_routes`.
7. `olf infra query` person path, `--as-workload`, the token cache, and `olf e2e`
   cases: per role through the stub GitHub users the e2e already runs, an
   `analyst` denied DEV Silver, a `viewer` denied DEV Gold, a
   `data-engineer` writing DEV and denied writing PROD, a `platform-admin`
   writing PROD; with Dex scaled to zero, the person path fails closed and
   `--as-workload` still answers; a vended read credential cannot write, nor
   read another table's location.
8. Docs of section 8 not already landed.

## Verification before building

The **T** items that could change the design. Items 1-9 were run locally
against the pinned images (Dex v2.46.0, Polaris 1.4.0, Trino 480, SeaweedFS
4.23 then 4.48), single processes outside a cluster; the scripts are not kept.

| # | Item | Result | Decision |
| --- | --- | --- | --- |
| 1 | SeaweedFS limits S3 requests signed with `AssumeRole` credentials to the session policy (section 3, finding 3) | Passed. A credential carrying Polaris's per-table policy reads only its table, not a sibling whose path starts with the table's, and cannot write; without the policy the role reaches every table. Static identities keep their legacy authorization | Step 2 gives SeaweedFS its advanced IAM file (STS signing key as an in-cluster Secret, the role, its trust policy) and Polaris's identity `sts:AssumeRole`; how narrow that grant can be is open (T) |
| 2 | Polaris vends credentials through SeaweedFS STS for a catalog with STS available, and still serves a request without delegation | Passed. Delegated loads return a SeaweedFS STS credential scoped to the table, read-only for `TABLE_READ_DATA`, writable for `TABLE_WRITE_DATA`, refused without `TABLE_READ_DATA`; loads without delegation are unchanged. Default lifetime one hour | Step 2 sets `STORAGE_CREDENTIAL_DURATION_SECONDS` to 900 (section 7) |
| 3 | Polaris trusts the local CA for the Dex JWKS, discovery off (ADR 0015 to-verify) | Passed, through a named Quarkus TLS configuration; without the CA Polaris refuses the token (500). Wrong audience and tampered tokens get 401; Polaris's own tokens keep working with Dex down. **Found:** a token that maps to no role activates every role of the principal | `olf-signed-in` (finding 3; section 4, steps 10 and 11; section 5) |
| 4 | Dex access tokens carry `email`, `groups` and `federated_claims` for step 1's scopes, `aud` equal to `olf-cli` | Passed with the GitHub connector as the module renders it, against the e2e's stub GitHub, device flow as a public client. `groups` holds mapped teams only; a refresh re-reads teams and a removed member is refused. A person in no mapped team is refused, but their device code stays pending | `olf infra query` bounds its device-flow polling (section 6). Removal takes effect from the first refresh after Dex's 3-second reuse interval |
| 5 | Polaris refuses the token exchange of Trino's unsigned JWT, so a query without the person's token fails closed (step 8) | Passed. Polaris answers the exchange with 400 and the query fails; `trino-people-<stage>`, given a grant to expose a fallback, is used only for the catalog's configuration call. With a Dex token the query runs with that person's role. Trino's message is generic; the reason is in the error's cause chain | `olf infra query` reads the cause chain (section 6) |
| 6 | Catalog-level grants apply to every namespace and table, and a Gold-only role can run `SHOW SCHEMAS` with `NAMESPACE_LIST` (section 5) | Passed. `olf-read` and `olf-write` reach Bronze, Silver, Gold and nested namespaces and tables created after the grant; `olf-read` cannot create. `olf-gold-read` lists every schema, nested ones included, and reads only Gold: Bronze and Silver tables, table lists and properties get 403. `olf-signed-in` cannot list. `information_schema.tables` over the whole catalog fails for a Gold-only role; filtered to a Gold schema it works. `olf-read` lists namespaces without `NAMESPACE_LIST` | Section 5 as designed; the `information_schema` limit is accepted (section 5) |
| 7 | `session-timeout=0s` builds a fresh session per request with a refreshed token (step 9) | Passed. With the timeout at zero each query reaches Polaris with the token it was sent with: another token under the same Trino user and source gets that token's role, and a fresh token works after the first expired. A control catalog with Trino's default timeout kept presenting the first token: the second token was ignored, and once the first expired every query failed | Step 7 as designed; the zero timeout is required |
| 8 | Trino 480's people catalog reads and commits with vended credentials and no static S3 key (section 3, finding 4) | Passed, with no S3 key in the catalog or Trino's environment. A data engineer creates, inserts, deletes and creates-as-select; an analyst reads Gold. The analyst's insert is refused by SeaweedFS with `Access Denied` on the read-only vended credential, before any commit; Bronze is refused by Polaris (403) | `olf infra query` names a role that may only read when the cause is an S3 `Access Denied` on a write (section 6) |
| 9 | oauth2-proxy applies `--allowed-group` to a bearer session (step 4) | Passed on oauth2-proxy v7.15.5 with the perimeter module's arguments, as Traefik's forward-auth calls it for `/v1/statement`: a token whose groups hold a role at or above the floor gets 202; an unmapped group or a token without the `groups` scope gets 403; another client's audience, a tampered or expired token, or none gets 401. `--oidc-extra-audience=olf-cli` and `--extra-jwt-issuers=<issuer>=olf-cli` behave the same. A token for the perimeter's own client is also admitted | Step 4 uses `--oidc-extra-audience=olf-cli`. The perimeter's own audience passing is accepted: Trino's `required-audience=olf-cli` (step 5) refuses it |

Still to run:

10. A device-flow sign-in can be driven against the e2e's stub GitHub: shown
    locally by item 4; wiring it into `olf e2e` remains.

Not covered by the local runs: the Helm deployments, `auth.<base_domain>`
through the cluster's DNS rewrite, Polaris on PostgreSQL, and real github.com.

With vended credentials, the earlier question of whether some Trino read path
skips `loadTable` no longer decides access: without a table's credentials no
file of it can be read.

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
- Once built, a person's role is enforced below Superset for `olf infra query`, on the
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

Verification items 1-9 were run locally against the pinned images; their
results and decisions are in "Verification before building". They verified
findings 2, 3 and 4 of section 3, steps 1, 4, 8, 9, 10 and 11 of section 4 and
the grants of section 5; found that Polaris vends for an hour by default
(section 7) and that a token mapping to no role activates every role of the
shared principal (fixed with `olf-signed-in`); added `olf query`'s bounded
device-flow polling and its reading of the error cause chain, Polaris's and
SeaweedFS's refusals (section 6); and accepted that a Gold-only role cannot
query `information_schema.tables` across the whole catalog (section 5).

Build step 2: SeaweedFS's credentials Job writes an advanced IAM file with the
`polaris-vending` role and a signing key it keeps across runs; the storage
contract carries the role ARN and STS endpoint; Polaris creates new stage
catalogs with STS available and vends 15-minute credentials. Polaris refuses
to add a role ARN to an existing catalog, so existing stacks keep vending
nothing until redeployed (section 7). The STS `MaxSessionLength` is now set,
to one hour.

Build step 3: Polaris runs `mixed` when the local root passes `person_access`
(`issuer: github`): Quarkus OIDC on Dex's keys, the roles mapper rendered from
`role_mapping`, and the CA from `olf-local-ca`. The bootstrap creates principal
`github`, the five principal roles and the three catalog roles, and reconciles
each role's catalog role to its `data_access` row. Polaris reaches Traefik for
Dex's keys (ADR 0017, F4). Checked against the pinned Polaris 1.4.0 and Dex:
it starts while Dex is unreachable, and a token whose groups map to no role
reaches nothing.

Build step 4: `olf infra catalog sync-namespaces` grants `olf-gold-read`
`TABLE_READ_DATA`, `NAMESPACE_READ_PROPERTIES` and `VIEW_READ_PROPERTIES` on each
Gold namespace of the stage, from the descriptors; the deployer principal gains
`CATALOG_MANAGE_ACCESS` on its stage catalog when people sign in. Polaris drops
a namespace's grants with the namespace, so pruning needs no revoke. Run against
Polaris 1.4.0 with `olf`'s client as the deployer: the grants land, a rerun plans
nothing, a recreated namespace has no grants left, and a stack without people's
access answers 404 for the role, so it is skipped.

Build step 7, `olf query`: ADR 0020 puts a person's queries on the platform
surface, so section 6 is `olf query`, signing in with `olf login` (PKCE, the
platform session) rather than a device flow of its own, and `olf infra query`
is the workload path the section called `--as-workload`. `olf login` asks Dex
for `federated:id`. The client was run against the pinned stack of step 5: a
data engineer writes, an analyst reads through Trino's paging, and the
refusals (S3 on a write, Polaris on Bronze, a workload catalog) each produce
their message.

Build step 6: the `query_api` grant row (every role) and, with `issuer: github`
and Trino at `password`, an ingress route for `trino.<base_domain>/v1` behind
the `viewer` floor; the Web UI keeps the `data-engineer` floor. Its bearer path
is the one verification item 9 ran.
