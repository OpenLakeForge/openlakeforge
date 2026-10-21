# ADR 0014: Identity and authorization — canonical roles, per-seam adapters

## Status

Partly binding. **Binding today:** the canonical role model (seam 1) —
`release/identity-roles.yaml`, rendered into `shared.identity.roles` by all
three roots and validated by `olf` (#175); the `identity.oidc` contract shape
(seam 2); and the local Dex adapter, which the local root deploys for
`spec.identity.issuer: github` and which emits `identity.oidc` (#384). The AWS and Azure roots still
emit their existing `identity` implementations
(`identity.aws_pod_identity`, `identity.azure_workload_identity_ready`).
The local perimeter (seam 3, #176) enforces the route grants; how far it has
been run on a cluster is recorded in `docs/architecture/identity-sessions-and-recovery.md`.
**Built for `issuer: github`:** GitHub teams as the source of people and
roles, with Dex as the issuer adapter ([ADR 0018](0018-github-is-the-forge.md)).
Keycloak, the earlier adapter, is removed (#384): `spec.identity.issuer` is
`github` or `external`, and has no default. The Dex adapter ran once against the
`OpenLakeForge` organisation (qualification run 1 on #384). **Seam 4 is removed:** user
and role administration is GitHub's; `olf users`, the `shared.identity.admin`
contract and `bootstrap_admins` no longer exist (#383).

## Context

v0.5-alpha is the first release in which a person logs in and their role
decides which services they reach. Until #175 that model lived nowhere:
baseline roles were a bullet inside the Keycloak issue (#24), which would have
made the authorization model an adapter detail and broken ADR 0003 — consumers
depend on capability contracts, never on the implementation behind them.

## Decision

### One principle

Canonical OpenLakeForge roles are the only vocabulary. Every provider-specific
thing is a mapping **keyed by** a canonical role, never the reverse. Three seams
each have their own adapter and none leaks into another:

| Seam | Contract location | Local / on-prem adapter | Later adapters |
| --- | --- | --- | --- |
| 1. Role model: what each role may reach | `shared.identity.roles` | none; identical on every provider | identical |
| 2. Issuer: who you are, which roles you hold (Dex **built** for local, #384) | `shared.identity` as `identity.oidc` | Dex with the GitHub connector, or an existing external OIDC issuer | Cognito, Entra ID, IAM Identity Center, Okta as other `identity.oidc` issuers |
| 3. Perimeter: enforce route grants before the service (**built** for local, #176) | `shared.access.perimeter` | Traefik `forwardAuth` to oauth2-proxy | cloud gateway or ALB OIDC action (#180) |

### Seam 1 — the role model (built)

A profile with no `spec.identity` block deploys no issuer and no perimeter
(#399); on the local provider, `spec.identity` requires `spec.access` (ADR 0013); the cloud roots read no `spec.access`. The role model does
not depend on an issuer, so the contract still carries `shared.identity.roles`
and validates it, with `implementation: identity.none` and no `issuer_url`,
`role_claim`, `role_mapping` or `clients`. `shared.access` then has no
`perimeter`. A consumer that needs an issuer must treat `identity.none` as
"unauthenticated, local only".

`release/identity-roles.yaml` is product-level and fixed for v0.5-alpha, like
the component catalog; it is not deployment-configurable. Terraform reads it
with `yamldecode(file(...))` in each root's `contracts.tf`; `olf` reads the
same file to validate the contract.

| Role (highest first) | Superset (`reporting`) | OpenMetadata (`governance_service`) | Dagster (`orchestration`) | Trino UI (`query`) |
| --- | --- | --- | --- | --- |
| `platform-admin` | Admin | Admin | full | yes |
| `data-engineer` | Alpha | write | full | yes |
| `analyst` | Gamma | read | — | — |
| `viewer` | read-only | read | — | — |

Rules that bind:

| Rule | Consequence |
| --- | --- |
| Grants are keyed by the service name the access routes already use: the last segment of a route's contract ref | the perimeter derives authorization mechanically: route ref, to service, to allowed roles |
| A grant's value is the in-service role it maps to | one table answers both "may an analyst open Dagster" and "what Superset role does an analyst get" |
| No entry means the perimeter denies | Dagster OSS has no authorization of its own; the denied route is the control |
| Grants are **monotonic**: a role may hold a service's grant only if every higher-precedence role does | the union of held roles equals the highest held role, so the perimeter and every service agree without precedence code |
| Multi-role = union | a user holding several roles gets the highest one's reach |
| No recognised role = deny | the user is authenticated but denied everywhere, with a "no OpenLakeForge role assigned — ask a platform-admin" response rather than a broken page |
| A grant applies to the service on **every enabled stage** | stage-scoped human roles are deferred |
| Matrix is fixed for v0.5-alpha | configurable authorization is more to build, test, and document, and can follow once the fixed model is proven |
| `identity` and `portal` carry no grant | the login surface is never behind the perimeter, and the portal is the landing page every authenticated user reaches |
| Grafana has no column | #210 adds the row when Grafana is integrated |

`olf` fails closed when `shared.identity.roles` is absent, names a role
outside `precedence`, names a service no route can publish, is not monotonic,
or differs in any way from `release/identity-roles.yaml` (read from the
distribution root, so an installed payload ships it). The published schema
`docs/schema/provider-contracts.schema.json` covers the shape.

### Boundary: OpenLakeForge roles versus cloud IAM

| | OpenLakeForge roles | Cloud IAM (AWS IAM, Azure managed identity) |
| --- | --- | --- |
| Governs | which platform services a **human** may use | what a **workload** (pod) may call in a cloud API |
| Defined by | this ADR | the provider roots (#178, #45) |
| Portable | identical on local, on-prem, AWS, Azure | provider-specific by nature |

End-user roles are never expressed in cloud IAM; that would make the product
non-portable. If per-user data access later binds canonical roles to cloud
principals (an `analyst` mapped to an IAM role with Lake Formation grants,
#179), that is a fifth mapping keyed by canonical role, added then. Nothing
here forecloses it and nothing here builds it.

### Seam 2 — issuer (GitHub through Dex built for local)

`shared.identity` with `implementation: identity.oidc` carries, beside
`roles`:

| Field | Meaning |
| --- | --- |
| `issuer_url` | https issuer; the token `iss` must equal it. The one exception is `spec.access.issuer: none` (ADR 0013, development on `*.localhost`), where Dex issues `http://auth.<base_domain>`, oauth2-proxy sets `--cookie-secure=false` and its callback is `http://oauth2.<base_domain>/oauth2/callback` |
| `role_claim` | the claim holding role values (`groups`, `cognito:groups`, ...) |
| `role_mapping` | canonical role to a list of claim values; keys outside `precedence` are rejected |
| `clients` | exactly `perimeter`, `superset`, `openmetadata`, `trino`, each `client_id` plus `secret_ref{name,key}` |

Nothing in these fields is Dex-specific. Dex: `groups` holding
`<organisation>:<team-slug>`. Entra ID: group object IDs. Cognito:
`cognito:groups` as the claim. The test fixtures include a Cognito-style issuer to prove an
external issuer satisfies the contract unchanged. A secret value in a client
is rejected by the parser and by the #181 secret-value check; credentials
reach pods only through `secretKeyRef`/`envFrom`.

Optional, so an issuer that lacks something says so and consumers degrade
rather than break: `adapter` (provenance only for consumers, e.g. `dex`;
the validator requires `adapter: "dex"` when the profile selects `github`,
to correlate that profile with its binding; external profiles need none) and `capabilities` (`groups_in_token`, `logout_endpoint`,
`email_delivery`, each boolean; absent means not declared). `email_delivery`
says the issuer can send account recovery mail. It carries no host or
credential; Dex declares it false.

The contract carries no free-form block: the only credential-shaped data is a
`secret_ref{name,key}`. If a cloud adapter later needs adapter-specific data,
it adds a typed field then.

**Adding a cloud adapter (Cognito or IAM Identity Center, Entra ID, GCP,
generic OIDC) is a checklist, not a schema change:**

1. Emit `implementation: identity.oidc` with the adapter in `adapter`.
2. Provide `issuer_url`, `role_claim`, and `role_mapping` keyed by canonical
   role, in the issuer's own claim values.
3. Provide all four `clients` with `client_id` and `secret_ref{name,key}`;
   secret values never enter Terraform outputs, the provider contract, logs
   or CI. Secrets the adapter generates are created in-cluster by a bootstrap
   Job, not by Terraform, so they are not in Terraform state.
4. Declare `capabilities` honestly.
5. Add a perimeter adapter only if the cloud has its own (#176, #180).
6. Add a fixture beside `identity-oidc-dex.json`; the conformance test
   runs every fixture through the same consumer-facing validation.

The Deployment Profile gains `spec.identity`: `issuer: github | external`,
which is required. A block without an issuer is rejected, because neither
choice can be made for the operator: `github` needs `spec.identity.github`
(organisation, teams, OAuth App Secret) and `external` needs `issuer_url`,
`role_claim` and `role_mapping`. Leaving out the block still means no issuer and
no perimeter (a quickstart). `external` takes optional `client_ids`
(issuer-assigned client ids by consumer, default the consumer name).

### Local cluster notes for any issuer

`spec.identity.issuer: external` deploys no issuer: the contract comes from the
profile, and the operator provides the four client Secrets under the names the
contract references (`oidc-client-<consumer>`, key `client-secret`).

Two local-cluster risks were checked rather than assumed. `*.localhost` is
loopback for some clients, so back-channel token and JWKS calls need the pod
to reach Traefik at `*.<base_domain>` and trust the local CA; the local root
does both (ADR 0013, decision 7). curl is such a client: it resolves
`*.localhost` to loopback itself without asking DNS, while getaddrinfo-based
clients get the Traefik address from cluster DNS. Single sign-on across hosts needs a cookie
on `.<base_domain>`: curl (libpsl) and headless Chromium both accepted a
`Domain=.olf.localhost` cookie and sent it to a sibling host, and both
rejected `Domain=.localhost`. Firefox and Safari were not tested.

### The GitHub adapter through Dex (built for local; #384)

The local issuer is Dex with its GitHub connector
([ADR 0018](0018-github-is-the-forge.md)). People and their roles live in
the client's GitHub organisation; the profile only says which team holds which
canonical role:

```yaml
spec:
  identity:
    issuer: github
    github:
      organization: acme
      teams:                      # canonical role -> team slugs
        platform-admin: [data-platform]
        data-engineer: [data-eng]
        analyst: [analysts]
        viewer: [dashboards]
      oauth_app_secret_ref:       # Dex's upstream GitHub OAuth App
        name: github-oauth-app
        client_id_key: client-id          # default
        client_secret_key: client-secret  # default
      host: github.example.com    # optional: GitHub Enterprise Server; absent is github.com
```

Keys outside `precedence` are rejected, as for `role_mapping`. The
team slugs are lowercased by `olf`, because GitHub slugs are always lowercase.
The organisation is kept exactly as written and must match its GitHub login's
case: GitHub preserves the case of a login, and Dex compares each team's
organisation login with the configured name exactly, so `openlakeforge` for the
`OpenLakeForge` organisation refuses every member (observed on Dex v2.46.0).
A role with no team is held by nobody. Being an organisation owner or a
repository admin grants nothing: only the mapped teams count.

| Piece | How |
| --- | --- |
| Upstream app | A GitHub OAuth App owned by the organisation with the one callback `https://auth.<base_domain>/callback`. Dex's connector requests `user:email` (it reads the primary, possibly private, address) and `read:org` (teams); both appear on the authorize page and in an organisation's approval request. The operator creates it and stores its client id and secret in the Secret named by `oauth_app_secret_ref` (a Secret name and two distinct keys); neither value enters the profile or state. This is Dex's upstream client, distinct from the four downstream consumer clients of `identity.oidc`. An organisation that restricts third-party access must approve the app |
| GitHub App instead of OAuth App | `olf github connect` (#420) creates a GitHub App from a manifest in the browser and stores `client-id`, `client-secret` and `private-key` in the `oauth_app_secret_ref` Secret. Dex's connector works with a GitHub App's client credentials: the user-to-server token ignores scopes, `/user/orgs` and `/user/teams` accept it with no extra permission, and `/orgs/{org}/members/{user}` and team visibility need the app's *Members: read* permission with the app installed on the organisation; refresh re-reads teams with the refresh token. `private-key` is stored for the role sync (#25); whether that sync reads this Secret or keeps `members_app_secret_ref` is decided there. The manual OAuth App remains supported |
| Admission | The connector's `orgs` lists the organisation with every mapped team. A GitHub user in none of them is refused by Dex at login, before any token exists, so an uninvited identity never reaches the perimeter |
| GitHub Enterprise | `host` is rendered as the connector's `hostName`, so Dex uses `https://<host>/login/oauth/*` and `https://<host>/api/v3/*`. Dex requires `rootCA` with `hostName`; the smallest input is no new field: Dex trusts the platform CA from the `olf-local-ca` ConfigMap (public certificate only, local profile). A GHE server behind a different private CA needs a CA input, left until a client has one |
| E2E without GitHub | CI cannot sign in to github.com. `olf e2e run` runs a stub GHE (`olf/e2e/github_stub.py`, a throwaway pod with a local-CA certificate) when Dex's `hostName` is `olf-e2e-github.<shared namespace>.svc`, and drives the perimeter role decisions through Dex's real connector with throwaway users per team, plus one in no mapped team who must be refused at login. Otherwise those checks skip |
| Groups claim | `teamNameField: slug`; the `groups` claim carries `<organisation>:<team-slug>` |
| `role_mapping` | rendered by the root from `spec.identity.github`: each canonical role to `<organisation>:<slug>` of its teams, `role_claim: groups`. The `identity.oidc` shape does not change, so no consumer changes how it reads roles |
| Scopes | Dex returns the `groups` claim only for the `groups` scope and a refresh token only for `offline_access`. The perimeter's default `--scope=openid email profile` becomes `openid email profile groups offline_access` for Dex, and so do the Superset and OpenMetadata clients; without it every user is denied and no refresh re-reads teams |
| Token lifetimes | Pinned to the session policy, not Dex's defaults (24 h ID tokens): `expiry.idTokens: 5m`, `expiry.refreshTokens.validIfNotUsedFor: 30m`, `expiry.refreshTokens.absoluteLifetime: 10h`, and `reuseInterval` kept (Dex default 3s) because the floor-specific oauth2-proxy instances can redeem the same cookie's refresh token concurrently. Asserted by the adapter's e2e, including parallel refreshes. **Revises the 10 h absolute bound for this adapter:** it ends the OpenLakeForge session chain, but a new one starts without a prompt while the person's GitHub session is valid, because Dex cannot force GitHub to re-authenticate. Re-authentication frequency and 2FA are therefore governed by GitHub and the organisation's policy, not by the platform |
| Clients | `perimeter`, `superset`, `openmetadata`, `trino` as static Dex clients, secrets from the existing bootstrap Job (#181) |
| Login entry | The portal at the base domain is the front door. An unauthenticated visit is sent by the perimeter (`--skip-provider-button`) to Dex; with one connector and `alwaysShowLoginScreen` off, Dex goes straight to GitHub, and `skipApprovalScreen` removes its consent page. The person sees GitHub's authorize page once per app, then lands back on the portal. Dex's own login page is never shown, so it needs no branding |
| Single sign-on | Dex keeps no browser session of its own by default; GitHub's session is what makes the next login silent. A native Superset or OpenMetadata login bounces through Dex to GitHub and back without a prompt while the GitHub session lasts. To be verified with the adapter |
| Logout | **Revises the platform-wide logout policy for this adapter.** Sign-out still ends the perimeter cookie and the native Superset and OpenMetadata sessions, so user B never reaches a surviving session of user A. Dex has no end-session endpoint and the GitHub session belongs to the person, not the platform: OpenLakeForge does not end it. The signed-out page links to GitHub's sign-out, and states that on a shared browser the next sign-in reuses the GitHub account still signed in there |
| Storage | Dex's `postgres` storage on its own `dex` database in the platform PostgreSQL, replacing the removed `keycloak` database. Refresh tokens, the upstream GitHub token and the signing keys stay in a database with the same exposure class, instead of plain `dex.coreos.com` custom resources that the Secret inventory and the exposure scan do not cover. Dex needs no Kubernetes API access, so no RBAC exception; the `identity-credentials.md` inventory gains the `dex` database row with the adapter (#384) |
| Network | Dex calls `github.com` (token exchange) and `api.github.com` (user and team lookups) at every login and refresh. ADR 0017 has no internet egress today; the adapter adds that flow row, scoped to Dex, before default-deny egress is enforced (#384) |
| Revocation | team membership is read at login and again on each refresh; the perimeter refreshes at most every 4 min, so removing someone from a team or the organisation ends perimeter access within the #177 bound. Measured once (qualification run 1): a member moved to another team lost the old role on the first request after the 4 min refresh point |
| Mail | none. Dex sends no mail, and the profile has no `smtp` setting |
| Break-glass | the issuer-outage procedure of `identity-sessions-and-recovery.md` for Dex: the cluster administrator's kubeconfig, the Superset and OpenMetadata in-pod CLIs, Dagster and Trino through their internal Services. Dex has no admin account; the recovery drill is still to run (#177) |

A member of several mapped teams holds the union of their roles, which by the
monotonic rule is the highest one. Whether membership of a child team counts
for a parent team mapped here is to be verified on the adapter; until then,
map the teams people are direct members of.

### Seam 3 — perimeter (built for local; #176)

`modules/access/perimeter` runs oauth2-proxy behind Traefik `forwardAuth`.
Route grants are read from `release/identity-roles.yaml` and nowhere else.

| Piece | How |
| --- | --- |
| Role floor | Grants are monotonic, so what a service admits is a prefix of `precedence`, fully described by its lowest role. The module computes each protected route's floor from its service name (`portal` has none: the lowest role) |
| Instances | One oauth2-proxy Deployment per distinct floor, started with `--allowed-group` for the `role_mapping` claim values of every role at or above it. Today: `oauth2-proxy-viewer` (any recognised role: portal, Superset, OpenMetadata) and `oauth2-proxy-data-engineer` (Dagster, Trino UI). A new service is a row in the matrix; a new floor is a new instance without new code |
| Middleware | Traefik file-provider middlewares (a `Middleware` CRD cannot be planned before the Traefik release installs it): `olf-strip-identity` deletes every inbound `X-Forwarded-*` / `X-Auth-Request-*` identity header, then `olf-auth-<floor>` forwards to that floor's proxy and copies only the proxy's `X-Auth-Request-*` headers upstream |
| Ingress | One `Ingress` per namespace and floor (`openlakeforge-routes[-<floor>]`) carrying the middleware annotation; the namespace's one wildcard certificate is still issued from a single primary `Ingress` (ADR 0013 decision 6) |
| Protected set | Every route of `shared.access.routes` except `shared/identity`, derived, never listed. `olf` rejects a contract whose `shared.access.perimeter.protected_routes` differs |
| Login callback | One host, `oauth2.<base_domain>/oauth2/*`, answered by the any-role instance and not behind the perimeter; the `perimeter` client has that single redirect URI, and the cookie on `.<base_domain>` carries the session to every route |
| Cookie secret | Created by an in-cluster Job only if missing (`oauth2-proxy-cookie`); Terraform never reads it. Rotation: delete the Secret, replace the Job, restart the proxies (signs everyone out once) |
| Client secret | `secretKeyRef` to `identity.oidc.clients.perimeter.secret_ref` |

Behaviour, all decided by the proxy before a request reaches a service:

| Request | Answer |
| --- | --- |
| No session, browser | 302 to the issuer's login (the one login page for every service); after login back to the original URL |
| No session, `Accept: application/json`, `X-Requested-With`, or path under `/api`, `/graphql`, `/v1` | 401, never an HTML login page |
| Session, role not permitted, or no recognised role | 403 with the "Access is role-restricted" page and a link to the portal. The proxy clears the session cookie on this path, so the next page load passes silently through the issuer's SSO session |
| Issuer down | A session the proxy cannot refresh expires with its 5 min access token and is not extended; a new login fails |
| Proxy down | forwardAuth cannot reach it and Traefik answers an error, never the service |
| Forged `X-Forwarded-*` / `X-Auth-Request-*` | Deleted before forwardAuth; the decision is the proxy's, from its own cookie |

Exempt from the perimeter, exhaustively: `auth.<base_domain>` (Dex's login,
callback, token and JWKS endpoints) and `oauth2.<base_domain>/oauth2/*`.
Nothing else is. Dex has no admin console to expose on that route.

The perimeter protects ingress only. Internal Services are reached directly
and bypass it by design; a pod, or an operator holding `pods/portforward`,
needs no session. That is the documented development fallback
(`kubectl port-forward`, `OLF_PORT_FORWARD=1`) and the break-glass path in
[`identity-sessions-and-recovery.md`](../architecture/identity-sessions-and-recovery.md).
`olf` itself takes it for every protected route (`olf.access.service_url`);
only the perimeter and ingress e2e checks go through the public hostnames.
Containing internal traffic is #46 ([ADR 0017](0017-local-network-policy-and-rbac.md)). Dagster in particular has no native
authentication or per-user authorization, so the perimeter is its only human
access control and anyone admitted has full Dagster capability; the boundary
and its internal paths are in
[`identity-callers.md`](../architecture/identity-callers.md#dagster-boundary).

`olf e2e run --env local` exercises the seam (`e2e/_perimeter.py`): per route,
an unauthenticated request is redirected to the issuer (or gets 401 with
`Accept: application/json`), also with forged identity headers; signed in as
per-team stub users for each mapped role and one in no mapped team (refused
by Dex at login), each route
answers as `release/identity-roles.yaml` says, a denial is the 403
role-restricted page with a portal link, and a direct API call carrying only a
denied user's session cookie is refused too.

Session settings are the policy document's: cookie `--cookie-expire=30m`,
re-issued on every refresh (Traefik `addAuthCookiesToResponse` carries the
refreshed cookie to the browser), refresh at most every 4 min; Dex sets
ID tokens 5 min, refresh tokens unused 30 min, absolute 10 h. The 10 h absolute
bound is the refresh token's absolute lifetime ending the refresh chain, not a
cookie lifetime.

### Catalog credentials (Polaris)

Polaris keeps its own OAuth client-credentials model in v0.5-alpha. Whether
the `identity.oidc` issuer could authenticate the catalog callers was
evaluated and deferred in
[ADR 0015](0015-polaris-identity-provider-evaluation.md), which also records
where table-level policy would be enforced for Polaris and for Glue with Lake
Formation (the different-mechanism case the "fifth mapping" above refers to).

### User and role administration — GitHub

There is no admin seam. Inviting someone, changing their role and offboarding
them are GitHub team and organisation changes, made in GitHub's web UI, with
`gh`, or with the client's own GitHub Terraform, under GitHub's audit log and
the organisation's 2FA policy. They need no `olf` command and no apply. The
first `platform-admin` is whoever is in the team mapped to it. OpenMetadata
cannot read roles from the token (`identity-service-permissions.md`), so its
roles follow a role sync, built by #25 as a
CronJob that `olf openmetadata deploy-metadata` applies. It runs only for
`issuer: github`: an `external` issuer or authentication off does not sync (OIDC
alone cannot enumerate members), so those users stay at the read baseline.

| Piece | Decision |
| --- | --- |
| Credential | the GitHub App `olf github connect` created, read from the `oauth_app_secret_ref` Secret (`app-id`, `private-key`), so there is no second Secret reference; a hand-made OAuth App has neither key and gets no sync. The job mints a short-lived installation token per run and reads each mapped team's members with it (*Members: read*) |
| Where it runs | `openmetadata-role-sync`, a CronJob in `olf-system` on the pinned `openmetadata_ingestion` image, the OLF-owned ingestion CronJob's image. The Python script is mounted from a ConfigMap because that image has no `olf`; it imports the standard library only and carries a copy of `github_app.app_jwt`'s signing, using the image's `cryptography` or else the `openssl` CLI, and exits naming what is missing. The App Secret and the `olf-automation` token reach it as secretKeyRefs, so the Secret read needs no RBAC. A project-code image was rejected: it is stage-scoped and exists only with user code |
| GitHub Enterprise | the job calls `https://<host>/api/v3` and trusts only the image's public CAs; a server behind a private CA is not supported by the sync yet |
| Reconciliation | for every non-bot OpenMetadata user except `admin` and `olf-automation`: `isAdmin` for `platform-admin`, the `DataSteward` role for `data-engineer`, neither otherwise, including a user in no mapped team. Other roles are left alone. A person who has not signed in yet has no user and starts at the baseline |
| Interval | every 5 minutes (`*/5 * * * *`), inside the 15 min native-service revocation bound of `identity-sessions-and-recovery.md`; removing someone from a team revokes within one interval plus the job's run. The #25 runtime verification measures a demotion |
| Network | one flow, F30 in `release/network-flows.yaml`: the job to `api.github.com` on 443; its calls to OpenMetadata ride F13 |

| Action | In GitHub |
| --- | --- |
| Invite as `analyst` | add the person to the organisation and to a team mapped to `analyst` |
| Change role | move them between mapped teams |
| Revoke every role | remove them from every mapped team: they can still sign in to GitHub, but Dex refuses them |
| Offboard | remove them from the organisation |
| List who holds what | `gh api --paginate orgs/<org>/teams/<slug>/members` per mapped team |

## Policy documents

Four written gates bind the implementation of seam 3 and the
credential-producing work. They are policy for #176, #25, #26 and #46 to
implement; the perimeter part of the sessions gate is built (seam 3), the rest
is not.

| Gate | Decision | Document |
| --- | --- | --- |
| Credentials (#181) | Identity credentials are generated by an idempotent in-cluster bootstrap Job, reused on redeploy, never read by Terraform. The client secrets are created this way | [`identity-credentials.md`](../architecture/identity-credentials.md) |
| Sessions (#177) | Perimeter cookie 30 min idle, 10 h absolute; access token 5 min; revocation delay at most 5 min at the perimeter and 15 min in native Superset and OpenMetadata sessions; platform-wide logout; fail closed on issuer outage; cluster-administrator recovery works with the issuer down | [`identity-sessions-and-recovery.md`](../architecture/identity-sessions-and-recovery.md) |
| Callers (#178) | Every authentication path is human (class 1), in-cluster or automation workload (class 2), or cloud workload (class 3). Workstation and CI automation reaches services by RBAC-authorized port-forward or in-cluster execution, never through the perimeter or as a human account | [`identity-callers.md`](../architecture/identity-callers.md); Trino callers: [ADR 0016](0016-trino-workload-authentication.md) |
| Service permissions (#25) | Per canonical role, what Superset and OpenMetadata allow and deny, the native roles that implement each grant, how roles reach the service, and how each meets the native session bound. Superset sign-in built (client, role mapping, 15 min bound); OpenMetadata sign-in built, role sync specified | [`identity-service-permissions.md`](../architecture/identity-service-permissions.md) |

## Consequences

- The `identity` binding of every v3 contract gains a required `roles`
  object, so a contract emitted before this change no longer parses. The AWS and Azure
  `implementation` strings are unchanged. The local root's moves from
  `identity.local_development_credentials` to `identity.oidc` (#24): the
  validator still accepts a legacy-implementation contract that carries `roles`,
  but the local root no longer emits one.
- The role model is a release artifact: changing a grant changes
  `release/identity-roles.yaml`, and every root picks it up on the next
  apply. `olf` rejects a platform whose contract was rendered from a
  different file.
- The local Dex adds one always-on pod to `olf-system` and a `dex` database in
  the platform PostgreSQL (input to the #171 fixed-cost budget). Its footprint
  has not been measured; the Keycloak figures this ADR carried (768Mi memory
  request, about 640 MiB resident) went with Keycloak.
- The provider-binding digest recorded at activation covers `shared.identity`,
  so the first activation after upgrading reads as a binding change.
- Platform users need a GitHub account in the mapped organisation, and the
  cluster needs to reach `github.com`; ADR 0018 records the trade-offs.

## History

OpenMetadata role sync (#25): a CronJob reads the mapped teams' members with the GitHub App's installation token and patches `isAdmin` and `DataSteward`. The `members_app_secret_ref` field and the `olf openmetadata sync-roles` command this ADR planned were dropped: the App's credentials are already in `oauth_app_secret_ref`.

Inline GitHub connect (#428): with a missing OAuth App Secret, an interactive
`olf deploy` runs the `olf github connect` flow itself instead of refusing;
CI, piped runs and `--no-github-connect` keep the refusal. The flow runs
before the foundation apply (#448), so the rest of the deploy is unattended; the
converted App stays in memory until the Secret is written, and the end-of-deploy
summary asks GitHub whether the App is installed. Bootstraps keep
their class-2 workload identities.

Keycloak removed (#384 slice c): `modules/identity/keycloak`, its `keycloak`
database, the `keycloak` and `keycloak-config-cli` catalog pins, `spec.identity.smtp`
(with its deploy-time Secret check and `OLF_SMTP_*` variables), the
`email_delivery` consistency check, the Keycloak e2e (admin API, throwaway
users, email delivery against the Mailpit sink) and the `e2e_mail_sink` pin are
deleted. `spec.identity.issuer` accepts `github` and `external` only. **It has
no default**: the old default, Keycloak, had no replacement that needs no
further input, since `github` requires a `github` block, so a block without an
issuer is now an error and omitting the block still means no issuer. The
nightly applies `openlakeforge.conformance.yaml`, which selects `github` with
`host: olf-e2e-github.olf-system.svc`; it creates a dummy `github-oauth-app`
Secret (any client id and secret, since the e2e stub never checks them) after a
first `olf platform apply` has created the shared namespace and stopped, then
applies again (`docs/setup/github-oauth-app.md`, `local-full-e2e.yml`). The
entries below that name Keycloak describe the code that existed then.
Because #421 requires a profile on a cloud provider to declare `spec.identity`,
the cloud profiles in the tests carry an `external` block; the AWS and Azure
roots emit no `identity.oidc` yet, so the check that an external profile meets
an `identity.oidc` contract applies to the local provider only.

#418 made a profile without `spec.identity` the explicit "authentication off"
mode: accepted only for `provider: local` with no `spec.access` or a
`*.localhost` base domain. Superset then signs every visitor in as its local admin (an anonymous
`Admin` role is not enough: Superset needs a real user as the owner of saved
objects); OpenMetadata has no anonymous mode, so `olf status`
shows its default admin login. With `spec.identity` nothing changes.

#403 added the http issuer exception to `issuer_url` for `spec.access.issuer: none`.

New record (#175). Seam 1 is built; seams 2–4 record the direction for #24,
#176, and #331, which rewrite this ADR as they land.

Seam 2 contract (#24 part a): `identity.oidc` fields fixed and validated in
`olf` and the schema; profile `spec.identity` added; realm-as-code decided as
a `keycloak-config-cli` Job. The Keycloak adapter is #24 part b.

Keycloak adapter (#24 part b): the local root deploys Keycloak, applies the
realm with `keycloak-config-cli`, routes `auth.<base_domain>` and emits
`identity.oidc`; `spec.identity.issuer: external` skips the deployment.

Policy gates (#181, #177, #178): the credential inventory and bootstrap target,
the session and recovery policy, and the caller inventory are recorded in
`docs/architecture/`; see "Policy documents". Design only; no behaviour changed.

Identity credentials (#181): the Keycloak admin password and the four OIDC
client secrets are created by an in-cluster bootstrap Job only if missing, not
by `random_password`, so Terraform state no longer holds them.

Outbound SMTP (#24): `spec.identity.smtp`, the `email_delivery` capability, the
realm `smtpServer` and the deploy-time Secret check are built. The shared
namespace is created by the platform apply, so a first deploy supplies the
login through `OLF_SMTP_USERNAME`/`OLF_SMTP_PASSWORD` (#331, slice 2; see "The
Deployment Profile gains `spec.identity`" above) or creates the Secret by hand
after a first deploy without `smtp`.

Email delivery check (#24): `olf e2e run` starts a throwaway Mailpit (catalog
`e2e_mail_sink`, digest-pinned; never part of the stack), creates a throwaway
realm pointed at it, asks Keycloak for an `UPDATE_PASSWORD` action email for a
test user and asserts it reaches the sink; a second send with SMTP pointed at a
closed port must come back as HTTP 500 `Failed to send execute actions email`
and leave nothing in the sink. A throwaway realm is used because replacing a
realm's `smtpServer` through the admin API drops the stored password, which
would break a real relay. The deployed realm is only read, to require that
`resetPasswordAllowed` and `verifyEmail` equal `capabilities.email_delivery`.
This proves Keycloak's mail path, not an operator's relay.

Perimeter (#176): seam 3 is built for the local root. oauth2-proxy instances
per role floor, Traefik file-provider middlewares, one `Ingress` per namespace
and floor (ADR 0013 decision 6 rewritten accordingly), a single login callback
host, `shared.access.perimeter` in the contract, realm token and SSO lifespans
set, and `olf` clients port-forward to protected routes. Runtime verification
and what remains (issuer outage drill, logout, latency) are tracked in
`docs/architecture/identity-sessions-and-recovery.md`.

Admin client (#331, slice 1): seam 4's contract is built. The realm gains the
service-account-only `olf-users` client with `view-users` and `manage-users`,
its secret is created by the credentials Job, the adapter emits
`shared.identity.admin` and declares `admin_api`, and the realm records admin
events without details. `olf e2e run` asserts that the client authenticates by
client credentials, lists users and groups, and cannot read or write realm
settings, list clients, read events or reach another realm.

Dagster boundary and Polaris (#26): the Dagster paragraph in seam 3 and the
"Catalog credentials" section point at `identity-callers.md` and ADR 0015.
Documentation only.

User commands (#331, slice 1): `olf users` roles, list, grant, revoke, disable and enable.

Invitations (#331, slice 2): `olf users invite`, `resend` and `cancel`. `olf e2e
run` points the deployed realm's `smtpServer` at the throwaway mail sink for the
step (skipped when the realm has its own SMTP, which a realm update would
corrupt), completes onboarding through the mailed link as the new member,
and empties `smtpServer` again.

Trino workload authentication (#26 decision gate): [ADR 0016](0016-trino-workload-authentication.md)
selects password-file authentication over HTTPS for the class-2 Trino callers.
Human OAuth2 for the Trino UI stays separate; no seam here changed.

Service permissions (#25 specification): the allowed and denied actions per
role for Superset and OpenMetadata, the exact native roles behind `Alpha`,
`Gamma`, `read-only`, `write` and `read`, and the session alignment are in
`docs/architecture/identity-service-permissions.md`. It records that
OpenMetadata 1.13.6 cannot map the canonical claim to roles itself. Design
only; no behaviour changed.

Bootstrap administrators and first-deploy SMTP (#331, slice 2): `spec.identity.bootstrap_admins`
is built (see seam 4). A first deploy can create the SMTP Secret from
`OLF_SMTP_USERNAME`/`OLF_SMTP_PASSWORD`, which `olf` removes from its own
process environment at start so no child process inherits them.

GitHub as the source of people (ADR 0018): the default local issuer is decided
as Dex with the GitHub connector, roles mapped to organisation teams in
`spec.identity.github`. Seam 4 is removed: `olf users`, the
`shared.identity.admin` contract and `capabilities.admin_api`, the Keycloak
`olf-users` client and `spec.identity.bootstrap_admins` are deleted, and
invitation, role assignment and offboarding are GitHub's. Keycloak stays
deployed until the Dex adapter replaces it.

Organisation login case (#397): the organisation is no longer lowercased.
Lowercasing it (#388, from a review finding that holds for team slugs only)
made Dex refuse every member of `OpenLakeForge`, because GitHub preserves the
case of a login and Dex compares it exactly. Team slugs stay lowercased.

Dex adapter (#384 slice b1): `modules/identity/dex` deploys a digest-pinned Dex
for `spec.identity.issuer: github` (postgres storage on a `dex` database, the
GitHub connector limited to the mapped teams, the four static clients with
secrets from an in-cluster Job) and the root emits `identity.oidc` with
`adapter: dex`. The perimeter requests `groups offline_access` for it, and
`olf deploy` refuses to start the platform phase without the OAuth App Secret
(an interactive run instead creates the GitHub App inline, #428; CI and `--no-github-connect` still refuse).
Dex's static clients match redirect URIs exactly, so the OpenMetadata and
Trino clients carry the Keycloak wildcard URIs until each is wired to OIDC
(#25); the Superset client already has its callback,
`https://superset-<stage>.<base_domain>/oauth-authorized/olf`. Keycloak's removal, the stub-upstream e2e and the
NetworkPolicy flow are separate slices.

OpenMetadata sign-in (#25 slice b): OpenMetadata authenticates through the
issuer as a confidential `custom-oidc` client with the principal taken from
`preferred_username`. The bootstrap Job applies it through the security
configuration API because 1.13.6 keeps that configuration in its database after
the first boot; an admin automation bot replaces basic login for `olf`. 1.13.6
has no claim-based admin mapping, so every signed-in person starts at the read
baseline until the role sync lands. Details in
`docs/architecture/identity-service-permissions.md`.
