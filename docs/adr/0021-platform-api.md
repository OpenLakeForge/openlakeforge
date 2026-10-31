# ADR 0021: The platform API — one backend for the portal, `olf` and agents

## Status

Decided, **build step 1 done, build steps 3, 4 and 5 begun**: `packages/lakehouse-services`
holds every service client — the HTTP transport, Dagster, OpenMetadata and
Superset — and the operations built on them: Superset report import and export
(`openlakeforge_services.reports`) and OpenMetadata metadata deployment
(`openlakeforge_services.governance`). `olf/infra` keeps only what resolves
endpoints and credentials from a deployment. The platform API in
`packages/platform-api` has token validation, the permission matrix,
`/api/healthz`, `/api/v1/whoami` and the first read endpoints: Dagster jobs and
runs per stage, each stage's archived pod logs, read with a read-only
object-store key scoped to the stages' log prefixes, and OpenMetadata domains
and data products with the caller's token forwarded. `olf jobs list`, `olf runs
list|show`, `olf domain list` and `olf product list` read through it with the
signed-in person's token. The local provider deploys it with `spec.identity`
(`modules/access/platform-api`), from an image `olf` builds, since no release
has published one to pin. Its first state-changing route launches a DEV run,
audited as decision 3 says, with a write-only key the deployment mounts;
`olf runs launch` calls it.
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

| Operation | Today | How it authenticates | Does the service accept the person's token? |
| --- | --- | --- | --- |
| Superset report import and export | `olf/infra/superset.py` | Port-forward (`superset_base_url`), the admin password from the `superset-credentials` Secret (`admin_password_for`), the stage's Trino password from its Secret | No. The REST API signs callers in with `provider: "db"` on `/api/v1/security/login` ([identity-service-permissions.md](../architecture/identity/identity-service-permissions.md)); accepting an external issuer's token would need a custom security manager (#536, out of scope) |
| OpenMetadata domains | `olf infra openmetadata list-domains`, `openlakeforge_services/openmetadata.py` | Port-forward, the automation bot's JWT from its Secret (`olf/infra/openmetadata/_auth.py`) | **Yes.** It trusts Dex's signing keys (`publicKeyUrls` lists `<issuer>/keys`), names the caller by `preferred_username` (`jwtPrincipalClaims`), and authorizes by that user's own OpenMetadata roles, which the role sync sets ([ADR 0014](0014-identity-and-authorization.md)); `infra/terraform/modules/governance/openmetadata/main.tf` |
| Dagster runs and jobs | `openlakeforge_services/dagster.py`, used only by `olf e2e` | None: Dagster's GraphQL API is unauthenticated ([ADR 0017](0017-local-network-policy-and-rbac.md)) | No: Dagster has no authentication or authorization of its own |
| Archived logs | Written by `libs/k8s_log_archive.py` to `OPENLAKEFORGE_LOG_BASE_URI` (default `s3://openlakeforge-ops/logs`) | Object-store credentials | No: no module under `infra/terraform/modules/storage/` configures an OIDC issuer |

Two facts shape the decision. The portal is an Astro application on Node, so
it cannot import this code. And the services split in two: Superset, Dagster
and the object store cannot take a person's token, so a call there needs a
service credential and someone else must decide access; OpenMetadata takes the
person's token and decides access itself, per user.

The #536 spike (comment "Spike results", OpenMetadata 1.13.6 read from source,
not run against a deployment) adds two facts about that OpenMetadata path:

- OpenMetadata's `JwtFilter` checks a token's signature and expiry, not its
  `aud` or `iss`. Any token Dex signed passes it, so the perimeter's audience
  check is the only audience check on this path. This holds for browser
  tokens today too.
- OpenMetadata derives the user from `preferred_username` and refuses a token
  without it, so a caller must request the `profile` scope (ADR 0014 already
  requires it of the `olf-cli` client). The spike notes Dex's GitHub connector
  sets the claim and its mock connectors do not, so the e2e stub path needs
  checking.

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
origin as the portal, so a browser session reaches it without CORS. The route
is a path on the portal's host, not a `shared.access.routes` entry, and sits
behind the perimeter's lowest role floor: every canonical role clears it, and
the API's own permission matrix decides above that. Its accepted audience is
the `olf-cli` client until open item 1 brings a browser session's token.

The portal stays in its own repository and stays Astro; React is added as
islands where a page is interactive, and no framework migration is part of
this decision.

