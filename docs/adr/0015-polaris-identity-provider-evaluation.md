# ADR 0015: Polaris identity-provider evaluation — deferred

## Status

Decided: **deferred**. This is an evaluation (#26), not a migration. Nothing
in the tree changes. It also records the catalog-side findings that the
data-plane authorization assessment (#179) needs.

Scope of the evidence. The deployed Polaris is the image `apache/polaris:1.4.0`
(`release/component-catalog.yaml`, `infra/helm/values/local/polaris.yaml`)
installed by Helm chart `polaris/polaris` 1.4.1, whose own `appVersion` is
1.4.1 — the image tag is overridden. Findings below come from the upstream
documentation and source at tag `apache-polaris-1.4.0`, the chart's
`values.yaml` at `apache-polaris-1.4.1`, and the Trino 480 documentation.
Nothing was run on a cluster. Statements about this repository were checked
against the tree. What was not verified is marked **to verify**.

## Context

Polaris has its own OAuth client-credentials model. Every writer to the Iceberg
catalog holds a Polaris-minted client id and secret, and the platform's human
identity work (ADR 0014) does not touch it. #26 asks whether Polaris can
instead validate tokens from the `identity.oidc` issuer, what that would cost
every caller, and whether it is worth doing in v0.5-alpha.

## What Polaris 1.4.0 supports

| Question | Finding |
| --- | --- |
| Can an external issuer authenticate principals? | Yes. `polaris.authentication.type` is `internal` (default), `external` or `mixed`, globally or per realm. `external` validates bearer JWTs with Quarkus OIDC (`quarkus.oidc.*`); `mixed` tries Polaris' own token first and falls back to OIDC. Opaque (non-JWT) tokens need a custom `PrincipalMapper` |
| Does the issuer create or own principals? | **No.** `DefaultAuthenticator` resolves the token to a principal that must already exist in the Polaris metastore, by numeric id when the id claim is present, otherwise by name; a miss is `NotAuthorizedException`. Its own javadoc: it "does not support federated principals that are not managed by Polaris". The issuer replaces authentication only. Principals, principal roles, catalog roles and grants stay Polaris data, written through the management API |
| How do issuer roles become Polaris roles? | `quarkus.oidc.roles.role-claim-path` selects the claim; `polaris.oidc.principal-roles-mapper` filters and rewrites values by regex into `PRINCIPAL_ROLE:<name>`. The roles active for a request are the token's roles that the principal was also granted in Polaris (or all granted roles for `PRINCIPAL_ROLE:ALL`) |
| How is the principal read from the token? | `polaris.oidc.principal-mapper`: `id-claim-path` and `name-claim-path` (chart: `oidc.principalMapper.idClaimPath` / `nameClaimPath`; one of the two is required). The id is parsed as a number, so a UUID `sub` cannot be the id claim; the name claim must equal an existing Polaris principal name |
| Token exchange or brokering? | Not for external tokens. Polaris' own `/api/catalog/v1/oauth/tokens` accepts `client_credentials` and `urn:ietf:params:oauth:grant-type:token-exchange`, but the exchange only re-issues a token that verifies against Polaris' own `TokenBroker` signing key (`JWTBroker.generateFromToken`). In `external` mode the token endpoint is disabled (the docs say HTTP 501; the chart says the token service is always disabled and the broker unused). No path exchanges an issuer token for a Polaris token: with an external issuer the client presents the issuer's token directly |
| Chart support | Chart 1.4.1 exposes `authentication.type`, `authentication.realmOverrides` and an `oidc` block (`authServeUrl`, `client.id`, `client.secret`, `principalMapper`, `principalRolesMapper`); defaults are `internal` and an empty `oidc.authServeUrl`. Only one default OIDC tenant is supported through values |
| What does this deployment set? | Nothing: `infra/helm/values/local/polaris.yaml` has no `authentication` or `oidc` key, so Polaris runs `internal`, and `release.tf` passes only persistence and storage settings |
| Issuer reachability | **To verify.** The issuer URL is `https://auth.<base_domain>` behind Traefik with the local CA (ADR 0014). The Polaris pod would need to resolve that host and trust the CA in its JVM trust store, as the other back-channel consumers do (`in-cluster-resolution`). Whether the 1.4.0 image honours a mounted CA bundle was not checked |
| Which claim names the principal for an issuer service-account token? | **To verify** against the pinned Keycloak. A client-credentials token's `sub` is a UUID, which cannot be the numeric id claim. A name claim can be a protocol mapper with a fixed value, or Keycloak's service-account `preferred_username`; which fits the existing principal names (`trino-<stage>`, `floe-<stage>`, ... in `environments/local/main.tf`) was not tested |

## How the Polaris credential is consumed today

Verified in the tree. Polaris has no ingress route (`service_routes` in
`infra/terraform/environments/local/contracts.tf`), so no human reaches it:
every caller below is class 2 (`identity-callers.md`).

| Caller | Credential | How it is used |
| --- | --- | --- |
| Trino, per stage catalog | The stage's Trino principal; Secret `trino_credentials_secret_name`, keys `POLARIS_TRINO_CLIENT_ID` / `_SECRET`, in the shared namespace | `iceberg.rest-catalog.security=OAUTH2`, `oauth2.credential=<id>:<secret>`, `oauth2.server-uri` = the contract `token_uri`, `oauth2.scope` = `PRINCIPAL_ROLE:ALL`, `vended-credentials-enabled=false` (`modules/query/trino`). Granted `CATALOG_MANAGE_CONTENT` on that stage's catalog |
| Floe runner | The stage's Floe principal; Secret `floe_credentials_secret_name` in the stage namespace, mounted into Dagster's user-code and run pods | The Floe profile generated by `olf floe generate-manifests` names the token URI, scope and `secret_name` (`olf/floe.py`); Floe authenticates with those credentials against that URI. `CATALOG_MANAGE_CONTENT` |
| `olf catalog sync-namespaces` | The stage's deployer principal; Secret `polaris-deployer-creds`, read through RBAC | `olf/polaris.py` POSTs `grant_type=client_credentials` with HTTP Basic to the token path over a port-forward. `CATALOG_MANAGE_METADATA` |
| Platform bootstrap Job | The Polaris root client (`random_password`, so in Terraform state — register entry "Terraform state holds the generated credential values") | Exchanges at the internal token endpoint, then creates catalogs, principals, roles and grants and writes each principal's Secret (`bootstrap.sh.tftpl`) |
| dbt | **None.** Gold is written through Trino as `olf-<stage>-runtime` | The bootstrap Job deletes the legacy `dbt` principal and `polaris-dbt-creds` Secret. #26 still lists dbt as a Polaris writer; it no longer is |
| OpenMetadata | **None used.** The bootstrap Job still creates the `om` principal, its Secret `om_credentials_secret_name` and a `CATALOG_MANAGE_CONTENT` grant on every stage catalog | The lakehouse service crawls through Trino as `openmetadata` (OpenMetadata 1.13 removed the Iceberg connector; `technical-debt.md`). No module, `olf` command or chart reads the `om` Secret: only the Polaris bootstrap and an unused field of the OpenMetadata contract variable name it. It is a dormant credential with a write-level grant |

Dagster pods receive the Polaris REST and token URIs and the scope
(`POLARIS_*` in `modules/orchestration/dagster`) but no client credential of
their own beyond the Floe Secret above.

## Blast radius if Polaris moved to the issuer

| Writer or operation | What changes |
| --- | --- |
| Trino | `oauth2.server-uri` and `oauth2.credential` point at the issuer's token endpoint and a new issuer client per stage; scope semantics change (the issuer decides roles, not `PRINCIPAL_ROLE:ALL`). Trino restarts per stage catalog. Token lifetime becomes the realm's access-token lifespan (5 min, ADR 0014); **to verify** that Trino's refresh keeps unattended queries alive across it |
| Floe | Its generated profile must name the issuer token URI and an issuer client. **To verify** that Floe's catalog client accepts a token endpoint that is not Polaris' and a different scope format; manifests are Floe-owned and must not be post-processed |
| `olf` deployer | `olf/polaris.py` assumes the Polaris token path and Basic client credentials, so it needs an issuer-aware login |
| Bootstrap Job | It authenticates to the internal token endpoint as root. Under `external` that endpoint is disabled and the Job fails; `mixed` keeps it. Principal rotation (`/principals/<name>/rotate`) stops mattering for migrated callers, and their secrets are then rotated at the issuer |
| Principals and grants | Unchanged and still required: every migrated caller needs a Polaris principal whose name the token's name claim matches, with the same catalog-role grants |
| Availability | Today Polaris is self-contained. With an external issuer, Trino and Floe cannot obtain a catalog token while Keycloak is down, which conflicts with the "recovery works with Keycloak down" bound in `identity-sessions-and-recovery.md` for every pipeline and query |
| Local trust | Polaris must reach and trust `https://auth.<base_domain>` (see above) |
| AWS | None. AWS uses Glue; no Polaris is deployed |

## Decision

Defer. Polaris keeps `internal` authentication in v0.5-alpha.

1. **No user-facing benefit.** No person authenticates to Polaris, and the
   issuer would authenticate the same non-interactive callers that a Polaris
   secret authenticates now. The class-2 credentials stay shared service
   accounts either way (`identity-callers.md`).
2. **It does not reach the goal that motivates it.** The issuer replaces
   authentication, not Polaris' principal and grant model. Per-user policy
   needs the end user's identity to arrive at Polaris (see below), which the
   Trino connection model decides, not Polaris' IdP support.
3. **It changes every writer together** (Trino, Floe, the deployer, the
   bootstrap) and puts the issuer in the catalog's availability path, for the
   same unattended pipelines #26 requires to keep running.
4. **Unverified dependencies** — JVM trust of the local CA, the claim that names
   a service principal, Floe's acceptance of a foreign token endpoint — would
   each need a cluster experiment.

### Follow-up scope (tracked in #367)

Start only when per-user identity must reach the catalog (the #179 follow-up)
or an operator needs to remove Polaris-held secrets. It is sequenced with the
#179 follow-up, ahead of the v1.0 governance work (#54–#57). Plan:

1. Verify the three **to verify** items on a cluster.
2. Set `authentication.type: mixed` and `oidc.*` in the chart. Mixed is
   additive: the bootstrap Job, existing Secrets and every current caller keep
   working.
3. Create one issuer confidential client per workload, with a name claim equal
   to its existing Polaris principal; migrate Trino, then Floe, then the
   deployer, one at a time, with the unattended e2e as the gate.
4. Move the bootstrap Job last, or leave it on `internal` and stay on `mixed`.
   Moving to `external` is only worth it once the bootstrap no longer needs
   the root client.
5. Independently of any migration: remove the dormant `om` principal, its
   Secret and grant, and the `om_*` contract fields (or record why they stay).

## Findings for the data-plane assessment (#179)

Where table-level policy would be enforced, as the tree is wired today:

| Layer | Polaris (self-hosted) | Glue (AWS) |
| --- | --- | --- |
| Catalog authorization | Polaris RBAC on the REST catalog: securables catalog, namespace, table, view and policy; privileges such as `TABLE_READ_DATA` granted to catalog roles, assigned to principal roles, assigned to principals. Optional external decision point (`polaris.authorization.type`, OPA documented). Evaluated for the principal on the request | AWS IAM on Glue databases and tables, optionally Lake Formation grants evaluated for the calling IAM principal. Today AWS scopes each stage's Pod Identity role to `database/<prefix>_*` and has **no Lake Formation** (`technical-debt.md`) |
| Who the catalog sees | Trino's per-stage principal with `CATALOG_MANAGE_CONTENT`, not the end user | The stage's Pod Identity role |
| Effect today | Stage isolation only: one catalog per stage, one principal per catalog | Stage isolation by name prefix and IAM |
| Data files | **Not gated by Polaris here.** Trino reads object storage with its own S3 key (`s3.aws-access-key`), `vended-credentials-enabled=false`, and the catalogs are created `stsUnavailable: true`. `TABLE_READ_DATA` is defined as receiving catalog-vended storage credentials, so a per-table grant would not restrict file reads unless Trino switched to vended credentials, which needs a storage side that issues them (**to verify** for SeaweedFS) | IAM on the S3 prefixes |
| Engine-level | Trino access control (`catalog_access_rules`, `tables` rules in `modules/query/trino`) keys on a user name Trino does not authenticate (register entry "Trino has no client authentication"); #26 chooses the mechanism | Same |

Implications to carry into #179's ADR:

- **The two catalog mechanisms are different and not interchangeable.** Polaris
  RBAC and Glue IAM/Lake Formation have different securables, grant shapes and
  principals. A canonical role (ADR 0014) can be mapped to each, but each is a
  separate adapter, and they enforce at different points. Table policy written
  against Polaris does nothing on AWS.
- **The one enforcement point the providers share is the query engine.** Trino
  evaluates the same access-control configuration whatever catalog backs a
  stage, so provider neutrality favours Trino-side table policy keyed by
  canonical role, with the catalog as defense in depth. This is a
  recommendation for #179 to decide, not a decision made here.
- **Polaris policy needs the user at Polaris, which Trino's connection model
  decides.** Trino 480 documents `iceberg.rest-catalog.session` (`NONE` or
  `USER`); what identity Polaris then receives and whether it is the issuer's
  token **is to verify**. Nothing in this evaluation forecloses it: Polaris can
  validate issuer tokens (`mixed` or `external`) and map them to existing
  principals.
- **A Polaris IdP migration is not a prerequisite for Trino-side policy** and
  does not by itself deliver per-user data access.

## Consequences

- No code, Terraform or contract changes. Polaris keeps `internal`
  authentication, the Secrets in `identity-callers.md` and the bootstrap Job.
- The class-2 inventory states that the `om` Polaris credential is unused
  (`identity-callers.md`).
- #26's Polaris acceptance item ("decision documented in an ADR or issue
  comment, with explicit follow-up scope") is met by this record and
  the follow-up issue #367.

## History

New record (#26): Polaris external identity-provider evaluation, explicit
deferral, and the catalog-side findings for #179.
