# ADR 0021: The platform API — one backend for the portal, `olf` and agents

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔, except D5's discovery document (`olf login` reads the Deployment Profile instead) and D1's catalog pin (no release has published the image; `olf` builds it) · AWS ✘ · Azure ✘ (only `environments/local` deploys the API) |

The routes, their permission rows and what each route does are in the
[platform API reference](../reference/platform-api.md).

## Context

[ADR 0020](0020-infrastructure-common-and-platform-cli-surfaces.md)'s platform
surface lets a person, or their local agent, work with a Dex session and no
kubeconfig. The operations it needs (read runs and jobs, read domains and data
products, move Superset reports, read archived logs) are also what the portal
needs to grow from a route index into a platform UI. The portal is a React
Router application on Node and cannot import the Python that already does
them, so calling every service from each client would write that logic twice.

The services split in two. Trino, through Polaris, and OpenMetadata validate a
person's token and authorize by it. Superset signs API callers in with its
database provider, Dagster checks nothing, and the object store takes no OIDC
token: a call there needs a service credential, and something else must decide
access. OpenMetadata checks a token's signature and expiry but not its `aud`,
and names the user by `preferred_username` (#536 spike, OpenMetadata 1.13.6).

## Decision

**D1. One Python backend, the platform API, serves the platform operations
to the portal, `olf`'s platform commands and agents.** It serves a versioned
HTTP API (`/api/v1`) and is a platform-phase component
([ADR 0002](0002-deployment-lifecycle.md)): Terraform deploys it, pinned by
digest in `release/component-catalog.yaml`, and it reads no user code at
startup. It lives in this repository as the `openlakeforge-platform-api`
distribution in `packages/platform-api/` with its image in
`images/platform-api/`; it is not published to PyPI and keeps its own locked
environment, so FastAPI and uvicorn never enter `olf`'s. The same image hosts
the alert evaluator's entrypoint ([ADR 0023](0023-alert-evaluation-and-webhook-delivery.md)),
run as a separate process. It is served at `https://<base_domain>/api/`, a path
on the portal's host behind the perimeter's lowest role floor, so a browser
session reaches it without CORS. It accepts only audience `olf-cli`: a browser
session's ID token carries it through Dex cross-client trust
([ADR 0014](0014-identity-and-authorization.md)), and a Traefik middleware on
this route alone passes it on. With an external issuer there is no such token,
the settings render `person_query: false`, and a browser cookie alone gets 401;
without `spec.identity`, D8 applies. The portal stays in its own repository as
a thin same-origin client that holds no data-plane authorization. Enforced by:
`infra/terraform/modules/access/platform-api`.

**D2. The identity a call carries follows the service.** Where a service
validates the person's token and authorizes by it, the call carries that
token: Trino through Polaris grants ([ADR 0019](0019-data-plane-authorization-deferral.md)),
and OpenMetadata by the user's own OpenMetadata roles. A CLI command may call
the service's public route directly, as `olf query` reaches Trino's
`query_api`, or the API forwards the token unchanged, so the CLI and the portal
share one client. The API holds no OpenMetadata credential and never reads
table data with a stage principal. Superset, Dagster and the object store
cannot take the token, so the API calls them with its own credentials and
decides access itself (D3). On the OpenMetadata path someone in front of
OpenMetadata checks the audience (the perimeter on a direct call, the API's
token validation on a forwarded one), and every caller requests the `profile`
scope; a person OpenMetadata has never seen is registered once through
self-signup with their own token, then the read is retried. A person's call to
the API, and a forwarded call onward, are class 1; a call onward with a
service credential is class 2 ([identity-classes.md](../architecture/identity/identity-classes.md)).
Enforced by: `openlakeforge_platform_api/reads.py`, `queries.py`, and
`tools/olf/olf/platform/session.py` (scopes).

**D3. The platform API is the enforcement point for its service-credential
calls.** Whoever reaches the API reaches what its credentials can do, so:

- It validates every token itself: signature against the issuer's keys,
  issuer, expiry, and an audience from an explicit list. A perimeter header is
  not proof of identity. A token it forwards is validated first, since on that
  hop the API is the only audience check, and it relays no request it cannot
  attribute.
- It derives the canonical role as ADR 0014 maps it and checks each operation
  against a permission matrix kept as data and tested: the lowest role, the
  stages, and an optional `role_stages` limiting a named role to some of them,
  in the monotonic-floor style of ADR 0014. A route without a row is refused.
- It admits one service principal besides people, the alert evaluator
  (ADR 0023, decision 11), by a TokenReview that names exactly its
  ServiceAccount and audience, and only on a row that lists it. That row is the
  services-only health route. The review fails closed (503), and the token is
  never forwarded.
- It holds the narrowest credential each service offers, mounted from a
  Secret (architectural rule 7): a non-admin Superset service user that co-owns
  the report assets it may overwrite, no OpenMetadata credential, and one
  object-store key per purpose (log reader, quality reader, alert state reader,
  audit writer, files), each with its own client.
- It reaches Dagster over its own rows in `release/network-flows.yaml`; the
  matrix is the only check on what it asks there.
- It audits before it acts: every operation that changes state stores an
  `attempted` record naming the person, the operation, the stage and the
  target, and answers 503 without acting if it cannot. Outcomes and refusals
  follow; a caller with no valid token is logged to stdout only. The records
  are tamper-evident against everyone but the API, not append-only.
- It guards the files explorer, since the files key decides nothing: the audit
  prefix is read-only through the API, a key the store would normalise into
  another is refused, and every object write is conditional, so no single key
  is silently overwritten. `platform-admin` uses every enabled stage,
  `data-engineer` only `dev`, and `analyst` and `viewer` nothing, reads
  included.
- It refuses cross-site writes without tokens or a session store: a foreign
  `Origin`, a cross-site `Sec-Fetch-Site`, or a body type other than the one the
  route takes. It serves no CORS headers.
- It is stateless with a read-only root filesystem. Saved queries or history
  would bring the shared PostgreSQL in as a decision of their own.

Enforced by: `openlakeforge_platform_api/permissions.toml` and
`packages/platform-api/tests/test_permissions.py`, `test_audit.py`,
`test_csrf.py`, `test_files_writes.py`, `test_service_tokens.py`.

**D4. One library, two hosts.** The service clients and the operations built
on them live in `packages/lakehouse-services/`, and an operation takes a client
and credentials and never resolves them itself: no `typer`, no `os.environ`, no
Kubernetes Secret read, no port-forward. `olf infra` supplies port-forwards and
Secrets read with the caller's kubeconfig; the API supplies in-cluster Service
DNS and its mounted Secrets, or the person's token (D2). The API calls the
library concurrently, so it holds no module-level state. The artifacts phase
keeps calling the library directly with infra credentials: routing it through
the API would make every deployment depend on the API and give CI a platform
credential, which ADR 0020's non-interactive authentication contract governs
and which is not built. Enforced by: review; no test checks the library's
imports yet.

**D5. The OpenAPI document is the contract between the API and its clients.**
It is committed as `docs/schema/platform-api.openapi.json` and checked against
the app in CI, so a change to an endpoint is a reviewed change to a schema; the
portal generates its types from it, and a breaking change is a new path
version, so the two repositories need not merge in lockstep. If most portal
changes come to need a paired backend change, moving the portal into this
repository is reconsidered. The API also serves a discovery document (the
issuer, the `olf-cli` client id, the API and `query_api` URLs, and each stage
with its catalog), so `olf login <base URL>` needs nothing else. Not built: no
route serves it. Enforced by: `packages/platform-api/tests/test_openapi.py`.

**D6. Deployment and recovery stay infra and never go through the API.**
`deploy`, `plan`, `destroy`, `doctor`, `status`, `forward`, `break-glass`,
`auth`, `secrets rotate`, `k8s`, `project build|deploy|image`, `diagnostics
collect` and `e2e` are infra commands. The API holds no Kubernetes or cloud
permission beyond reading its own mounted Secrets, D7's listing and D3's
TokenReview, so `platform-admin` in the UI grants no cluster rights
(ADR 0020).

**D7. Platform health reads the cluster through a namespaced Role for the
API's own ServiceAccount.** In each stage namespace the API may `list` pods,
Deployments and StatefulSets, and nothing else: no `get` or `watch`, nothing in
`olf-system`, no `secrets`, `pods/log`, `pods/exec` or `events`; the module
refuses `kube-system` and the API's own namespace. The grant, the mounted token
and the F20 egress to the API server land only together with a route that uses
them, and `environments/local` requires `probe_namespaces` to name a stage
namespace whenever it deploys the API. Its one ClusterRole is `tokenreviews`
`create`, for D3's service principal. A probe that times out, errors or is
refused is `unknown`, a component the deployment leaves out is `disabled`, a
200 never counts alone, and a selector that matches nothing is `unknown`. The
names on each service row are part of the contract, and `last_success_at`
reports only a component's own evidence, never the probe time. Enforced by:
`modules/access/platform-api/probe_rbac.tf` and its Terraform tests,
`packages/platform-api/tests/test_service_probes.py`.

**D8. With authentication off, every caller is one anonymous principal
holding the highest role.** A local profile with `spec.access` and no
`spec.identity`, which profile validation allows only on a `*.localhost` base
domain, deploys the API with no issuer and `anonymous_role`. A call with no
token is `anonymous` with that role and the matrix applies unchanged; an issuer
token is refused with 401, and D3's service principal is still checked. The API
refuses to start with `anonymous_role` beside an issuer or audiences, or with a
`public_origin` off `*.localhost`. Changes are audited as `anonymous`, and the
OpenMetadata routes are not configured. Queries run as the Trino user
`anonymous` with no bearer: the stage catalogs are not keyless here
(ADR 0019 D9), each stage Trino's catalog credential serves every
request, and `olf`'s Polaris bootstrap grants it `CATALOG_MANAGE_CONTENT` on its
catalog; with identity the grant is removed. `olf` reaches the API over the
routes' own scheme. Enforced by: `openlakeforge_platform_api/settings.py` and
`packages/platform-api/tests/test_anonymous.py`.

**D9. Log reads are bounded.** Both archived-log routes read the archive on one
shared pool of two workers. A read gets 20 seconds from when a worker takes it
(502 past that), a read that waits more than 5 seconds for a worker is answered
503 with `Retry-After: 5`, and a client that disconnects cancels its read. The
bound is what holds the API inside its memory limit. The archive format is
[ADR 0022](0022-pod-log-collection-and-archive-format.md)'s; this decision owns
how the routes read it. Enforced by: `openlakeforge_platform_api/log_reads.py`
and `packages/platform-api/tests/test_log_reads.py`.

## Consequences

- Platform logic is written once, in Python, and the portal, `olf` and agents
  share it.
- The API holds service credentials for Superset and the object store (its
  files key reaches every enabled stage's buckets) and is the authorization
  point for them and for Dagster: its matrix and token validation carry the
  review weight the perimeter carries for the UIs.
- OpenMetadata keeps its per-user authorization on every path, at the price
  that the audience check in front of it is the only one, and every caller on
  that path handles a first-time user's registration.
- Only the multi-key folder move of the files explorer is not atomic: a
  failure midway leaves part of the folder moved.
- No per-person limit on queries in flight: it would need state shared between
  replicas.
- One more pod in `olf-system` with `spec.access`, recorded in
  [ADR 0013](0013-service-access-and-ingress.md)'s table.
- A compromised API learns the names, labels, environment values and status of
  pods and controllers in the stage namespaces (D7); no Secret, log, exec or
  write.

## Boundaries

| Not decided here | Where |
| --- | --- |
| Routes, permission rows, per-route limits and status codes | [Platform API reference](../reference/platform-api.md) |
| Roles, the role claim and the perimeter's floors | [ADR 0014](0014-identity-and-authorization.md) |
| What a person may read and write in the data plane | [ADR 0019](0019-data-plane-authorization-deferral.md) |
| Command surfaces and which command calls the API | [ADR 0020](0020-infrastructure-common-and-platform-cli-surfaces.md) |
| The API's network flows | [ADR 0017](0017-local-network-policy-and-rbac.md), `release/network-flows.yaml` |
| The log archive's format and collection | [ADR 0022](0022-pod-log-collection-and-archive-format.md) |
| Alert evaluation, its state and its service principal | [ADR 0023](0023-alert-evaluation-and-webhook-delivery.md) |
| The metrics series and ranges | [ADR 0024](0024-metrics-prometheus-reference-and-read-path.md) |
| Health states and the probe components | [Health signals](../reference/health-signals.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Each platform command calls each service's public route itself (ADR 0020 as first written) | The same logic again in the portal, which cannot import Python |
| A top-level `services/` directory for the API | A third layout for first-party Python |
| An OpenMetadata bot credential in the API (#665) | Replaces OpenMetadata's per-user decision with the API's |
| The Superset `admin` account | Broader than import and export need; a non-admin service user can do both |
| A probe component with its own identity for health (D7) | A second image, pin, NetworkPolicy and failure to explain, to avoid a small, read-only, namespaced grant; kept as the fallback if a signal needs more |
| The artifacts phase through the API (D4) | Every deployment would depend on the API, and CI would need a platform credential |
| An unbounded log read per request (D9) | A read the browser had cancelled kept running beside its retry until the API was OOMKilled |

## History

- Undated: New record. Rewrote ADR 0020's platform row: platform commands
  reach the platform API, not each service's public route.
- Undated (#665): D2 rewritten: the identity follows the service, not a
  data-plane/control-plane split; D3 applies to service-credential calls only,
  and the API's OpenMetadata bot credential was dropped.
- Undated (#666): D1's location settled as `packages/platform-api` and
  `images/platform-api`.
- Undated (#670): D3 gained audit-before-act with durable objects under a
  write-only key, and the non-admin Superset service user `olf_platform_api`
  (formerly open item 3).
- Undated (#799): D1: a browser session reaches the API through Dex
  cross-client trust (formerly open items 1 and 2); D3 gained the cross-site
  write guard.
- Undated: D1: the portal is a React Router app, no longer Astro.
- 2026-11-16 (#1126, #1156): D7 added; D6 gained its exception; the grant,
  token and F20 egress land only with their caller.
- 2026-11-16 (#1114): D3 gained the files key, the files explorer's guards and
  `role_stages`.
- 2026-11-16 (#1167): D1: the image also hosts the alert evaluator's
  entrypoint, as a separate process.
- 2026-11-17 (ADR 0023 slice AP-c): D3 admits the alert evaluator as a service
  principal on the services-only health route; D7 gained the `tokenreviews`
  ClusterRole.
- 2026-11-18 (#1258): D3 gained the read-only alert state key.
- 2026-11-18 (#1262): D7: the service rows' names are part of the contract,
  and `last_success_at` reports only a component's own evidence.
- 2026-11-18 (#1270): D8 added, then extended the same day so anonymous
  queries read through each stage's catalog credential.
- 2026-11-20 (#1325): D2: the files key also reads a run's retained quality
  reports under the stage's activation prefix.
- 2026-11-21 (#1365): Log reads bounded by a shared two-worker pool, a
  20-second deadline and a 503 with `Retry-After`; stated as D9 since the
  rewrite below.
- 2026-11-23: Rewritten in the standard form. Decisions 1 to 8 are D1 to D8;
  the log-read bound is D9; routes, permission rows, per-route behaviour and
  the files explorer's mechanics moved to the
  [platform API reference](../reference/platform-api.md), and the probe table
  to [Health signals](../reference/health-signals.md). No decision changed.
