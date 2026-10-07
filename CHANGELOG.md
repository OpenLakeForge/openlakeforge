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

## [0.4.0-alpha.1] - 2026-10-05

The secure service access release (Milestone 4), local and on-premises
only. Enabled user-facing services are reached at stable HTTPS URLs through
one ingress with a local certificate authority, without port-forwarding. A
real installation sets its own base domain. Authentication arrives in
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
- Local and on-premises only. AWS and Azure keep port-forward access; the
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