### 2. The identity a call carries follows the service

**Where a service validates the person's token and authorizes by it, the call
carries that token.** Service credentials are used only where the service
cannot accept the person's token. Per service:

| Service | Operations | Identity on the service call | Who decides access |
| --- | --- | --- | --- |
| Trino, through Polaris | Reading or writing table data: `olf query`, a query page in the portal | The person's Dex token, end to end ([ADR 0019](0019-data-plane-authorization-deferral.md)) | Polaris grants |
| OpenMetadata | Domains, data products, glossary | The person's Dex token, end to end | The perimeter's floor on the route the call entered by, then **OpenMetadata**, by the user's own OpenMetadata roles |
| Superset | Report export and import | The platform API's Superset service credential | **The platform API** |
| Dagster | Runs and jobs | None (Dagster checks nothing); the platform API reaches it over its own flow rows | **The platform API** |
| Object store | Archived log reads | The platform API's read-only key for the log prefix | **The platform API** |

A call that carries the person's token takes one of two routes. A CLI command
may call the service's public route directly, as `olf query` reaches Trino's
`query_api` route. Or the platform API forwards the token unchanged: `olf
domain list` and `olf product list` read OpenMetadata through the API's
`/api/v1/domains` and `/api/v1/data-products`, so the CLI and the portal share
one client and one first-time-user remedy, and the portal's query page will
reach Trino the same way. Either way the
platform API holds no OpenMetadata credential to act on a person's behalf, and
never reads table data with a stage principal: either would replace the
service's per-user decision with its own.

Two conditions bind every caller on the OpenMetadata path:

- **Someone in front of OpenMetadata checks the audience.** OpenMetadata
  checks signature and expiry, not `aud` (see Context). On a direct call that
  is the perimeter: it admits a bearer token only if it was issued to
  `olf-cli` (`--extra-jwt-issuers=<issuer>=olf-cli`,
  `infra/terraform/modules/access/perimeter/main.tf`) or, per the #536 spike,
  to the perimeter's own client, and enforces the route's role floor on it.
  The platform API reaches OpenMetadata over in-cluster Service DNS
  (decision 4), around the perimeter, so on a forwarded call its own token
  validation (decision 3: an audience from an explicit list) takes that
  place. A path with neither would accept any token Dex signed, including one
  issued to Superset or Trino.
- **The caller requests the `profile` scope**, because OpenMetadata keys users
  on `preferred_username`. `tools/olf/olf/platform/session.py` requests
  `openid email profile groups offline_access`.

