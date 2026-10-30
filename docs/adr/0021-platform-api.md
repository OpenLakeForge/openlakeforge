# ADR 0021: The platform API — one backend for the portal, `olf` and agents

## Status

Decided, **build step 2 begun**: `packages/lakehouse-services` holds every
service client — the HTTP transport, Dagster, OpenMetadata and Superset. The
operations built on them (report import and export, metadata deployment,
domain listing) are still in `olf/infra`. The platform API's skeleton is in
`packages/platform-api`: token validation, the permission matrix,
`/api/healthz` and `/api/v1/whoami`, with no service endpoint. It is not
deployed yet.
What binds today is the direction: the platform surface of [ADR 0020](0020-infrastructure-common-and-platform-cli-surfaces.md)
reaches services through one backend, written in Python in this repository,
and new code that would duplicate a platform operation in another language or
another host follows the split below.

## Context

ADR 0020 decided a platform surface: commands a person, or their local agent,
runs with a Dex session and no kubeconfig. It assumed each platform command
would call each service's public route itself. The operations that surface
needs — read pipeline runs and jobs, read domains, data products and glossary
from OpenMetadata, read and move Superset reports, read archived logs — are
also what the portal ([openlakeforge-portal](https://github.com/OpenLakeForge/openlakeforge-portal))
needs to grow from a route index into a platform UI. Calling every service
from the CLI would put that logic in `olf` and again in the portal.

The logic already exists, in Python, under the infra surface:

| Operation | Today | How it authenticates |
| --- | --- | --- |
| Superset report import and export | `olf/infra/superset.py` | Port-forward (`superset_base_url`), the admin password from the `superset-credentials` Secret (`admin_password_for`), the stage's Trino password from its Secret |
| OpenMetadata domains | `olf infra openmetadata list-domains`, `openlakeforge_services/openmetadata.py` | Port-forward, the automation bot's JWT from its Secret (`olf/infra/openmetadata/_auth.py`) |
| Dagster runs and jobs | `openlakeforge_services/dagster.py`, used only by `olf e2e` | None: Dagster's GraphQL API is unauthenticated ([ADR 0017](0017-local-network-policy-and-rbac.md)) |
| Archived logs | Written by `libs/k8s_log_archive.py` to `OPENLAKEFORGE_LOG_BASE_URI` (default `s3://openlakeforge-ops/logs`) | Object-store credentials |

Two facts shape the decision. The portal is an Astro application on Node, so
it cannot import this code. And none of these operations can simply forward a
person's token: Superset and Dagster do not accept a Dex token, and every path
above uses an admin or service credential.

## Decision

### 1. One backend: the platform API

A Python service, the **platform API**, serves the platform operations over a
versioned HTTP API (`/api/v1`). The portal's pages, `olf`'s platform commands
and agents are its clients. It is a platform-phase component
([ADR 0002](0002-deployment-lifecycle.md)): Terraform deploys it, pinned by
digest in `release/component-catalog.yaml`, and it reads no user code at
startup.

It lives in this repository, beside the code it reuses and the contracts it
versions with: the `openlakeforge-platform-api` distribution in
`packages/platform-api/`, next to the two packages it builds on, and an image
defined in `images/platform-api/`, as `project-code` and `superset` are. A
top-level `services/` directory would be a third layout for first-party Python.
Unlike the other packages it is not published to PyPI; it ships only as the
image. It keeps its own locked environment (`packages/platform-api/uv.lock`),
so FastAPI and uvicorn never enter `olf`'s.

It is served at `https://<base_domain>/api/` behind the perimeter, the same
origin as the portal, so a browser session reaches it without CORS. The portal stays in its own repository and stays Astro; React is
added as islands where a page is interactive, and no framework migration is
part of this decision.

### 2. Two classes of call

| Class | Examples | Identity on the service call | Who decides access |
| --- | --- | --- | --- |
| **Data plane**: reading or writing table data | `olf query`, a query page in the portal | The person's Dex token, end to end ([ADR 0019](0019-data-plane-authorization-deferral.md)) | Polaris grants |
| **Control plane**: metadata, runs, reports, logs | Runs and jobs, domains and products, report export and import, log reads | The platform API's own service credentials | **The platform API** |

The platform API never reads table data with a stage principal on a person's
behalf: that would bypass the Polaris grants ADR 0019 builds. `olf query`
reaches Trino's `query_api` route directly with the person's token and does not
pass through the platform API; the portal's query page does pass through it and
forwards the person's token unchanged.

Under the classification rule in
[identity-classes.md](../architecture/identity/identity-classes.md), a person's
call to the platform API is class 1, and the platform API's control-plane call
onward to Superset, OpenMetadata, Dagster or the object store is class 2: a
workload with a service credential, acting after it has checked a class 1
identity.

### 3. The platform API is an enforcement point

Because control-plane calls carry a service credential, whoever reaches the
platform API reaches what those credentials can do. It therefore:

- **validates every token itself**: signature against the issuer's keys,
  issuer, expiry, and an audience from an explicit list. A header set by the
  perimeter is not proof of identity on its own;
- **derives the canonical role** from the role claim exactly as
  [ADR 0014](0014-identity-and-authorization.md) maps it, and checks each
  operation against a **permission matrix kept as data and tested** — an
  operation, the lowest role allowed, and the stages it applies to — in the
  monotonic-floor style of ADR 0014's perimeter;
- **holds the narrowest credential each service offers**, mounted from a
  Secret (architectural rule 7): an OpenMetadata bot with a non-admin role, a
  Superset service user rather than `admin` wherever Superset's API allows the
  operation, a read-only object-store key for the log prefix;
- **reaches Dagster's GraphQL API through its own rows in
  `release/network-flows.yaml`**, and its permission matrix is the only check
  on what it asks there, since Dagster checks nothing;
- **writes an audit record** naming the person for every operation that
  changes state;
- **is stateless** with a read-only root filesystem until a feature needs
  storage; saved queries or history bring the shared PostgreSQL in as their
  own decision.

### 4. One library, two hosts

The service clients and operations move out of `olf` into a
library package, `packages/lakehouse-services/`, following the
`packages/domain-model` precedent. Its rule is **inject, don't fetch**: an
operation takes a client and credentials and never resolves them itself — no
`typer`, no `os.environ`, no `k8s.secret_value`, no port-forward. Each host
supplies its own resolver:

| Host | Reaches a service through | Credentials from |
| --- | --- | --- |
| `olf infra` (deploy, artifacts phase, e2e) | Port-forward or the applied contract's endpoint | Kubernetes Secrets with the caller's kubeconfig |
| The platform API | In-cluster Service DNS | Secrets mounted into its pod |

`olf` stays a single-operator CLI (AGENTS.md, Non-goals). The library does not
inherit that: the platform API calls it concurrently, so it holds no
module-level state.

**The artifacts phase keeps calling the library directly**, with infra
credentials. `olf infra deploy --phase artifacts` imports reports and metadata
through `olf/infra/deployment/artifact_steps.py`; routing that through the
platform API would make every deployment depend on the API being up and give
CI a platform credential, which ADR 0020's non-interactive authentication
contract governs and which is not built. It is revisited when that exchange
exists.

### 5. The contract between the API and its clients

The API's OpenAPI document is committed under `docs/schema/` and checked
against the code in CI, so a change to an endpoint is a reviewed change to a
schema, as the descriptor and contract schemas already are. The portal
generates its types from it. A breaking change is a new path version, so the
two repositories do not have to merge in lockstep; if most portal changes come
to need a paired backend change, moving the portal into this repository is
reconsidered.

The platform API also serves a discovery document: the issuer, the `olf-cli`
client id, the API and `query_api` URLs, and each stage with its catalog.
`olf login <base URL>` needs nothing else. The platform surface cannot read
Terraform state or `OPENLAKEFORGE_QUERY_TRINO_CATALOG`, which `olf infra query`
gets from the applied contract today.

### 6. What stays infra

Deployment and recovery stay infra and never go through the platform API:
`deploy`, `plan`, `destroy`, `doctor`, `status`, `forward`, `break-glass`,
`auth`, `secrets rotate`, `k8s`, `project build|deploy|image`, `diagnostics
collect`, `e2e`. The platform API has no Kubernetes or cloud permission beyond
reading its own mounted Secrets, so `platform-admin` in the UI still grants no
cluster rights (ADR 0020).

## Consequences

- Platform logic is written once, in Python, and the portal, `olf` and agents
  share it through one API.
- The platform API holds service credentials and is an authorization point.
  Its permission matrix and token validation carry the review weight the
  perimeter carries today.
- One more pod in `olf-system`. Its footprint is not measured; it is recorded
  in ADR 0013's table when the module lands.
- The extraction of `packages/lakehouse-services` is a refactor with no
  behaviour change and can land before any of the API.
- AGENTS.md's repository map gains `packages/lakehouse-services/` and the
  platform API's source and image directories when they exist.

## Open items

| # | Item | Why it is open |
| --- | --- | --- |
| 1 | How a browser session's token reaches the platform API | The perimeter sets `--set-xauthrequest=true` but does not pass the access token on; passing it needs oauth2-proxy and Traefik `authResponseHeaders` configuration, not yet written or run |
| 2 | A token Trino accepts for the portal's query page | Trino requires the audience `olf-cli` (ADR 0019, step 5), and a browser session's token is the perimeter's. Dex cross-client trust or a client Trino also accepts are candidates; neither is tested. It changes ADR 0014 and ADR 0019 |
| 3 | The narrowest credential per service | Which Superset and OpenMetadata operations a non-admin service user can perform has not been checked against Superset 6.1.0 and OpenMetadata 1.13.6 |

## Build order

Each step is one pull request within AGENTS.md's size rule.

1. Extract `packages/lakehouse-services` from `olf/common/clients`,
   `olf/infra/superset.py` and `olf/infra/openmetadata`; `olf infra` uses it.
   No behaviour change.
2. Platform API skeleton on the local provider: token validation, the
   permission matrix, `/healthz`, the discovery document; its Terraform
   module, flow rows and catalog pin.
3. Read endpoints: Dagster runs and jobs, OpenMetadata domains and products,
   archived logs.
4. `olf login`, `whoami`, `logout` and the read commands as clients of the API.
5. State-changing endpoints — report export and import, launching a run — with
   audit records.
6. Platform `olf query` on ADR 0019's person path (ADR 0020, Command
   ownership).

## History

New record. Rewrites ADR 0020's platform row: platform commands reach the
platform API, not each service's public route.

Build step 1, first part: the transport and the Dagster client moved to
`packages/lakehouse-services`, a third published distribution (ADR 0009).

Build step 1, second part: the OpenMetadata and Superset clients followed;
`olf/common/clients` no longer exists.

Build step 2, first part (#666): the service skeleton in
`packages/platform-api`, which settles open item 4 (where the source and image
live, decision 1). The proposed `services/platform-api/` was not used.
