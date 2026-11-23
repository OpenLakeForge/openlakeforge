# ADR 0014: Identity and authorization — canonical roles, issuer and perimeter

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ (Dex for `issuer: github`, #384; perimeter, #176) · AWS, Azure: role model only, no issuer or perimeter (#180) |

## Context

A person signs in, and their role decides which services they reach. Until
#175 that model lived nowhere: the baseline roles were a bullet inside the
Keycloak issue (#24), which would have made authorization an adapter detail
and broken ADR 0003, under which consumers depend on capability contracts,
never on the implementation behind them. The platform also has to run on
local, on-prem and two clouds whose identity systems share nothing, and the
team it serves already keeps the list of who works there in its GitHub
organisation ([ADR 0018](0018-github-is-the-forge.md)). So the role model, the
issuer that says who a person is, and the perimeter that enforces the grants
are three separate seams, each with its own adapter, and none leaks into
another.

## Decision

**D1. Canonical OpenLakeForge roles are the only vocabulary, and the role
model is `release/identity-roles.yaml`.** Every provider-specific thing is a
mapping keyed by a canonical role, never the reverse. The file is product-level
and fixed, like the component catalog, and is not deployment-configurable;
every root renders it into `shared.identity.roles` unchanged. Its grants are
keyed by the service name the access routes use (the last segment of a
route's contract ref), and a grant's value is the in-service role it maps to.
No entry means the perimeter denies. Grants are monotonic: a role may hold a
service's grant only if every higher-precedence role does, so several held
roles are a union equal to the highest. An authenticated person with no
recognised role is denied everywhere with a "no OpenLakeForge role assigned"
answer, not a broken page. A grant applies on every enabled stage. `identity`
and `portal` carry no grant. `query_api`, Trino's `/v1` path on
`trino-<stage>.<base_domain>`, is a grant row with no route of its own,
held at the `viewer` floor when people sign in with GitHub. `olf` fails
closed when `shared.identity.roles` is absent, names a role outside
`precedence`, names a service no route can publish, is not monotonic, or
differs in any way from the release file read from the distribution root.
The file's `data_access` rows obey the same rules, plus one of their own:
each stage row is a shape a Polaris catalog role can express. What they grant
is [ADR 0019](0019-data-plane-authorization-deferral.md)'s.
Enforced by: `release/identity-roles.yaml`, validated in
`tools/olf/olf/common/provider_contracts/_v3.py` and
`docs/schema/provider-contracts.schema.json`.

**D2. The issuer is reached only through the `identity.oidc` contract, and the
profile names it as `issuer: github` or `issuer: external`, with no
default.** The contract carries `issuer_url`, `role_claim`, `role_mapping`
keyed by canonical role, and exactly four `clients`: `perimeter`, `superset`
and `openmetadata`, each a `client_id` and a `secret_ref{name,key}`, and the
public `olf-cli` with a `client_id` only. Nothing in it is issuer-specific,
and the only credential-shaped data is a `secret_ref`: a secret value is
rejected, there is no free-form block, and an adapter that needs more adds a
typed field. Optional `adapter` (required as `dex` when the profile selects
`github`), `internal_url` (only for an issuer OpenLakeForge deploys) and
`capabilities` let an issuer say what it lacks, so consumers degrade rather
than break. `issuer_url` is https, except with `spec.access.tls: none` on
`*.localhost`. A `spec.identity` block without an issuer is rejected, because
`github` needs `spec.identity.github` and `external` needs the issuer fields,
and neither can be chosen for the operator. `external` deploys no issuer: the
operator provides the client Secrets the contract names. Secrets an adapter
generates are created in-cluster, never in Terraform state, outputs, the
contract, logs or CI. A new issuer is a checklist, not a schema change
([reference](../architecture/identity/identity-perimeter-and-adapter.md#adding-an-issuer-adapter)).
Enforced by: `tools/olf/olf/common/provider_contracts/_v3.py`,
`tools/olf/olf/common/profile.py`.

**D3. Every published route except the login surface sits behind the
perimeter, which enforces the role floor before a request reaches the
service.** oauth2-proxy runs behind Traefik `forwardAuth`, with one instance
per distinct role floor (the lowest role holding a service's grant), admitting
the claim values of every role at or above it. Route grants are read from
`release/identity-roles.yaml` and nowhere else. The protected set is derived,
never listed: every route of `shared.access.routes` except `shared/identity`,
and `olf` rejects a contract whose `protected_routes` differs. Exempt,
exhaustively: `auth.<base_domain>` and `oauth2.<base_domain>/oauth2/*`. A
request with no session gets a redirect to the issuer from a browser and 401
on an API path, never an HTML login page; a role not permitted gets 403. The
perimeter fails closed: it extends no session it cannot refresh, and with the
proxy down Traefik answers an error, never the service. It protects ingress
only: internal Services bypass it by design (port-forward fallback,
break-glass), and containing that traffic is
[ADR 0017](0017-local-network-policy-and-rbac.md)'s. For Dagster, which has no
authorization of its own, the perimeter is the only human access control.
Mechanics:
[reference](../architecture/identity/identity-perimeter-and-adapter.md#the-perimeter).
Enforced by: `infra/terraform/modules/access/perimeter`, `olf infra e2e run
--env local` (`e2e/_perimeter.py`).

**D4. User and role administration is GitHub organisation and team
membership; OpenLakeForge has no administration seam.** Inviting someone,
changing their role and offboarding them are GitHub team and organisation
changes, made in GitHub, with `gh` or with the client's own GitHub Terraform,
under GitHub's audit log and 2FA policy, and they need no `olf` command and no
apply. The first `platform-admin` is whoever is in the team mapped to it. Only
mapped teams count: being an organisation owner or a repository admin grants
nothing. Why GitHub, and why Dex rather than GitHub OAuth in each service, is
[ADR 0018](0018-github-is-the-forge.md)'s. Steps:
[manage members](../guides/identity/manage-members.md). Enforced by: the Dex
connector's `orgs` in `infra/terraform/modules/identity/dex/main.tf`; there is
no user command in `tools/olf/olf/cli.py`.

**D5. Authentication is off only where nothing is published beyond the
operator's machine.** A profile with no `spec.identity` deploys no issuer and
no perimeter. That is allowed on `local` with no `spec.access` or a
`*.localhost` base domain, and on `aws` or `azure` with no `spec.access`,
since those roots publish no routes and are reached only through `olf infra
forward`. A cloud `spec.access` asks for routes, so it requires
`spec.identity`; on `local`, `spec.identity` requires `spec.access`. The
contract still carries and validates `shared.identity.roles`, with
`implementation: identity.none` and no issuer fields, and `shared.access` has
no `perimeter`; a consumer treats `identity.none` as "unauthenticated, local
only". Superset then signs every visitor in as its local admin, and `olf`
shows OpenMetadata's default admin login, since it has no anonymous mode. Enforced by: `tools/olf/olf/common/profile.py`.

**D6. OpenLakeForge roles are never cloud IAM.** Canonical roles govern which
platform services a person uses, identically on every provider; cloud IAM
governs what a workload may call in a cloud API and is the provider roots'.
A workload never holds a canonical role. A person's data access reaches
Polaris catalog roles through `data_access` instead (ADR 0019). Enforced by:
`infra/terraform/environments/*/contracts.tf`, which render the release file
and map no role to IAM.

**D7. The perimeter accepts a bearer token only for the `olf-cli` audience,
and the role floor still decides.** Every instance that accepts bearer tokens
starts with `--skip-jwt-bearer-tokens=true`; a token is checked for issuer,
audience, signature and expiry, then meets the instance's `--allowed-group`
like a session. A token issued to any other client gets 401; the perimeter's
own client is accepted as well, which neither oauth2-proxy mechanism can
exclude. With an `internal_url` (Dex) the proxy's own verifier accepts the
`olf-cli` audience (`--oidc-extra-audience`). For an `external` issuer,
`--extra-jwt-issuers=<issuer_url>=<olf-cli client_id>` is set only when
`role_claim` is `groups`, because oauth2-proxy reads an extra issuer's roles
from a hard-coded `groups` claim; with any other claim a bearer token gets
401. With Dex, `olf-cli` lists `perimeter` in `trustedPeers` and the
perimeter requests the `olf-cli` audience, so the browser session's ID token
is one Trino, Polaris and the platform API accept; `aud` `olf-cli` therefore
no longer means the CLI asked, and `azp` names the client that did. Enforced
by: `bearer_args` in `infra/terraform/modules/access/perimeter/main.tf`;
`tests/browser_token.tftest.hcl`.

**D8. Identity headers are stripped before the perimeter decides, and only the
platform API's route receives the ID token.** The `olf-strip-identity`
middleware deletes every inbound `X-Forwarded-*` and `X-Auth-Request-*`
identity header, and `olf-auth-<floor>` copies only the proxy's own
`X-Auth-Request-*` headers upstream, so the decision is the proxy's, from its
own cookie. The proxies put `Authorization: Bearer <ID token>` on the auth
response, and the one middleware that copies it upstream is attached only to
the platform API's `/api` route ([ADR 0021](0021-platform-api.md), decision
1). Superset, OpenMetadata, Dagster, Trino and the portal never receive it.
Enforced by: `infra/terraform/modules/access/perimeter/main.tf`;
`tests/browser_token.tftest.hcl`.

**D9. For `issuer: github` the issuer is Dex with its GitHub connector, which
admits only members of the mapped teams.** The profile maps each canonical
role to team slugs; the root renders `role_mapping` as `<organisation>:<slug>`
with `role_claim: groups`, so no consumer reads roles differently. The
connector lists the organisation with every mapped team, so a GitHub user in
none of them is refused by Dex before any token exists. Team slugs are
lowercased; the organisation is kept exactly as written, because Dex compares
it with GitHub's case-preserving login. The perimeter, Superset and
OpenMetadata request the `groups` and `offline_access` scopes; without them
every user is denied and no refresh re-reads teams. The upstream app's
credentials live only in the `oauth_app_secret_ref` Secret, never in the
profile or state. Dex stores its refresh tokens, upstream token and keys in
its own `dex` database on the platform PostgreSQL, not in custom resources
outside the Secret inventory, and needs no Kubernetes API access. Mechanics:
[reference](../architecture/identity/identity-perimeter-and-adapter.md#the-github-adapter-through-dex).
Enforced by: `infra/terraform/modules/identity/dex`,
`tools/olf/olf/common/profile.py`.

**D10. Dex's token lifetimes are pinned to the session policy, sign-out ends
the perimeter session only, and the person's GitHub session is GitHub's.** ID
tokens last 5 minutes, refresh tokens 30 minutes unused and 10 hours
absolute, and `reuseInterval` is kept because the floor-specific proxies can
redeem one cookie's refresh token concurrently. Team membership is re-read at
every login and refresh. The 10-hour bound ends the OpenLakeForge refresh
chain, but a new chain starts without a prompt while the GitHub session is
valid, so re-authentication and 2FA are governed by GitHub and the
organisation. Sign-out is the perimeter's `/oauth2/sign_out`, which clears
the perimeter cookie and lands on the portal's unprotected
`/signed-out.html`; the portal's Sign out link uses it, and Superset's Logout
ends Superset's own session first and then uses it. It guarantees only that
the next request to a protected route needs a new sign-in. It does not revoke
Dex's refresh token, which lapses after 30 minutes unused; it does not end an
OpenMetadata session, whose logout is not tied to the perimeter, or a
Superset session when started from the portal, each of which lasts until its
native bound (D12); and it does not end the GitHub session, since Dex has no
end-session endpoint. On a shared browser the next sign-in therefore silently
reuses the GitHub account still signed in there. With `issuer: external`
there is no sign-out address and the portal shows no Sign out link.
Mechanics:
[reference](../architecture/identity/identity-sessions-and-recovery.md#logout).
Enforced by: `expiry` in `infra/terraform/modules/identity/dex/main.tf`;
`sign_out_url` in `infra/terraform/environments/local/contracts.tf`;
`OlfAuthOAuthView.logout` in `images/superset/olf_security.py`
(`tools/olf/tests/infra/test_superset_oidc.py`); the adapter's e2e, including
parallel refreshes. No runtime test covers sign-out.

**D11. OpenMetadata roles follow GitHub teams through a role-sync CronJob,
only for `issuer: github`, and the sync changes nothing when a mapped team has
child teams.** OpenMetadata cannot map the role claim itself, so
`openmetadata-role-sync` runs every 5 minutes, within the 15-minute native
bound, with the GitHub App's installation token from the
`oauth_app_secret_ref` Secret. It sets `isAdmin` for `platform-admin`, the
`DataSteward` role for `data-engineer`, and neither for anyone else, a user in
no mapped team included; `admin`, `olf-automation` and bots are left alone.
GitHub's team-members API includes child teams' members, which would hand a
parent's role to them, hence the stop. A hand-made OAuth App, an `external`
issuer or authentication off gets no sync, and users stay at the read
baseline. With sign-in on, `adminPrincipals` is empty and the local `admin` is
demoted, so an issuer login named `admin` is not an administrator. Mechanics:
[reference](../architecture/identity/identity-perimeter-and-adapter.md#openmetadata-role-sync).
Enforced by: `tools/olf/olf/infra/openmetadata/_role_sync.py`,
`role_sync_script.py`.

**D12. Four policy documents bind the identity implementation.** Credentials:
identity secrets are generated by an idempotent in-cluster bootstrap Job,
reused on redeploy and never read by Terraform
([`identity-credentials.md`](../architecture/identity/identity-credentials.md)).
Sessions: perimeter cookie 30 minutes idle and 10 hours absolute, access token
5 minutes, revocation within 5 minutes at the perimeter and 15 minutes in
native Superset and OpenMetadata sessions, fail closed on an issuer outage,
and cluster-administrator recovery that works with the issuer down
([`identity-sessions-and-recovery.md`](../architecture/identity/identity-sessions-and-recovery.md)).
Callers: every authentication path is human (class 1), in-cluster or
automation workload (class 2) or cloud workload (class 3); workstation and CI
automation reach services by RBAC-authorized port-forward or in-cluster
execution, never through the perimeter or as a person
([`identity-callers.md`](../architecture/identity/identity-callers.md)).
Service permissions: per canonical role, what Superset and OpenMetadata allow
and deny and which native roles implement it
([`identity-service-permissions.md`](../architecture/identity/identity-service-permissions.md)).
Enforced by: the four documents, and the e2e checks each one names.

## Consequences

- The role model is a release artifact: changing a grant changes
  `release/identity-roles.yaml`, every root picks it up on the next apply, and
  `olf` rejects a platform whose contract was rendered from another file. A
  grant cannot be configured per deployment.
- Human roles are not stage-scoped: a grant holds on every enabled stage.
  Grafana has no column; #210 adds it when Grafana is integrated.
- Every v3 contract's `identity` binding requires `roles`, and the
  provider-binding digest covers `shared.identity`, so the first activation
  after an upgrade reads as a binding change.
- Anyone who holds a kubeconfig reaches every service through its internal
  Service; a role limits people, not cluster access.
- Anyone admitted to Dagster has its full capability.
- Platform users need a GitHub account in the mapped organisation, and the
  cluster must reach `github.com` and `api.github.com` (flow F28); ADR 0018
  records the trade-offs. Whether a child team's members count for a mapped
  parent team at sign-in is not verified, so map direct teams.
- The local Dex adds one always-on pod to `olf-system` and a `dex` database to
  the platform PostgreSQL; its footprint is not measured (#171).
- AWS and Azure have no issuer or perimeter yet (#180); `docs/technical-debt.md`
  tracks it.

## Boundaries

| Not decided here | Where |
| --- | --- |
| What a person reads and writes in the lakehouse (`data_access`, Polaris grants, people's queries) | [ADR 0019](0019-data-plane-authorization-deferral.md) |
| Whether Polaris authenticates with the issuer | [ADR 0019](0019-data-plane-authorization-deferral.md) D7, D13 |
| Trino workload authentication and its Web UI user | [ADR 0016](0016-trino-workload-authentication.md) |
| Why GitHub and Dex | [ADR 0018](0018-github-is-the-forge.md) |
| Ingress, routes, TLS, in-cluster resolution of the issuer | [ADR 0013](0013-service-access-and-ingress.md) |
| NetworkPolicy and flows (F28 Dex egress, F30 role sync) | [ADR 0017](0017-local-network-policy-and-rbac.md), `release/network-flows.yaml` |
| The platform API's role matrix and its service principals | [ADR 0021](0021-platform-api.md) |
| Which identity class each `olf` command uses | [ADR 0020](0020-infrastructure-common-and-platform-cli-surfaces.md) |
| What each role opens, by service | [Roles and permissions](../reference/roles-and-permissions.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Roles defined inside the issuer adapter (#24 as first scoped) | Makes authorization an adapter detail; consumers would depend on the implementation (ADR 0003) |
| End-user roles expressed in cloud IAM | Not portable across providers |
| A deployment-configurable authorization matrix | More to build, test and document; can follow once the fixed model is proven |
| Keycloak as the issuer, with SMTP invitations, `olf users` and an admin seam (#331) | Removed (#383, #384): a second user directory beside GitHub (ADR 0018) |
| Lowercasing the organisation name (#388) | Dex compares GitHub's case-preserving login exactly and refused every member (#397) |
| Dex storage as Kubernetes custom resources | Outside the Secret inventory and the exposure scan |
| Traefik `Middleware` CRDs for the perimeter | Cannot be planned before the Traefik release installs the CRD |
| A second Secret reference and an `olf` command for the role sync | The GitHub App's credentials are already in `oauth_app_secret_ref`; a CronJob needs no command |
| The project-code image for the role sync | Stage-scoped, and exists only with user code |

## History

- 2026-10-13 (#175): New record. D1 built; the issuer and perimeter seams
  recorded as direction. D12's credentials gate added (#181).
- 2026-10-14 (#24): D2's `identity.oidc` fields and `spec.identity` fixed, with
  a Keycloak adapter.
- 2026-10-16 (#383): The administration seam (`olf users`, the admin contract,
  bootstrap administrators) removed; D4 makes administration GitHub's
  (ADR 0018).
- 2026-10-16 (#397): D9 keeps the organisation's case.
- 2026-10-18 (#403, #418): D2 allows an http issuer with `tls: none`; D5
  added, local only.
- 2026-10-19 (#419): Keycloak removed; D2's issuer is `github` or `external`
  with no default.
- 2026-10-21 (#384): D9 and D10: Dex with the GitHub connector is the local
  issuer.
- 2026-10-22 (#515): D5 extended to `aws` and `azure` with no `spec.access`.
- 2026-10-22 (#25): D11 added; OpenMetadata signs in through the issuer.
- 2026-10-23 (#176, #177, #178): D3 built for local; D12's sessions and
  callers gates added.
- 2026-11-01 (#536): `olf-cli` joins D2's clients; D7 added.
- 2026-11-04 (#799): D7's Dex audience and D8's ID token on `/api` only.
- 2026-11-09 (#983): the Dex `trino` client removed; D2 names four clients.
- 2026-11-11 (#991): D2 gains `internal_url`; D7's Dex verification uses it.
- 2026-11-22 (#1385): Corrected to match the code; no decision changed.
- 2026-11-23: Rewritten in the standard form; seams 1 to 3 kept as D1 to D3;
  adapter and perimeter mechanics moved to
  `docs/architecture/identity/identity-perimeter-and-adapter.md`; no decision changed.
- 2026-11-23: D10 corrected to match the code: sign-out ends the perimeter
  session only.
