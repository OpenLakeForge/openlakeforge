# ADR 0014: Identity and authorization — canonical roles, per-seam adapters

## Status

Partly binding. **Binding today:** the canonical role model (seam 1) —
`release/identity-roles.yaml`, rendered into `shared.identity.roles` by all
three roots and validated by `olf` (#175); the `identity.oidc` contract shape
(seam 2); and the local Keycloak adapter, which the local root deploys by
default and which emits `identity.oidc` (#24). The AWS and Azure roots still
emit their existing `identity` implementations
(`identity.aws_pod_identity`, `identity.azure_workload_identity_ready`).
**Decided, not built:** seams 3–4 below. Nothing enforces a grant yet. Rows
marked "not built" are the direction #176 and #331 implement; they are not a
description of the code.

## Context

v0.5-alpha is the first release in which a person logs in and their role
decides which services they reach. Until #175 that model lived nowhere:
baseline roles were a bullet inside the Keycloak issue (#24), which would have
made the authorization model an adapter detail and broken ADR 0003 — consumers
depend on capability contracts, never on the implementation behind them.

## Decision

### One principle

Canonical OpenLakeForge roles are the only vocabulary. Every provider-specific
thing is a mapping **keyed by** a canonical role, never the reverse. Four seams
each have their own adapter and none leaks into another:

| Seam | Contract location | Local / on-prem adapter | Later adapters |
| --- | --- | --- | --- |
| 1. Role model: what each role may reach | `shared.identity.roles` | none; identical on every provider | identical |
| 2. Issuer: who you are, which roles you hold (**built** for local, #24) | `shared.identity` as `identity.oidc` | Keycloak, or an existing external OIDC issuer | Cognito, Entra ID, IAM Identity Center, Okta as other `identity.oidc` issuers |
| 3. Perimeter: enforce route grants before the service (**not built**, #176) | `shared.access.perimeter` | Traefik `forwardAuth` to oauth2-proxy | cloud gateway or ALB OIDC action (#180) |
| 4. Admin: write user and role assignment (**not built**, #331) | optional `shared.identity.admin` | Keycloak admin API behind `olf users` | absent: `olf users` is read-only and points at the issuer console |

### Seam 1 — the role model (built)

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

### Seam 2 — issuer (built for local; admin surface #331)

`shared.identity` with `implementation: identity.oidc` carries, beside
`roles`:

| Field | Meaning |
| --- | --- |
| `issuer_url` | https issuer; the token `iss` must equal it |
| `role_claim` | the claim holding role values (`groups`, `cognito:groups`, ...) |
| `role_mapping` | canonical role to a list of claim values; keys outside `precedence` are rejected |
| `clients` | exactly `perimeter`, `superset`, `openmetadata`, `trino`, each `client_id` plus `secret_ref{name,key}` |

Nothing in these fields is Keycloak-specific. Keycloak: groups named exactly
as the canonical roles. Entra ID: group object IDs. Cognito: `cognito:groups`
as the claim. The test fixtures include a Cognito-style issuer to prove an
external issuer satisfies the contract unchanged. A secret value in a client
is rejected by the parser and by the #181 secret-value check; credentials
reach pods only through `secretKeyRef`/`envFrom`.

Optional, so an issuer that lacks something says so and consumers degrade
rather than break: `adapter` (provenance only for consumers, e.g. `keycloak`;
the validator requires `adapter: "keycloak"` when the profile selects keycloak,
the default, to correlate that profile with its binding; external profiles
need none) and `capabilities` (`admin_api`, `groups_in_token`,
`logout_endpoint`, `email_delivery`, each boolean; absent means not declared,
and no `admin_api` means `olf users` is read-only and points at the issuer
console). `email_delivery` says the issuer can send account-setup and recovery
mail, so #331 can fail an invitation early with "no mail configured" instead of
after the account exists. It carries no host or credential, and for the
keycloak adapter `olf` requires it to be true exactly when the profile sets
`spec.identity.smtp`.

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
   or CI. Secrets the adapter generates (`random_password`, Kubernetes Secret
   values) do live in Terraform state, next to the kubeconfig; see
   `docs/technical-debt.md`.
4. Declare `capabilities` honestly; implement `admin_api` only if the issuer
   has an admin surface (#331).
5. Add a perimeter adapter only if the cloud has its own (#176, #180).
6. Add a fixture beside `identity-oidc-keycloak.json`; the conformance test
   runs every fixture through the same consumer-facing validation.

The Deployment Profile gains `spec.identity`: `issuer: keycloak | external`
(default `keycloak`), with optional `issuer_url`, `role_claim`, `role_mapping`, `client_ids` (external only: issuer-assigned
client ids by consumer, default the consumer name);
`external` requires all three. `bootstrap_admins` (#331) and the optional
`shared.identity.admin` block (seam 4) are deferred to #331.

`spec.identity.smtp` (keycloak only; an external issuer sends its own mail)
configures outbound mail. Settings are non-secret: `host`, `port`,
`from_address`, optional `from_name`, `security` (`none`, `starttls` default,
`ssl`) and `auth` (default false). With `auth: true`, `credentials_secret_ref`
`{name, username_key, password_key}` (keys default `username`/`password`)
names a Secret the operator creates in the shared namespace; the profile
never carries the login. Validation rejects a missing or out-of-range port, a
malformed host or sender, `auth` without a reference (and a reference without
`auth`), and `auth` over `security: none`, which would send the login in the
clear. `olf deploy` refuses to start the platform phase until the Secret and
both keys exist, naming the `kubectl create secret` command shape; it reads key
names, never values.

### The local Keycloak adapter

`modules/identity/keycloak` runs one Keycloak (digest-pinned in
`release/component-catalog.yaml`) in the shared namespace, on its own
`keycloak` database in the platform PostgreSQL. `spec.identity.issuer:
external` deploys none of it: the contract comes from the profile, and the
operator provides the four client Secrets under the names the contract
references (`oidc-client-<consumer>`, key `client-secret`).

| Piece | How |
| --- | --- |
| Realm as code | a `keycloak-config-cli` Job applies one realm file; the Terraform Keycloak provider is not used, because it must reach Keycloak at plan time and would be configured from a resource created in the same apply |
| Roles | one Keycloak group per canonical role, named exactly as the role, so the default `role_mapping` is the role to its same-named group; a group mapper emits them in `role_claim` (`groups`) |
| Clients | `perimeter`, `superset`, `openmetadata`, `trino`; confidential, authorization-code only, redirect URIs limited to the routes of the service they front |
| Secrets | `random_password` into a Kubernetes Secret per client (`keycloak-client-<consumer>`, key `client-secret`); the Job reads them through `secretKeyRef` and `$(env:...)` substitution, so no value is in the realm file, the Job spec or the contract |
| Mail | `smtpServer` in the realm file from the non-secret profile fields. The login reaches the realm Job only as `SMTP_USERNAME`/`SMTP_PASSWORD` from `secretKeyRef`, substituted by `keycloak-config-cli` like a client secret, so it is in no ConfigMap, Terraform variable, output or state. `verifyEmail` and `resetPasswordAllowed` are true only when SMTP is configured: a reset link that cannot be mailed is a dead end. Self-registration stays off |
| Users | never in Terraform or the realm file; created in Keycloak's admin console, so onboarding needs no apply. The bootstrap admin is in Secret `keycloak-admin-creds` |
| Route | `https://auth.<base_domain>`, the `shared/identity` route; never granted a role and never behind the perimeter |
| Issuer | Keycloak pins every URL, `iss` included, to `https://auth.<base_domain>` whatever address a caller used |

The adapter declares `capabilities` `groups_in_token` and `logout_endpoint`
true, `email_delivery` true when SMTP is configured, and `admin_api` false
until `olf users` exists (#331). The consumers are
wired by #25, #26 and #176; none is yet.

Rotating a client secret is a Terraform replace of its `random_password`
followed by the realm Job re-running; the runbook, with the exercise that
closed the #181 rotation criterion, is `docs/setup/identity-secret-rotation.md`.

Realm-as-code and user records: groups, clients and the realm only. Two
local-cluster risks were checked rather than assumed. `*.localhost` is
loopback for some clients, so back-channel token and JWKS calls need the pod
to reach Traefik at `*.<base_domain>` and trust the local CA; the local root
does both (ADR 0013, decision 7), and `olf e2e run` fetches the discovery
document from a pod and compares `iss`. curl is such a client: it resolves
`*.localhost` to loopback itself without asking DNS, while getaddrinfo-based
clients get the Traefik address from cluster DNS. Single sign-on across hosts needs a cookie
on `.<base_domain>`: curl (libpsl) and headless Chromium both accepted a
`Domain=.olf.localhost` cookie and sent it to a sibling host, and both
rejected `Domain=.localhost`. Firefox and Safari were not tested.

### Seam 3 — perimeter (decided, not built; #176)

oauth2-proxy behind Traefik `forwardAuth`; each route's allowed roles come
from seam 1 translated through `role_mapping`. Traefik attaches middleware per
`Ingress` and ADR 0013 renders one `Ingress` per namespace, so routes with
different grants need separate `Ingress` objects; #176 rewrites ADR 0013
accordingly. The identity route (`auth.<base_domain>`) is never behind the
perimeter.

### Seam 4 — admin (decided, not built; #331)

Assigning roles is an optional capability of the issuer. Where it is absent,
`olf users` only reads.

## Policy documents

Three written gates bind the implementation of seams 3 and 4 and the
credential-producing work. They are policy for #176, #25, #26, #331 and #46 to
implement; none is built.

| Gate | Decision | Document |
| --- | --- | --- |
| Credentials (#181) | Identity credentials are generated by an idempotent in-cluster bootstrap Job, reused on redeploy, never read by Terraform. The Terraform-generated Keycloak admin and client secrets are in state today and migrate in a follow-up | [`identity-credentials.md`](../architecture/identity-credentials.md) |
| Sessions (#177) | Perimeter cookie 30 min idle, 10 h absolute; access token 5 min; revocation delay at most 5 min at the perimeter and 15 min in native Superset and OpenMetadata sessions; platform-wide logout; fail closed on issuer outage; cluster-administrator recovery works with Keycloak down | [`identity-sessions-and-recovery.md`](../architecture/identity-sessions-and-recovery.md) |
| Callers (#178) | Every authentication path is human (class 1), in-cluster or automation workload (class 2), or cloud workload (class 3). Workstation and CI automation reaches services by RBAC-authorized port-forward or in-cluster execution, never through the perimeter or as a human account | [`identity-callers.md`](../architecture/identity-callers.md) |

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
- The local Keycloak adds one always-on pod to `olf-system` (input to the #171
  fixed-cost budget), measured on a fresh kind deploy with the realm applied:

  | Item | Value |
  | --- | --- |
  | CPU request | 200m |
  | Memory request / limit | 768Mi / 1Gi |
  | Resident memory (cgroup `memory.current`, idle) | about 640 MiB; `memory.peak` reached the 1Gi limit while `start` ran its build step, so the limit is not safe to lower |
  | PostgreSQL load | the `keycloak` database, 13 MB, 2 connections; no new server |
  | Realm Job | runs on a change of realm or client secrets, 50m / 256Mi request, exits in seconds |

- The provider-binding digest recorded at activation covers `shared.identity`,
  so the first activation after upgrading reads as a binding change.

## History

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

Outbound SMTP (#24): `spec.identity.smtp`, the `email_delivery` capability, the
realm `smtpServer` and the deploy-time Secret check are built. The shared
namespace is created by the platform apply, so on a first deploy the Secret
cannot exist before it: deploy once without `smtp`, create the Secret, then add
`smtp` and re-run the platform phase. Delivery against a test mail sink is
checked separately; this ADR does not claim a configured relay was tested.
