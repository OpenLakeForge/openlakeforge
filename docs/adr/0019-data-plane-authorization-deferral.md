# ADR 0019: Data-plane authorization — Polaris decides for every caller

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ (people #674, every caller #960) · AWS ✘: Glue, no per-person path, until Polaris (#681) · Azure: keyless shared coordinator; no person path until a Dex adapter (#180) |

## Context

A canonical role ([ADR 0014](0014-identity-and-authorization.md)) decides which
services a person reaches, not what they read once inside. Trino 480 cannot
read groups from a token, so a role check in Trino would need a group file
synced from GitHub; Polaris reads roles from a token claim and keeps one
catalog per stage, so a grant on a catalog is a grant on a stage. Trino once
held every stage's Polaris credential and an S3 key, which made it a second
policy point that had to authenticate its callers
([ADR 0016](0016-trino-workload-authentication.md)), and a second engine
(DuckDB, #888) has no rules of its own. So Polaris decides, every caller
presents its own Polaris identity, and an engine only forwards it. Polaris can
validate an issuer's tokens but still owns principals and grants, and its own
token endpoint serves the bootstrap and every pipeline.

## Decision

**D1. A person's data access is a matrix of canonical role, stage and
medallion layer.** `platform-admin` writes every layer of every stage;
`data-engineer` writes DEV and reads UAT and PROD; `analyst` reads Gold on
every stage; `viewer` reads UAT and PROD Gold. `uat` takes the PROD column for
every role. Bronze is not in the matrix: it is raw files with no catalog
namespace, so no Polaris grant reaches it, and its isolation is the stage's
own object-store identity. "Write" is DML and DDL on tables, views and
namespaces (Polaris `CATALOG_MANAGE_CONTENT`), never grant management: no role
administers Polaris access through SQL, and the matrix changes only by a
deploy. The matrix is monotonic, so a person in several mapped teams holds the
highest role's row. The table is in
[Roles and permissions](../reference/roles-and-permissions.md#what-each-role-reads-and-writes).
Enforced by: `release/identity-roles.yaml`.

**D2. The matrix is the `data_access` section of `release/identity-roles.yaml`,
validated fail-closed.** It is product-level and fixed, keyed by canonical role
(role → stage → layer → `read` | `write`; absent is none), and rendered into
`shared.identity.roles` by every root. `olf` rejects a role outside
`precedence`, a stage outside the stage enum, a layer other than `silver` and
`gold` (a `bronze` key included), a row that is not monotonic on every stage
and layer, and a stage row that is not one of the three shapes Polaris catalog
roles express (D5): every layer `write`, every layer `read`, or `gold: read`
alone. A fourth shape is a new catalog role, added when a role needs it.
Enforced by: `tools/olf/olf/common/provider_contracts/_v3.py`,
`docs/schema/provider-contracts.schema.json`.

**D3. Polaris is the only authority for data access, for every caller, and no
engine carries roles.** Canonical roles and the matrix exist only as Polaris
grants. An engine's configuration never names a canonical role, a team or a
layer, and a keyless catalog has no engine access rules at all, so an engine
can be added or replaced without moving any policy. Every caller, person or
workload, presents its own Polaris identity on every table request. Enforced
by: `infra/terraform/modules/query/trino/main.tf` (no rules on a keyless
catalog), ADR 0016 D2.

**D4. No engine holds a storage key: data files are read and written only with
credentials Polaris vends for the table.** The engine asks for vended
credentials (`X-Iceberg-Access-Delegation`), and Polaris issues them through
the object store's STS `AssumeRole`, scoped by a session policy to the table's
location, only when the active roles hold the data privilege: read-only for
`TABLE_READ_DATA`, writable for `CATALOG_MANAGE_CONTENT`. STS is therefore a
requirement of the object-store adapter behind Polaris, and the contract
parser refuses a Polaris stage without `sts_endpoint`. Vended credentials last
15 minutes and are cached for 5. Trino pods carry no AWS or storage key in
their environment and no service-account annotation, and the stage's
default-deny NetworkPolicy gives them no route to an instance metadata
endpoint, so the AWS SDK's default chain finds nothing. Floe is the one caller
that still reads with the stage's S3 key, without delegation. Enforced by:
`modules/query/trino/tests/keyless.tftest.hcl`, `sts_endpoint` in
`tools/olf/olf/common/provider_contracts/_v3.py`,
`STORAGE_CREDENTIAL_DURATION_SECONDS` in
`infra/terraform/modules/catalog/polaris/release.tf`.

**D5. Polaris holds the matrix as three catalog roles per stage catalog, and
Gold grants follow the namespaces.** `olf-write` holds `CATALOG_MANAGE_CONTENT`;
`olf-read` holds `TABLE_READ_DATA`, `NAMESPACE_READ_PROPERTIES` and
`VIEW_READ_PROPERTIES` on the catalog; `olf-gold-read` holds `NAMESPACE_LIST`
on the catalog and the three read privileges on each Gold namespace. Each
principal role `olf-<role>` is assigned the catalog role its `data_access` row
needs on each stage catalog, and the bootstrap revokes one a narrowed row no
longer names. Gold namespaces come from the descriptors, and `olf infra catalog
sync-namespaces` grants on them in the artifacts phase, so the platform apply
never waits for user code; a pruned namespace takes its grants with it. To make
those grants the stage deployer gains `CATALOG_MANAGE_ACCESS` on its own stage
catalog, only when people sign in. Object list:
[Roles and permissions](../reference/roles-and-permissions.md#how-polaris-holds-it).
Enforced by: `tools/olf/olf/infra/polaris_bootstrap.py`,
`tools/olf/olf/infra/catalog.py`.

**D6. A person queries as themself with `olf query`, through the stage's
`query_api` route, with their Dex token.** `olf query` signs in with `olf
login` (Authorization Code with PKCE on the public `olf-cli` client, asking
also for `federated:id`), and sends `POST
https://trino-<stage>.<base_domain>/v1/statement` with the ID token as
`Authorization: Bearer` and as the `token` extra credential, and the stage's
`lakehouse_<stage>` as the session catalog. The route sits behind a perimeter
instance at the `viewer` floor that accepts bearer tokens for the `olf-cli`
audience (ADR 0014 D7); the Web UI route keeps the `data-engineer` floor. With
no `spec.identity`, or `issuer: external`, `olf query` refuses and names `olf
infra query`. A refusal names its cause from Trino's error chain: Polaris 403
(the role does not reach it), S3 `Access Denied` on a write (the role may only
read), 401 (`olf login`), or a perimeter 403 (no role). `olf infra query` stays
the operator's workload and break-glass path, as the stage processing
principal, for whoever holds `pods/exec`. Enforced by:
`tools/olf/olf/platform/query.py`, `olf infra e2e run --env local`
(`e2e/_person_access.py`).

**D7. Every person's token names the Polaris principal `github`, its groups
decide the active roles, and `olf-signed-in` keeps the active set from ever
being empty.** With `issuer: github` Polaris authenticates `mixed` and accepts
Dex tokens for the audience `olf-cli`. The principal is read from
`federated_claims/connector_id`; roles from `groups` and the connector id,
through a mapper rendered from `role_mapping`: one mapping per claim value to
`PRINCIPAL_ROLE:olf-<role>`, plus the connector id to `olf-signed-in`, a
principal role with no catalog role. Polaris activates every granted role when
a token maps to none, platform-admin included, so the connector-id mapping is
mandatory. The mapper's regexes match each claim literally through character
classes (`acme[.]org:team`) and contain no backslash, because the chart writes
them into `application.properties`, which drops it; a claim holding `[`, `]`,
`\` or `^` is refused at plan time. Revocation is Dex's: a person removed from
a team is refused at their next refresh, within 5 minutes, with no Polaris
change. Enforced by: `infra/terraform/modules/catalog/polaris/person_access.tf`
and its `variables.tf` validation; `olf infra e2e run --env local`.

**D8. Each stage has its own Trino coordinator, holding only its own stage's
catalog.** A coordinator is reachable only from its stage namespace, Traefik,
the platform API and the OpenMetadata crawl Job (ADR 0017). Stage isolation
rests on the Polaris grant of the token each caller presents and on that
NetworkPolicy; there is no fallback credential. AWS and Azure keep one shared
coordinator until their cloud milestone. Enforced by: `module "trino"` in
`infra/terraform/environments/local/main.tf`, `release/network-flows.yaml`.

**D9. Every Trino catalog is keyless: one `lakehouse_<stage>` per stage,
`session=USER`, and a catalog credential with no grant.** The catalog sets
`iceberg.rest-catalog.session=USER`, `session-timeout=0s` (so each query builds
its session from the token it carries, not the first one cached),
`oauth2.token-refresh-enabled=false` (so an expired token reaches Polaris as
itself instead of being swapped for the catalog's credential),
`vended-credentials-enabled=true` and no static S3 key. Its OAuth2 credential,
`trino-catalog-<stage>`, holds no catalog role and serves only the
configuration call; a request without a `token` extra credential falls through
to a token exchange Polaris refuses, and fails closed. Every caller sends its
own Polaris token as the `token` extra credential. One exception: with
authentication off (no `spec.identity`, ADR 0021 decision 8) no caller holds a
token, so the catalog takes `session=NONE` and `trino-catalog-<stage>` holds
`CATALOG_MANAGE_CONTENT`; vended credentials and the absence of a key are
unchanged. Enforced by: `rest_catalog_properties` and `keyless` in
`infra/terraform/modules/query/trino`, `tools/olf/olf/infra/polaris_bootstrap.py`.

**D10. Each stage has one Polaris writer, `olf-<stage>-processing`, used by
dbt and Floe.** It holds `CATALOG_MANAGE_CONTENT` on its own stage catalog
only, never manages grants and has no grant on another stage, so DEV's
pipeline cannot write PROD. Its one Secret, `polaris-<stage>-processing-creds`,
lives in the stage namespace. dbt and Gold maintenance mint its token once per
dbt invocation, one invocation per Gold generation, and Gold models are
`view_security: invoker`. The deployer is not a data writer: it keeps
`CATALOG_MANAGE_METADATA`, and `CATALOG_MANAGE_ACCESS` only under D5.
`--allow-stage-removal` deletes a removed stage's processing, deployer and
people principals. Enforced by: `tools/olf/olf/infra/polaris_bootstrap.py`,
`olf check dbt`.

**D11. Superset queries as each viewer.** The imported database points at the
stage's `lakehouse_<stage>` on the stage's Trino with no user and no password,
`impersonate_user`, `per_user_caching`, and no background queries: cache
warm-up, alerts, reports and thumbnails stay disabled, because their executors
hold no Dex token. The sign-in token response seeds one per-user OAuth2 token
row for that database, and the access token is dropped from FAB's session
cookie. The local `admin` and the platform API's service user hold no Dex
token and administer, import and export only. Enforced by:
`images/superset/olf_trino.py`, `tools/olf/olf/infra/superset.py`,
`e2e/_superset_person.py`.

**D12. OpenMetadata governs its own users, and its crawler reads with its own
`openmetadata` principal.** OpenMetadata never queries data for the person
signed in to it; its own roles decide what a person sees there (ADR 0014
D11). The crawler mints the `openmetadata` token at start and sends it as the
`token` extra credential; that principal reads every stage catalog and writes
nothing, and the workflow enables no profiler, sampler, data-quality or usage
feature that would read rows. Enforced by:
`tools/olf/olf/infra/openmetadata/_ingestion.py`,
`tools/olf/olf/infra/polaris_bootstrap.py`.

**D13. Workloads authenticate to Polaris with Polaris's own client
credentials, never through the issuer.** Polaris runs `internal`
authentication, or `mixed` when people sign in (D7), so its own token endpoint
keeps serving the bootstrap, the deployer, the processing principals, the
crawler and the catalog credential, and pipelines keep running with the issuer
down. `external` is not used. Enforced by:
`infra/terraform/modules/catalog/polaris/person_access.tf`.

**D14. AWS moves to Polaris instead of Glue, and gets no per-person path
before it does.** Polaris RBAC and Glue IAM or Lake Formation share no
securables or principals, so table policy written against Polaris does nothing
on Glue. Until #681, `olf infra query` refuses the `aws` provider and `olf
query` needs `issuer: github`, which AWS has no adapter for. Enforced by:
`tools/olf/olf/infra/commands/query.py`; `docs/technical-debt.md`.

## Consequences

- People's queries depend on Dex; pipelines, dashboards' imports and `olf infra
  query` do not.
- Polaris records the principal `github` and the active roles, not the person;
  Trino's query log names the person, but that user is a claim (ADR 0016).
- The bearer is the authority: a client holding another person's token gets
  their grants. Trino keeps extra credentials out of `system.runtime.queries`
  and the UI.
- A write that commits after the person's 5-minute token expires fails at the
  commit; `olf query` is for inspection and small changes, loads belong to
  pipelines.
- Vended credentials outlive the token: a person removed from a team keeps
  reading tables already loaded for up to 15 minutes.
- `analyst` and `viewer` see Bronze and Silver namespace names in `SHOW
  SCHEMAS`, not their tables, and cannot query `information_schema.tables`
  across the whole catalog; filtered to a Gold schema it works.
- A stage catalog created before STS was available vends nothing: Polaris
  refuses to add a role ARN to an existing catalog, so such a stack must be
  redeployed from scratch.
- An S3-compatible store without STS (#803) cannot run the keyless engine.
- A Gold generation must build within the processing token's lifetime (one
  hour), start-up included.
- Polaris denials reach a Superset chart as Trino's generic error; Superset has
  no hook for a custom message.
- One coordinator per stage costs about 2 Gi requested per extra stage at
  `small`.

## Boundaries

| Not decided here | Where |
| --- | --- |
| Canonical roles, the issuer, the perimeter and the `olf-cli` client | [ADR 0014](0014-identity-and-authorization.md) |
| Trino authentication and its Web UI user | [ADR 0016](0016-trino-workload-authentication.md) |
| Flows to and from each stage's Trino | [ADR 0017](0017-local-network-policy-and-rbac.md), `release/network-flows.yaml` |
| The platform API's queries and the authentication-off exception | [ADR 0021](0021-platform-api.md) |
| Which surface `olf query` and `olf infra query` belong to | [ADR 0020](0020-infrastructure-common-and-platform-cli-surfaces.md) |
| How each caller reaches Trino and Polaris, and the request path | [Caller inventory](../architecture/identity/identity-callers.md#a-persons-query-through-polaris) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Trino file-based rules keyed by canonical role (this record's first decision, #179) | Trino 480 reads no groups from a token, so roles would need a group file synced from GitHub; policy in one engine is rewritten for every other, and DuckDB has none |
| The engine reads a person's tables with its own storage key | Polaris would decide only metadata, and the guarantee would depend on every engine never exposing a path to its key |
| A fallback credential with a grant on Trino's catalog (#964) | An expired or missing token would read with it; keeping the catalog credential grantless makes the fallback inert |
| One Polaris principal per person | Needs a job copying GitHub membership into Polaris, with revocation as late as the job runs; the token's `groups` already carry the role |
| One Polaris principal per role, chosen by Trino | A Trino catalog has one credential and cannot pick one per user |
| Polaris `external` authentication (#26) | Disables Polaris's own token endpoint, which the bootstrap and every workload use |
| Workloads on issuer client credentials through `mixed` (#26) | Changes every writer together and puts the issuer in every pipeline's availability path, for no user-facing benefit |
| A workload principal impersonating the person (`X-Trino-User`) | The workstation would hold a service password, and Trino would trust the client's claim of who the person is |
| `kubectl exec`, mapping Kubernetes RBAC to roles | RBAC is not the role model and needs a kubeconfig per person (ADR 0014) |
| A metadata-only grant for the OpenMetadata crawler (#963) | On a catalog that vends credentials Polaris refuses `LOAD_TABLE` without `TABLE_READ_DATA`, so the crawler would see no table |
| A nested `gold` parent namespace with one grant | Re-lays every Gold namespace to save the deployer's `CATALOG_MANAGE_ACCESS` |
| Regexes quoted with `\Q...\E` | `application.properties` drops the backslash, so they matched nothing and every token activated every role |

## History

- 2026-10-23 (#26): Polaris external identity-provider evaluation (former ADR
  0015): workloads keep Polaris's own credentials; now D13.
- 2026-10-21 (#179): New record: per-user policy deferred, to be enforced by
  Trino rules keyed by canonical role.
- 2026-10-31 (#367, #674): Rewritten: Polaris grants reached through Dex tokens
  replace Trino rules; D1 to D7 added for the person path; AWS left out (D14).
- 2026-10-31 (#674): D7's regexes use character classes with no backslash
  after a kind run found every token activating every role.
- 2026-11-08 (#979): Bronze removed from D1 and D2; the crawler's grant becomes
  read-only (D12).
- 2026-11-08 (#1011): D9 sets `oauth2.token-refresh-enabled=false`.
- 2026-11-10 (#960): Polaris decides for every caller (D3, D4 extended to
  workloads); D8 to D12 added.
- 2026-11-11 (#1052): D9's people catalog `<stage>` folded into the one
  `lakehouse_<stage>`.
- 2026-11-12 (#1061): D10 mints one token per Gold generation.
- 2026-11-18 (#1278): D9 gains the authentication-off exception.
- 2026-11-22 (#1385): Corrected to match the code; no decision changed.
- 2026-11-23: Rewritten in the standard form, absorbing ADR 0015 (now a
  stub) as D13 and two Alternatives rows. Sections 1, 2, 5 and 6 are D1, D2,
  D5 and D6; section 3's two rules are D3 and D4; section 4's Polaris steps
  (10, 11) are D7 and its catalog steps (7 to 9) D9; section 9's points 1 to 5
  are D8, D10, D9, D11 and D12, and point 6 is in D1 and D2. No decision
  changed.
