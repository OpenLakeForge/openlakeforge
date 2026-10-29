# Changelog

All notable changes to OpenLakeForge are documented here. This project is in
the **Alpha** lifecycle stage (see
[docs/release/releasing.md](docs/release/releasing.md#lifecycle-stages)):
breaking changes are allowed between alpha releases, with migration notes
recorded below for every tag. Until Beta,
only the latest alpha tag is maintained.

Release tags are create-only semantic versions (`release_tag_policy:
immutable-semver` in `release/component-catalog.yaml`) and are never
force-updated. See [docs/release/releasing.md](docs/release/releasing.md)
for how a release is cut and verified.

## [Unreleased]

### Added

- `source.yaml` accepts an optional `runtime` block naming the settings
  (`config`) and credentials (`secrets`) a source's loader reads, as key names
  only. Each stage binds the values in a ConfigMap `olf-source-<source>` and a
  Secret `olf-source-<source>-credentials`, which reach dlt as
  `SOURCES__<SOURCE>__<KEY>` (#546).

### Changed

- A local Full deploy pulls OpenLakeForge's prebuilt, signed Superset image by
  the digest pinned in `release/component-catalog.yaml` (`superset`) instead of
  building it on the machine; Slim does no Superset image work. The release
  publishes amd64 only, so other Docker engines still build it, as do
  maintainers who set `BUILD_SUPERSET_IMAGE=true` after changing
  `images/superset/`. AWS and Azure still build and push it (#542).
- Floe 0.8, the Python core, runs inside the project-code image
  (`floe-python` 0.8.2, `dagster-floe` 0.4.0). The code location builds the
  Floe manifest in memory from the project's contracts and a profile it
  renders for its own stage, and each domain runs as one Kubernetes Job on the
  same image (`olf-floe-job`): a whole-domain run passes `--domain`, a partial
  one only the entities it needs. There is no Floe runner image, no Job per
  entity, and no generated, published or versioned manifest (#533, ADR 0006).
- The Silver contracts are one Floe project: `lakehouse_code/silver/floe.yml`
  with named Bronze sources in `silver/sources.yml`, and per domain a
  `_domain.yml` of sink defaults plus one file per entity. `olf domain new`
  and `olf product new` write this layout. Each Silver asset depends on the
  Bronze resource its entity reads, and its quarantined rows are the asset
  `<domain>/rejected/<table>`.
- Silver runs on AWS Glue stages: each domain writes into its stage-prefixed
  Glue database (`overwrite` and `append`; merges need a REST catalog).
- Stage activations are written as `ACTIVATION.json` schema 2, without a Floe
  manifest revision; schema-1 records still read and verify as published.
  `olf project status --json` no longer reports `floe_manifest_revision`.
- Floe run reports of an activated stage are written under its own
  `activations/<stage>/` prefix, and Silver assets carry their row, accepted
  and rejected counts again.
- A dbt `profiles.yml` is no longer part of a project revision, and the
  reference products no longer track a rendered copy. `olf dbt parse` and
  `olf check dbt` render the profile into a temporary directory, so running
  them no longer changes the next `olf project build` (#546).
- dbt reads its connection from the run pod's environment. One profile,
  `libs/dbt/profiles/trino.yml`, replaces the `local`, `aws` and `azure`
  templates; every stage value in it is an `env_var()` reference with no
  default, so an unset variable fails the run instead of reaching the DEV
  catalog. The image bakes each product's manifest once with placeholder
  names and no longer depends on the provider. Each run re-parses with its
  stage's values and uploads its own manifest, run results and log rather
  than the baked manifest (#546).
- The reference products' `dbt_project.yml` and `sources.yml`, and those
  `olf product new` writes, read the stage catalog with no `lakehouse_dev`
  fallback. A scaffolded product's Silver source now carries the stage
  schema prefix as the reference products do, so it resolves on an AWS
  stage (#546).
- Activation puts only the contract values the project-code runtime reads on
  the code location and its run pods, rather than every `OPENLAKEFORGE_*`
  variable in the operator's environment, and takes `OPENLAKEFORGE_STAGE`
  from the activation itself. Bronze loads read
  `OPENLAKEFORGE_STORAGE_BRONZE_BUCKET`, with no `lakehouse-bronze` fallback
  (#546).

### Removed

- `olf floe render-profile|generate-manifests|revision`, `olf artifacts
  upload-manifests`, `libs/floe/profiles/`, and the generated manifests under
  `silver/<domain>/contracts/floe/manifests/`. The runtime patch of
  `floe_dagster.assets._fsspec_read_text` for S3-compatible stores is gone.
- The provider-contract fields `floe_manifest_*`, `floe_report_base_uri` and
  `domain_floe_manifest_uris`, their Terraform outputs and Dagster env, and
  the bucket-root `floe/manifests|revisions|reports` storage grants.
- The `PROJECT_CODE_DBT_PROFILE_ENV` image setting and the
  `OPENLAKEFORGE_DBT_TARGET`, `OPENLAKEFORGE_DBT_PROFILE_ENV` and
  `DBT_PROFILES_DIR` variables on Dagster pods. The dbt profile no longer
  depends on the provider (#546), so nothing reads them.

### Migration

- Move each domain's `<domain>.yml` into `_domain.yml` plus one file per
  entity, and declare its Bronze sources in `silver/sources.yml`; the seed
  `sales` and `supply_chain` domains show the shape. Delete
  `contracts/floe/manifests/`.
- The upgrade is one-way: a deploy never rolls a stage back to an activation
  made before Floe 0.8. If its pointer fails to commit over one, the new
  release stays installed and the deploy asks to be rerun.
- Dagster asset keys of Floe's quarantined rows move from
  `<domain>/<table>_rejected` to `<domain>/rejected/<table>`; their history
  stays under the old keys.
- Delete any `lakehouse_code/gold/<product>/dbt/profiles.yml` from version
  control; OpenLakeForge renders it for each stage.

## [0.5.0-alpha.1] - 2026-10-23

The identity and authorization release (Milestone 5). People sign in with
GitHub through Dex, their GitHub team decides their role, and a perimeter at
the ingress decides which services each role reaches. Authorization is at the
service level: nothing below Superset and OpenMetadata applies a per-user data
rule. The first run is simpler: plain HTTP and no sign-in by default. The AWS
and Azure deployments still publish no routes and are reached with
`olf forward`.

### Added

- GitHub sign-in through Dex. `spec.identity` takes `issuer: github` with the
  organisation, a map of canonical roles (`platform-admin`, `data-engineer`,
  `analyst`, `viewer`) to team slugs, and `oauth_app_secret_ref`; or
  `issuer: external` for your own OIDC issuer with a `role_claim` and
  `role_mapping`. The `identity.oidc` provider contract carries the issuer to
  every consumer (#24, #384, #388, ADR 0014).
- `olf github connect` creates the GitHub App through the browser (manifest
  flow) and stores its credentials in the Secret `oauth_app_secret_ref` names.
  `olf deploy` runs it inline when that Secret is missing, opens the browser on
  WSL, and ends with the sign-in steps. Dex explains a refused sign-in
  (#420, #428, #433, #438, #448).
- The oauth2-proxy perimeter: every service route is authorized at the
  ingress by role (`release/identity-roles.yaml`). Only `platform-admin` and
  `data-engineer` reach Dagster. The portal at the base domain lists the
  services your role reaches, has an Admin link and a Sign out button, and is
  served from the `openlakeforge-portal` image. Superset's Logout signs out of
  the platform (#176, #363, #431, #512, #528).
- Authentication off: a local profile without `spec.identity` publishes
  nothing beyond `*.localhost`, and Superset admits everyone as Admin.
- Plain HTTP by default. `olf init` and the repo profile declare
  `spec.access: {issuer: none}`; HTTPS with the local CA is `spec.access: {}`
  plus `olf access trust`. `issuer: none` is local-only (#399, #403, #417).
- Superset signs users in through the issuer and maps roles. OpenMetadata
  signs users in, a CronJob syncs its admins (`platform-admin`) and data
  stewards (`data-engineer`) from the GitHub teams every five minutes, and the
  local `admin` account is demoted once SSO is on (#25, #409, #427, #469, #518).
- Trino: the Web UI signs in through the issuer on HTTPS, or uses a fixed user
  on plain HTTP (Trino allows OAuth2 only over HTTPS). Trino serves HTTPS and
  workload clients (dbt, Superset, `olf e2e`) authenticate with a per-stage
  password; it is the default when `spec.access` is set. OLF-owned Jobs run
  the metadata ingestion (#26, #377, #425, #459, #471).
- `olf query` runs SQL against a stage's Trino catalog (#447).
- NetworkPolicy and RBAC: default-deny policies for `olf-system` and every
  stage namespace, driven by `release/network-flows.yaml`, with least-privilege
  service accounts. `olf deploy` first proves the CNI enforces policy, and the
  e2e holds flow and RBAC conformance tests (#46, #386, #392, #404, #408,
  #463, #466, ADR 0017).
- `olf break-glass` opens a time-boxed port-forward or shell (30 minutes by
  default, `--reason` required) and records each use; `olf status` lists
  recent uses. `olf secrets rotate` rotates a Dex client or the GitHub App
  (#178, #510, #519).
- Recovery drills in the local e2e: issuer outage and recovery, secret
  rotation, perimeter latency, and Trino stage-isolation probes
  (#176, #177, #516, #520).
- `olf e2e run --checks core|all` selects the check tier. Manual runs of the
  full local workflow default to DEV only; the nightly keeps DEV and PROD
  (#525, #526).
- `olf check docs` validates the docs manifest and every relative link
  (#439). The identity guides, roles reference and tutorial lesson 08 are new;
  the documentation is restructured for first-time users and the tutorial
  covers lessons 1 to 10 (#442, #444, #508).
- Credential scans of state, artifacts, diagnostics and logs, and a check
  that provider contracts carry no secret values (#181, #348, #365).

### Changed

- **Local hostnames are flat.** Stage routes are
  `<service>-<stage>.<base_domain>` (for example `dagster-dev.olf.localhost`),
  so one wildcard certificate covers them (#389, #395).
- **Local stage buckets are named `lakehouse-<stage>-<layer>`** in every
  stage; DEV was `lakehouse-<layer>` (#396, #401). Azure uses the same scheme.
- **A profile without `spec.access` has no ingress**: no Traefik, local CA or
  portal, and services are reached with `olf forward`. Likewise, no
  `spec.identity` deploys no issuer and no perimeter. Block presence is the
  switch (#406).
- **The default AWS cluster name is `eks-openlakeforge`**, was
  `limited-eks-openlakeforge-poc`. With `AWS_CLUSTER_NAME` unset, `olf` keeps
  the name an existing foundation's Terraform state records, so no migration
  is needed (#514).
- Trino keeps writing after a Polaris restart; `olf report export` writes a
  stage-neutral Superset bundle; the local Superset image is tagged by content
  so a change rolls the pods; `olf deploy` preloads the catalog's images into
  kind and retries after a failed release (#412, #416, #430, #456).
- Dependencies: certifi 2026.10.19 (#509). New pins: Dex v2.46.0, oauth2-proxy
  v7.15.5 and the portal image 0.3.0.

### Removed

- Keycloak, `spec.identity.smtp` and the `OLF_SMTP_*` variables: Dex and
  external issuers are the only identity options. Keycloak and `olf users`
  (invite, grant, revoke) were never in a tagged release; roles now come from
  GitHub teams (#331, #383, #419).

### Migration notes

- A profile with no `spec.access` used to get the HTTPS ingress. It now has
  none. To keep the 0.4 behaviour, add `access: {}` and run `olf access trust`;
  for plain HTTP, add `access: {issuer: none}`.
- Update bookmarks and scripts: `dagster.dev.olf.localhost` is now
  `dagster-dev.olf.localhost`. Trino and OpenMetadata keep
  `trino.<base_domain>` and `openmetadata.<base_domain>`.
- Recreate the local cluster: `olf destroy --provider local`, then
  `olf deploy`. The DEV buckets are renamed and Polaris keeps the storage
  locations its warehouse was created with, so an in-place apply leaves the
  catalogs on the old buckets.
- Without `spec.identity` on `*.localhost`, everyone who reaches Superset is
  Admin and OpenMetadata keeps its default login. To require sign-in, add
  `spec.identity` (with `spec.access`), create the GitHub App with
  `olf github connect`, and map teams to roles. `external` needs your own
  client Secrets; see ADR 0014.
- A profile that sets `base_domain` outside `.localhost`, or targets AWS or
  Azure with `spec.access`, now requires `spec.identity`. AWS and Azure
  profiles without `spec.access` need none.
- With `spec.access` set, Trino workload clients need passwords: callers
  outside `olf` that query Trino directly must use the HTTPS listener and the
  stage's credential Secret (ADR 0016). Without `spec.access` it stays off.
- `olf superset deploy-reports` and `olf superset export-reports` are still
  deprecated aliases of `olf report import` and `olf report export`.

### Known limitations

- **Dagster has no per-user authorization.** Dagster OSS has no native
  authentication or authorization. The perimeter decides who reaches it: only
  `platform-admin` and `data-engineer` hold the Dagster grant. Anyone admitted
  has the full Dagster capability, including launching any job and reaching
  the credentials its run pods hold, so assign `data-engineer` only to people
  you would trust with that. Callers inside the cluster (`olf e2e`, and any
  pod) reach Dagster without a session through its cluster-internal Service;
  that path is bounded by the stage namespace's default-deny NetworkPolicy and
  Kubernetes RBAC, not by authentication. See
  [identity-callers.md](docs/architecture/identity/identity-callers.md#dagster-boundary).
- **No data-level authorization.** A user's role decides which services they
  reach, not which data they read. Superset connects to Trino as one shared
  identity per stage, so Trino and the catalog never see the end user. Superset
  limits `analyst` and `viewer` to the Gold schemas, but nothing below it
  enforces that. Do not load data of differing sensitivity into a deployment
  whose users should not all see it. Stage separation (DEV cannot read PROD)
  holds against a forged caller name only while Trino workload authentication
  is on, which is the default with `spec.access`. Per-user data access is
  assessed in
  [ADR 0019](docs/adr/0019-data-plane-authorization-deferral.md) and is not
  part of this release.
- Workstation-local only for sign-in and NetworkPolicy. AWS and Azure keep
  port-forward access, publish no routes and have no perimeter or policies;
  the AWS secure reference is v0.6-beta.
- Plain HTTP mode is for `*.localhost`. Trino's Web UI uses a fixed user
  there, and the perimeter alone gates the route.
- The Trino workload password reaches the Dagster webserver and daemon pods,
  not only run pods, and a missing principal Secret fails with a 401 rather
  than naming the Secret (#521).
- The issuer-outage drill's recovery timer starts after the second ingress
  query and snapshots only ingress names and rules, and the
  recovery-timings status in the sessions document predates the recorded drill
  (#521).
- On WSL2 with `networkingMode=Mirrored`, Docker's userland proxy can hang
  connections to ports published on `127.0.0.1`. Setting
  `"userland-proxy": false` in Docker's `daemon.json` fixes it.

## [0.4.0-alpha.1] - 2026-10-07

The secure service access release (Milestone 4), on the local workstation.
Enabled user-facing services are reached at stable HTTPS URLs through one
ingress with a local certificate authority, without port-forwarding. The
ingress listens on `127.0.0.1` only; `spec.access.base_domain` changes the
host names, but serving a private network or another machine needs a
non-loopback adapter, which does not exist yet. Authentication arrives in
v0.5-alpha: v0.4 is private evaluation infrastructure, not a shared
production release. The AWS secure reference moved to v0.6-beta.

### Added

- The `access.ingress` provider contract: `shared.access` carries the base
  domain, the certificate issuer, the TLS mode and one route per enabled
  user-facing service. Internal endpoints and disabled capabilities cannot be
  routed, and each route is bound to its own stage label (#264, #308,
  ADR 0013). The Deployment Profile gains `spec.access.base_domain` (default
  `olf.localhost`) and `spec.access.issuer` (default `local-ca`).
- The local ingress adapter: Traefik and cert-manager in `olf-system`, a
  stable `local-ca` ClusterIssuer that survives re-deploys, and kind host
  ports 80/443 on `127.0.0.1`. `olf deploy` waits for a probe certificate, so
  a broken CA fails the deploy (#265, #310).
- Stage routes: `https://dagster.<stage>.<base_domain>` and
  `https://superset.<stage>.<base_domain>` per stage,
  `https://trino.<base_domain>`, and `https://openmetadata.<base_domain>`
  when governance is enabled, with one wildcard certificate per stage
  (#266, #314).
- `olf access trust` exports the local CA and prints the trust steps for
  Linux, macOS and the Windows store under WSL2. `olf status` lists the
  resolved URLs and any certificate that is not Ready. olf's own clients
  reach services through the ingress with the local CA and pinned loopback
  resolution: no `/etc/hosts` edits, no disabled verification (#267, #318,
  #268, #322).
- Ingress preflight: a local deploy stops before Terraform runs if the
  issuer is unsupported or `127.0.0.1:80/443` is taken. The full local e2e
  adds a Traefik restart drill and a certificate renewal drill, and the
  nightly uploads their evidence (#268, #322).
- `olf report export` / `olf report import`, next to `olf report validate`:
  dashboards are exported by uuid or slug, so renaming one no longer breaks
  re-export, and import/export go through the Superset REST API instead of
  `kubectl exec` (#281, #282, #303, #315).

### Changed

- Terraform toolchain 1.8.5 → 1.16.4; roots require `>= 1.10.0` (#269, #296).
- mypy now checks every module of `tools/olf`; the exemption baseline is gone
  (#189, #286–#291, #301–#311, #321).
- `provider_contracts`, `commands/checks`, `auth` and `contracts_check` are
  split into packages along their existing boundaries, with no behaviour
  change (#191, #293, #294, #302, #305).
- The roadmap document is removed: GitHub milestones are the plan, and the
  lifecycle stages moved to `docs/release/releasing.md` (#292).
- Dependencies: pyjwt 2.15.0 (#261) and urllib3 2.8.0 (#260), which close the
  16 open Dependabot security alerts on `tools/olf/uv.lock` (1 critical,
  7 high).

### Fixed

- `olf project deploy` can pull revision images from private registries:
  digest-pinned images are now resolvable inside kind nodes (#280, #300).
- OpenMetadata's first catalog crawl waits for Trino on a fresh apply
  (#277, #297).
- `olf e2e run` works from the exported contract file without platform
  Terraform state, and reads Dagster code locations for the stage under test
  (#278, #295).
- `olf platform contract` and the project commands exit with a clean message
  instead of a traceback on contract and toolchain errors (#279, #299).
- The doctor commands no longer provision a managed toolchain behind the
  host-mode setting, which made `test_cloud_provider` flaky (#233, #298).
- The nightly uploads the rendered provider contract even when it differs
  from the conformance fixture, and the fixture now matches the ingress
  contract (#341).

### Migration notes

- Recreate the local cluster: the new port mappings apply only to a new kind
  cluster. Run `olf destroy --provider local`, then `olf deploy`. Host ports
  80 and 443 on `127.0.0.1` must be free.
- After the first apply with Terraform 1.16.4, local state can no longer be
  read by Terraform 1.8.5.
- `olf superset deploy-reports` and `olf superset export-reports` are
  deprecated aliases of `olf report import` and `olf report export`.
  `SUPERSET_DASHBOARD_TITLE` now fails with a pointer to
  `SUPERSET_DASHBOARD=<uuid or slug>`, which a draft bundle's first export
  requires. `SUPERSET_REPORTS_MOUNT_PATH` is gone, and report import/export
  read `SUPERSET_ADMIN_PASSWORD` (default `admin`, matching the Terraform
  default).
- Removed: the Terraform outputs `dagster_webserver_service_names`,
  `dagster_code_location_name` and `dagster_code_location_names` (#313,
  #316), the Superset `superset-reports` volume, and `reports_mount_path`
  from the analytics contract (#319, #320).
- Port-forwarding is a development fallback: set `OLF_PORT_FORWARD=1`.
- After `olf access trust`, trust the exported CA once in your browser's
  store. Under WSL2 that is the Windows store.

### Known limitations

- No authentication or authorization: anyone who can reach the ingress can
  use Dagster, Superset and Trino. Keep v0.4 on loopback or a private
  network. Login arrives in v0.5-alpha (#176).
- Workstation-local only. AWS and Azure keep port-forward access; the
  AWS secure reference is v0.6-beta (#274). Only the `local-ca` issuer is
  supported, so there are no public or ACME certificates yet.
- Plain HTTP on port 80 returns 404 rather than redirecting to HTTPS.
- On WSL2 with `networkingMode=Mirrored`, Docker's userland proxy can hang
  connections to ports published on `127.0.0.1`. Setting
  `"userland-proxy": false` in Docker's `daemon.json` fixes it.

## [0.3.0-alpha.1] - 2026-10-01

The deployment profiles, stages and promotion release (Milestone 3): one
Deployment Profile describes shared DEV, optional UAT and PROD stages on one
cluster, each stage gets its own storage, catalog, query access and Dagster
(and Superset when it enables analytics), and one immutable project revision
is built once and promoted
unchanged from DEV to PROD without copying data or runtime state.

### Added

- Deployment Profile v1: the project-root `openlakeforge.yaml` declares the
  provider, preset, and the enabled stages with their analytics/governance
  capabilities; `olf profile validate` and `olf profile resolve --json`
  expose the resolved topology, and unknown fields or versions fail closed
  (#113, ADR 0011).
- Provider contract v3 and stage data-plane isolation: every stage has its
  own Bronze/Silver/Gold buckets, `lakehouse_<stage>` catalog, Trino catalog
  and runtime identity, and DEV-generated configuration cannot reach PROD
  objects (or the reverse); multi-stage isolation is verified at runtime on
  local (#153, #114, ADR 0003).
- The profile-driven lifecycle: `olf platform plan|apply -f` deploys the
  static platform for every enabled stage; `olf project image -f` builds and
  pushes the project-code image, `olf project build --project --image`
  publishes one revision, `olf project deploy -f --stage --revision`
  activates that exact revision in a stage, idempotently, and `olf project
  status -f [--stage]` reports it (#115).
- Isolated Dagster per stage, with recurring product schedules only in PROD
  and created stopped (#134); deterministic DEV Bronze seeded from project
  CSV fixtures (#116); stage-aware Superset report export and import, with
  report bundles carried in the immutable revision (#130).
- One shared OpenMetadata with stage-qualified roots
  (`<service>.lakehouse_<stage>`, `dagster_<stage>`, `superset_<stage>`) and
  stage-scoped reconciliation (#131, #211, #237). On the local provider every
  governed stage emits its own OpenLineage under its own namespace, and its
  lineage resolves only to its own tables (#252, #257).
- `olf init` renders the project's Deployment Profile from `--stages`
  (DEV by default), `--preset` and `--name`, validated before it is written,
  and `olf init --profile-only` adds one to an existing project (#259).
- `olf platform contract -f` exports the applied provider contract, and
  `OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE` points `olf project build`,
  `deploy` and `status` at that copy instead of the platform's Terraform
  state (#253).
- Reference CI/CD: the `build-revision` and `deploy-stage` composite actions
  and a documented promotion workflow through DEV, optional UAT and PROD
  (`docs/reference/ci-cd.md`) (#254, #119).
- A multi-stage nightly conformance gate: from an `olf init` project on the
  freshly built wheel, it applies a full DEV+PROD profile, promotes one
  revision, and asserts cross-stage isolation, disjoint Dagster run state,
  no data copied by promotion, stage-qualified OpenMetadata roots, per-stage
  lineage, and that removing a stage needs `--allow-stage-removal` (#155,
  #208, #232, #242, #244, #245, #247, #255).
- The local platform Terraform root is stage-aware: it takes the resolved
  `DeploymentTopology` as typed inputs and provisions one shared `olf-system`
  namespace plus one `olf-<stage>` namespace, runtime service account, and
  isolated metadata database per enabled stage. Dagster and Superset are
  `for_each` instances over the enabled stages instead of single module
  blocks; PostgreSQL, SeaweedFS, Polaris, Trino, and OpenMetadata keep exactly
  one Terraform owner (#133, ADR 0002).
- `olf deploy`/`plan`/`status`/`forward`/`e2e run` take `--stage` (default
  `dev`), and `olf deploy`/`plan` take `--allow-stage-removal`: an apply that
  would drop an already-deployed stage — deleting its namespace, services, and
  credentials — now fails closed without it. The guard reads both the applied
  `stage_names` output and the namespaces still labelled as this deployment's,
  so it holds when Terraform state is missing and the drift reset would
  otherwise tear the stage down by label. A removed stage's databases stay
  on the shared PostgreSQL server, so re-enabling it reuses its existing run
  history (#133).

- `olf project build --project P --image REF` computes and publishes an
  immutable, content-addressed `ProjectRevision` covering descriptors, Floe
  contracts, dbt, Dagster orchestration code, report assets when present,
  the project-code image digest, and the distribution version; `olf project
  revision inspect|verify` read a published revision without rebuilding
  source (#154, ADR 0012).

### Changed

- OpenMetadata moves from 1.12.10 to 1.13.6. 1.13 removed its Iceberg
  connector, so the lakehouse database service (still named `polaris`, or
  `aws_glue` on AWS) is now a Trino service, crawled as a read-only
  `openmetadata` Trino user across every governed stage's catalog; the
  Polaris OAuth token workaround is gone (#252).
- `olf e2e run` takes `-f/--file`, the Deployment Profile path every other
  profile-driven command already accepts. It names the profile the deployment
  was applied from, so validation resolves the topology the v3 contract
  recorded instead of re-resolving the project root. The nightly local e2e,
  and the documented local workflow now derive
  their topology from one profile file on both the deploy and the validate
  side; the documented `olf deploy --profile slim|full` steps drop the
  deprecated shorthand, whose `legacy` topology no profile file can name. The
  three `deployment.*` contract-mismatch errors name both values.

- The local stack no longer runs in one `lakehouse` namespace. Shared services
  move to `olf-system` and stage services to `olf-<stage>`, and every service
  endpoint in the provider contract is namespace-qualified. Upgrading a v0.2
  local deployment means `olf destroy --provider local` followed by a fresh
  deploy -- destroying first is what releases the cluster-scoped objects a
  chart owns (SeaweedFS' ClusterRole among them), which a new release in
  another namespace cannot adopt. Namespaces are derived from the profile
  on every provider (`--namespace` is rejected), and the `aws-poc` and
  `azure-poc` roots use the same `olf-system` and `olf-<stage>` namespaces
  (#133, #114).
- The `azure-poc` root's PostgreSQL databases moved to the same typed
  `databases` list the local root uses. Their names, users, and Secret names
  are unchanged, but the Terraform addresses are keyed now, so an
  already-applied POC stack needs the same destroy-then-redeploy as local
  rather than an in-place upgrade: a `moved` block cannot name an address
  whose key comes from a variable (#133).
- Turning a capability off is now reconciled, not just skipped. A stage that
  drops `governance` has its replicated `openmetadata-ingestion-bot` Secret
  deleted rather than merely no longer refreshed -- the copy already there
  stays a valid credential otherwise -- and turning off the last `analytics`
  stage removes the Superset dashboard service and its ingestion pipeline from
  OpenMetadata instead of leaving governance pointed at a service the same
  apply destroyed (#133).
- `olf destroy --provider local --phase foundation` discovers namespaces by
  the `openlakeforge.io/profile` label as well as from the topology, so a
  stage disabled in the profile but still deployed blocks the cluster
  deletion. `--force` remains the way past it (#133).
- A Deployment Profile's `metadata.name` must now be a valid Kubernetes label
  value: at most 63 characters and ending in an alphanumeric character. It
  becomes the `openlakeforge.io/profile` label on every namespace the
  deployment owns, so a name like `acme-` used to validate and then fail the
  apply that creates them (#133).
- `--profile slim|full` is now an explicit, deprecated single-DEV shorthand.
  With no `--profile`, the project-root `openlakeforge.yaml` Deployment
  Profile decides which stages and capabilities are deployed; a project with
  no profile file falls back to the same single-DEV shorthand, and an invalid
  profile fails closed (#133, ADR 0011).
- `olf.contracts.load_provider_contracts` returns a provider-contract v3
  payload instead of refusing it; `build_contract_env` resolves it against a
  `DeploymentTopology` and an explicit stage (#133, ADR 0003).
- `olf revision compute|publish|activate|verify` (the v0.2 Floe
  runtime-artifact revision) moved to `olf floe revision ...`, freeing the
  top-level `revision` name for the new project revision.
- `libs/product_dagster.py` prefers the stage-activated runtime
  `OPENLAKEFORGE_FLOE_MANIFEST_REVISION` over the value baked into the
  project-code image at build time, so one image digest no longer requires
  a rebuild per Floe revision (#154).

### Migration notes

- There is no in-place upgrade from 0.2. Destroy the 0.2 deployment
  (`olf destroy --provider local`, or the POC equivalent), then give the
  existing project its Deployment Profile with `olf init --profile-only`.
  Keep the preset you deployed with (`--preset full` if you ran 0.2 with
  `--profile full`, or Superset and OpenMetadata are left out), and choose
  the stages:

  ```bash
  olf init --profile-only --stages dev,prod --preset full   # or --stages dev
  ```

  The profile targets the local provider. On the AWS POC, set
  `provider.type: aws` and `provider.region`; on the Azure POC, set
  `provider.type: azure` and keep your foundation tfvars available
  (`--var-file` on `olf platform apply`, or `AZURE_TFVARS_FILE`), which is
  where Azure takes its region from. Then deploy with the profile-driven
  lifecycle from the project directory:

  ```bash
  olf platform apply -f openlakeforge.yaml
  image="$(olf project image -f openlakeforge.yaml | tail -n 1)"   # digest-pinned reference
  revision="$(olf project build --project . --image "$image" | tail -n 1)"
  olf project deploy -f openlakeforge.yaml --stage dev --revision "$revision"
  olf project deploy -f openlakeforge.yaml --stage prod --revision "$revision"   # promotion, if prod is enabled
  ```

  Locally, set `PROJECT_CODE_IMAGE_REPOSITORY` to a registry you can push to
  first; the AWS and Azure POCs push to their foundation's ECR/ACR
  repository by default. The namespaces, Terraform addresses and
  OpenMetadata service type all changed, and OpenMetadata metadata from 0.2
  is not carried over.
- A project needs an `openlakeforge.yaml` Deployment Profile: `olf init`
  writes one for a new project and `olf init --profile-only` for an existing
  one. `--profile slim|full` still works as a deprecated single-DEV
  shorthand.
- The `Makefile` is gone (#212, ADR 0008). Every former target has an `olf`
  command: `olf deploy`/`destroy`, `olf platform apply`, `olf e2e run`,
  `olf check all`, `olf forward`.
- `olf revision ...` is now `olf floe revision ...`, and `olf e2e run` takes
  `-f` with the profile the platform was applied from.
- Promotion needs a container registry: the project-code image is
  identified by a pullable digest (see `docs/setup/local.md`, "Promote a
  project revision between stages").

### Known limitations

- On AWS and Azure, CI jobs still need the foundation's Terraform state;
  the contract file covers only the platform contract until remote state
  (#132). `olf e2e run` still reads Dagster names from the platform state,
  and `olf project deploy` cannot yet pull a revision image from a private
  registry (#256).
- OpenMetadata 1.13.6 fails to auto-create OpenLineage pipeline entities
  (an upstream bug); lineage edges are still recorded.
- Optional UAT is supported by the profile and resolves to its own
  identities, but the nightly exercises DEV and PROD only (#251).
- The Azure root is verified by `terraform validate`/plan only. The AWS root
  has a live deploy and full e2e run behind it, but multi-stage isolation on
  live AKS and EKS is still to be proven.
- Multi-stage governance is local-only: the AWS and Azure roots accept at
  most one governed stage. AWS OpenMetadata entity resolution is still to be
  proven, as part of the AWS reference-profile beta gate.
- Personal workspaces and local DuckDB execution are post-beta work.

## [0.2.0-alpha.1] - 2026-08-26

The small-team adoption release (Milestone 2): OpenLakeForge installs from
PyPI without a checkout, scaffolds a data product without touching shared
platform code, and runs a slim profile with no product allowlist baked into
the tooling.

### Added

- PyPI distribution: `pip install openlakeforge` installs the `olf` console
  command with a verified, immutable Terraform/Helm/runtime payload embedded
  in the wheel and sdist (#128, ADR 0009).
- `olf init` bootstraps a writable `lakehouse_code/` project from the
  packaged demo in the current directory; `olf init --empty` creates a
  transitional project with no source, domain, or product yet (#146,
  ADR 0009).
- A managed Terraform/Helm/kubectl/kind toolchain: `olf` downloads, verifies,
  and privately invokes its own versioned copies under `OLF_HOME`, so none of
  those tools need to be installed on the host. `OLF_TOOLCHAIN_MODE=host`
  opts back into host-installed copies (#127, ADR 0008).
- SDK-managed AWS and Azure authentication: `olf auth login --provider
  aws|azure` goes through boto3 / the Azure SDK directly; the `aws` and `az`
  CLIs are no longer required (#142, ADR 0008).
- Golden-path scaffolding: `olf source new`, `olf domain new`, and
  `olf product new` generate a runnable Bronze source, Silver domain, or Gold
  product from documented inputs, with no shared-code edit (#40).
- A typed domain inventory built from validated descriptors; every seed-
  product allowlist is gone from shared platform code, so an added product
  is discovered automatically (#39).
- Persistent Polaris catalog state: a Polaris pod restart no longer loses
  table identity or requires a full platform re-apply (#79).
- A slim local profile that omits OpenMetadata and Superset, with e2e
  assertions skipped rather than failed when a layer is absent (#78).
- A kind smoke gate (`olf smoke run`) on every pull request: one product
  pipeline through to a queryable Gold table, within a 45-minute budget
  (#81).

### Changed

- `olf` is now the only repository orchestration implementation; the
  shell-scripted deploy path is gone. `Makefile` targets are deprecated
  one-line delegates to the equivalent `olf` command (#122-#126, ADR 0008).
- Dagster collapses to one merged code location by default; a per-domain
  split is now an explicit configuration choice rather than the default
  (#76, ADR 0006).
- `lakehouse_code/` replaces `domains/` as the user-code root: Bronze is
  source-owned, Silver is domain-owned, and Gold stays product-owned (#109,
  ADR 0004). The `openlakeforge.io/v1alpha3` `Lakehouse`/`Source` descriptor
  pair replaces `v1alpha1`/`v1alpha2` `Domain` descriptors; their loader and
  validator are removed along with `docs/schema/domain*.json` and the
  `v1alpha1`->`v1alpha2` migration guide.
- `scripts/release/verify-install.sh` is replaced by `olf release
  verify-install`.
- The decision log is consolidated from 32 ADRs to 10, renumbered `0001`-`0010`
  and rewritten to describe what binds today rather than stacking supersessions.
  Each new ADR carries a History footer naming the records it absorbs. ADR
  numbers referenced in earlier releases, commits, and closed pull requests
  refer to the old numbering.

### Migration notes

Consumers upgrading from `v0.1.0-alpha.1` should:

1. Stop cloning the repository to install: `pip install openlakeforge` (or
   `uv tool install openlakeforge --python 3.12`), then `olf init` in an
   empty project directory. See the [README](README.md#quick-start) and
   [local installation guide](docs/setup/local.md).
2. Migrate any `domains/<domain>/domain.yaml` descriptor to
   `lakehouse_code/lakehouse.yaml` plus one `lakehouse_code/bronze/<source>/
   source.yaml` per Bronze source (`openlakeforge.io/v1alpha3`). See
   [docs/reference/domain-descriptor.md](docs/reference/domain-descriptor.md).
3. Replace direct `scripts/*.sh` or checkout `make <env>-*` invocations with
   the equivalent `olf` command; every `Makefile` target still works as a
   delegate, but is no longer the primary interface.
4. Expect further breaking changes in the next alpha; this stage carries no
   forward compatibility guarantee.

### Known limitations

- No stable support window is published before `v1.0`; only the latest
  alpha tag is maintained.
- The Azure POC and AWS POC deployment targets remain proof-of-concept
  scope; see `docs/architecture/aws-eks-poc.md` for the current AWS
  compatibility gate.

## [0.1.0-alpha.1] - 2026-08-10

The first publishable OpenLakeForge alpha: a signed, SBOM'd, provenance-
attested release bundle built from the seed multi-product POC (Sales
`order_revenue` and `customer_health`, Supply Chain
`inventory_reliability`) across the local (kind), Azure POC (AKS), and AWS
POC (EKS) deployment targets.

### Added

- `.github/workflows/release.yml`: tag-triggered (`v*`) and
  `workflow_dispatch` (dry-run capable) release pipeline. Builds and pushes
  `project-code` and `superset` images to `ghcr.io/malon64/openlakeforge/*`
  by digest, signs them keylessly with cosign (Sigstore OIDC, no long-lived
  keys), generates an SPDX SBOM per image attached as both a cosign
  attestation and a release asset, attaches SLSA build provenance via
  `actions/attest-build-provenance`, and publishes the GitHub Release with
  the changelog, the component manifest, the compatibility matrix, and
  `checksums.txt`.
- `olf release` CLI group (`tools/olf/olf/release.py`): `manifest`,
  `checksums`, `compatibility-matrix`, and `check` (the release-readiness /
  clean-install consistency gate).
- `make release-check` and `make release-bundle` targets, and a
  `release-check` job in `.github/workflows/checks.yml` so release drift is
  caught on every pull request, not only at tag time.
- `docs/release/releasing.md` and `docs/release/compatibility-matrix.md`.
- `scripts/release/verify-install.sh`: the scripted clean-checkout install
  verification a consumer (or the maintainer, post-merge) runs against a
  published tag.

### Migration notes

This is the first tagged release; there is no prior version to migrate
from. Consumers adopting this alpha should:

1. Pin to the immutable tag `v0.1.0-alpha.1` (or the resolved image
   digests recorded in the release's `component-manifest.json`) rather than
   `:local` or `main`.
2. Follow [docs/release/releasing.md](docs/release/releasing.md) to verify
   signatures and checksums before deploying.
3. Expect breaking changes in the next alpha; this stage carries no forward
   compatibility guarantee (see "Lifecycle stages" in
   `docs/release/releasing.md`, formerly "Lifecycle Definitions" in the
   since-removed `docs/industrialization-roadmap.md`).

### Known limitations

- No stable support window is published before `v1.0`; only the latest
  alpha tag is maintained.
- The Azure POC and AWS POC deployment targets remain proof-of-concept
  scope; see `docs/architecture/aws-eks-poc.md` for the current AWS
  compatibility gate.