A person who has never opened OpenMetadata's UI has no user there, and a
bearer request from them fails. The remedy (#536) registers the caller once
through `POST /v1/users`, which self-signup
(`enableSelfSignup = true`) allows for exactly the caller, then retries; the
role sync assigns roles on its next run. A platform API endpoint that forwards
a person's token to OpenMetadata does the same.

Under the classification rule in
[identity-classes.md](../architecture/identity/identity-classes.md), a person's
call to the platform API is class 1. A forwarded call onward stays class 1:
the service sees the person. A call onward to Superset, Dagster or the object
store is class 2: a workload with a service credential, acting after it has
checked a class 1 identity.

### 3. The platform API is an enforcement point for its service-credential calls

On a call it forwards with the person's token, the platform API decides
nothing: the service does, as it would for a direct call. It still validates
the token as below — signature, issuer, expiry and audience — before
forwarding it: on that hop it is the only audience check (decision 2), and it
never relays a request it cannot attribute.

A call to Superset, Dagster or the object store carries a service credential,
so whoever reaches the platform API reaches what those credentials can do.
For those calls it therefore:

- **validates every token itself**: signature against the issuer's keys,
  issuer, expiry, and an audience from an explicit list. A header set by the
  perimeter is not proof of identity on its own;
- **derives the canonical role** from the role claim exactly as
  [ADR 0014](0014-identity-and-authorization.md) maps it, and checks each
  operation against a **permission matrix kept as data and tested** — an
  operation, the lowest role allowed, and the stages it applies to — in the
  monotonic-floor style of ADR 0014's perimeter;
- **holds the narrowest credential each service offers**, mounted from a
  Secret (architectural rule 7): a Superset service user rather than `admin`
  wherever Superset's API allows the operation, a read-only object-store key
  for the log prefix, and no OpenMetadata credential;
- **reaches Dagster's GraphQL API through its own rows in
  `release/network-flows.yaml`**, and its permission matrix is the only check
  on what it asks there, since Dagster checks nothing;
- **writes an audit record** naming the person for every operation that
  changes state: who (the token's subject, email and canonical role), the
  operation, the stage, its target, and the outcome. Each record is one JSON
  line on stdout and, for an authenticated person, one object in the ops
  bucket under `audit/platform-api/date=<day>/`, written with a key that may
  only `Write` there. The API stores an `attempted` record before it acts and
  answers 503 without acting if it cannot; the outcome follows, and a lost
  outcome is logged rather than undoing what was done. A refusal of an
  authenticated person is stored too; one of a caller without a valid token
  goes to stdout only, so no anonymous caller can fill the bucket. The request
  id in each record and key is the API's own. SeaweedFS's `Write` also
  overwrites and deletes (`weed/s3api/s3api_server.go`, 4.48), so the records
  are tamper-evident against everyone but the API itself, not append-only;
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
| The platform API | In-cluster Service DNS | Secrets mounted into its pod, or the person's token, forwarded unchanged, for a service that authorizes it (decision 2) |

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
- The platform API holds service credentials for Superset and the object
  store, and is the authorization point for them and for Dagster. Its
  permission matrix and token validation carry the review weight the
  perimeter carries today.
- OpenMetadata keeps its per-user authorization on every platform path, and
  the platform API holds one credential fewer. The price is that the
  perimeter's audience check is the only one in front of OpenMetadata, and
  every caller on that path handles a first-time user's registration.
- One more pod in `olf-system`, with `spec.identity` only, recorded in
  ADR 0013's table. Its memory was measured in Docker, not yet on kind.
- The extraction of `packages/lakehouse-services` is a refactor with no
  behaviour change and can land before any of the API.
- AGENTS.md's repository map gains `packages/lakehouse-services/` and the
  platform API's source and image directories when they exist.

## Open items

| # | Item | Why it is open |
| --- | --- | --- |
| 1 | How a browser session's token reaches the platform API | The perimeter sets `--set-xauthrequest=true` but does not pass the access token on; passing it needs oauth2-proxy and Traefik `authResponseHeaders` configuration, not yet written or run. Portal pages that read OpenMetadata depend on it, since the API has no credential of its own to fall back to. That session's token is issued to the perimeter's client; OpenMetadata ignores `aud`, so it should pass, but no one has run it |
| 2 | A token Trino accepts for the portal's query page | Trino requires the audience `olf-cli` (ADR 0019, step 5), and a browser session's token is the perimeter's. Dex cross-client trust or a client Trino also accepts are candidates; neither is tested. It changes ADR 0014 and ADR 0019 |
| 3 | The narrowest credential per service | Settled for Superset (#670, see History): a non-admin service user, role `olf_platform_api`, can import and export. It must co-own the report assets to overwrite them, and needs data access to export them |

## Build order

Each step is one pull request within AGENTS.md's size rule.

1. Extract `packages/lakehouse-services` from `olf/common/clients`,
   `olf/infra/superset.py` and `olf/infra/openmetadata`; `olf infra` uses it.
   No behaviour change.
2. Platform API skeleton on the local provider: token validation, the
   permission matrix, `/healthz`, the discovery document; its Terraform
   module, flow rows and catalog pin.
3. Read endpoints: Dagster runs and jobs and archived logs with service
   credentials; OpenMetadata domains and products forwarding the person's
   token.
4. `olf login`, `whoami`, `logout` and the read commands as clients of the API,
   except where a command calls a service's route directly with the person's
   token, as `olf query` does Trino's (step 6).
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

Build step 1, third part: Superset report import and export moved to
`openlakeforge_services.reports`; `olf infra` keeps the stage resolution, the
port-forward and the Secret reads, and passes its `log` module as the
operations' progress reporter.

Build step 1, last part: OpenMetadata payloads, upserts and the deploy
sequence moved to `openlakeforge_services.governance`; `olf infra` keeps the
contract and descriptor resolution, the port-forward, the Secrets, the
ingestion Jobs, SSO and role sync. Domain listing needed no move: it was
already `OpenMetadataClient.list_domains`.

Build step 2, first part (#666): the service skeleton in
`packages/platform-api`, which settles open item 4 (where the source and image
live, decision 1). The proposed `services/platform-api/` was not used.
Second and third parts: the image and its release build, then the Terraform
module, flow rows and local wiring behind the perimeter.

Decision 2 rewritten (#665): the identity on a service call follows the
service, not a data-plane/control-plane split. OpenMetadata, which validates
the person's token and authorizes by it, gets that token, directly from the
CLI or forwarded by the API; service credentials remain for Superset, Dagster
and the object store. Decision 3 now applies to those service-credential
calls only, the OpenMetadata bot credential is dropped, and the context table
records which services accept the person's token.

Build step 3, first part (#668): Dagster jobs and runs under
`/api/v1/stages/{stage}/`, at the Dagster route's `data-engineer` floor, with
no credential; OpenMetadata domains and data products at the `viewer` floor,
forwarding the caller's token unchanged. Archived logs, first-time user
registration on the OpenMetadata path, and the endpoints' e2e through the
perimeter remain.
Second part: the local root renders each stage's Dagster GraphQL URL and
OpenMetadata's URL into the API's settings, and ADR 0017's F32 lets the API
reach them.

Build step 4, read commands (#669): `olf jobs list`, `olf runs list|show`,
`olf domain list` and `olf product list` call the platform API with the stored
session's token, and the API's refusal below a floor names the role it needs.
`olf domain list` reads OpenMetadata through the API rather than OpenMetadata's
own route, as decision 2 had planned, so the CLI and the portal share one client
and one first-time-user remedy.

Build step 3, archived logs (#668): the platform API reads each stage's pod log
archive with its own SeaweedFS identity, `Read` and `List` on the stages' log
prefixes only, minted in-cluster and mounted from a Secret as decision 3 says,
at the `data-engineer` floor.

Build step 5, first part (#670): decision 3 now says where audit records go. A
pod log is lost with the pod and nothing archives `olf-system`, so the durable
copy is an object of its own, written before the API acts. The permission
matrix marks an operation `changes_state`, and every such operation runs inside
the audit; without an audit key it answers 503. No operation is marked yet: the
first, launching a run, and the write-only key's deployment follow.
Second part: `POST /api/v1/stages/{stage}/runs` launches a job as the
signed-in person, at `data-engineer` and on DEV only, as #670 asks. Dagster
checks nothing, so the row is the only control. The run is tagged with who asked
for it and with the API's request id, by which a launch whose response was lost
is found rather than launched twice. The job is looked up among the jobs the
read endpoint lists, so an unknown name is answered at once instead of waiting
on, and reloading, code locations as the e2e's launch does.
Third part: the SeaweedFS credentials Job mints the API's audit key in
`olf-system`, identity `openlakeforge-platform-api-audit` with `Write` on
`audit/platform-api/` only, never generated by Terraform (#400). The pod reads it
under its own variable names, beside the log reader's key, and the local root
sets `audit_base_uri` from the same prefix as the grant. `olf runs launch` posts
the launch with the stored session's token and does not retry it.

Open item 3, Superset, checked against the 6.1.0 source (#670):
`ImportExportRestApi` is an Alpha view, not an Admin one
(`security/manager.py`), so a role of its own can hold `can_import_` there. The
import overwrites every asset with permission checks on: it needs `can_write`
on each type, and an existing dataset, chart or dashboard may be overwritten
only by an owner or an admin. Export filters charts by data access alone
(`charts/filters.py`), so the role needs the report schemas' `schema_access`.
The import form's `sparse=true` resolves databases already in Superset by uuid,
so the API sends no database: the service user holds no `can_write` on
databases, the API needs no Trino credential, and an uploaded report cannot
replace a stage's connection. The role is created from data by Superset's init
script (`fab import-roles`, which only adds permissions); the user's password is
minted in `olf-system` by one Job that copies it into each stage namespace.
The artifacts phase making the user a co-owner of what it imports and granting
the role `schema_access`, the API's mount and its flow to Superset follow.
The API reads each stage's Superset URL from its settings and that stage's
service-user password from a variable mounted from the Secret in `olf-system`,
and reaches Superset over ADR 0017's F35.
The artifacts phase, still importing as `admin` (decision 4), now also makes the
service user a co-owner of every dashboard it imports, with its charts and
datasets, keeping every other owner, and grants `olf_platform_api` the report
schemas in the step that grants `olf_data_access` them. An import keeps owners
and adds the importer, so the user stays an owner across later imports; without
the platform API there is no such user or role, and both steps do nothing.
