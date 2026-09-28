# OpenLakeForge Industrialization Roadmap

Status: Active

Last updated: 2026-08-28

Roadmap board: [OpenLakeForge Industrialization Roadmap](https://github.com/users/malon64/projects/3)

This document defines the route from the current proof of concept to a
supportable OpenLakeForge distribution. GitHub milestones and issues are the
execution view and must be reconciled with this document after each release.

The `v0.1.0-alpha.1` closeout was intentionally narrow: it validates the local
kind profile and the three existing seed products. Its release gates were an
immutable Floe artifact-revision contract, a clean local full end-to-end run,
and a signed, independently verifiable release bundle. Generic product
discovery, a product scaffold, a fourth product, and scheduled local
conformance were deferred to `v0.2-alpha`, which is now scoped as the
small-team adoption release described in Milestone 2.

## Who This Is For

OpenLakeForge targets small data teams that want a complete, self-hosted,
open-source lakehouse they can run anywhere. Teams large enough to fund a
platform engineering function will generally buy a managed platform instead;
that is not the audience being optimised for here.

That audience constrains the product in ways a generic "enterprise
distribution" framing does not:

- Runtime footprint is a feature. A team without a platform engineer cannot
  absorb a stack that needs one.
- Onboarding a data product must be a scaffold, not a platform-code edit.
- Time from clean checkout to a queryable Gold table is the metric that decides
  whether an evaluator continues.
- Recoverability matters more than scale. Restarts happen on small clusters;
  losing catalog state is unrecoverable in practice for these teams.

The governance, identity, and multi-environment security work in the later
milestones stays in scope — it is what makes the distribution supportable — but
it follows adoption rather than preceding it.

## Product Boundary

The supported `v1.0` product is a self-hosted distribution for one organization
and one environment per deployment, shipped in two footprints from a single
codebase:

| Footprint | Contents | Intended use |
| --- | --- | --- |
| Slim | Ingestion, validation, table format, catalog, query, orchestration | Fastest path to a first Gold table; the small-team default |
| Full | Slim plus governance (OpenMetadata) and reporting (Superset) | Teams that need a catalog and dashboards |

Both footprints are introduced in Milestone 2 (#78). Before that, the local
profile deploys the full component set.

- Local kind is the `v0.1.0-alpha.1` validated developer profile.
- AWS is an experimental POC until the secure beta reference profile.
- Azure remains an experimental preview until after `v1.0`.
- Streaming, a SaaS control plane, hard multi-tenancy, GCP, multi-region high
  availability, and simultaneous AWS/Azure parity are outside the `v1.0`
  boundary.

OpenLakeForge is currently an early alpha. Its strong foundations should be
preserved: ADR-backed decisions, provider contracts, the
foundation/platform/artifact separation, and shared typed deployment tooling.
The cloud paths are not alpha release evidence until their own compatibility and
recovery gates pass.

## Rebalance History

### Second rebalance — adoption before hardening (2026-08-11)

`v0.1.0-alpha.1` shipped a signed, reproducible release with SBOMs, provenance,
an immutable component catalog, and a compatibility matrix. That work stands.
What it also made clear is that release trust was ahead of basic adoptability:

- A published release is not installable. Running OpenLakeForge still requires
  cloning the repository.
- Adding a data product requires editing shared platform code in several places
  across two languages.
- The local stack runs 16 pods and asks for 6 CPU / 12Gi, most of it governance
  and reporting an evaluating team does not need on day one.
- The local catalog does not survive a pod restart.
- No CI job deploys the platform, so a green pull request says nothing about
  whether the platform still deploys.

`v0.2-alpha` is therefore refocused from conformance follow-on work to
small-team adoption, and the security, operability, and governance milestones
shift one release later. Nothing is removed from them; the ordering changes.
Issues carry `priority: P0/P1/P2` labels expressing the intended sequence within
a milestone.

### First rebalance — industrialization sequence (2026-07-16)

The roadmap at that point was a useful component backlog, but it did not express
an industrialization sequence. The changes made then were:

- Put security, recovery, reproducibility, and release engineering before
  production exposure rather than in a final hardening phase.
- Define install, upgrade, rollback, compatibility, and support contracts.
- Remove hardcoded product knowledge so onboarding another product does not
  require shared platform changes.
- Introduce secrets management before adding more secret-bearing identity
  clients.
- Specify provider-neutral identity and observability contracts. Keycloak and
  the OSS observability stack remain reference implementations, not mandatory
  platform contracts.
- Treat backup/restore, capacity, cost, failure recovery, SLOs, and live cloud
  validation as release gates.
- Split umbrella issues into independently testable deliverables and reconcile
  roadmap state with merged work.
- Keep lineage desirable but prevent it from blocking platform safety and
  release readiness.

## Roadmap Restructuring

Recorded from the 2026-07-16 rebalance; retained as the rationale for the
current milestone shape.

- Keep [#17](https://github.com/OpenLakeForge/openlakeforge/issues/17) and
  [#19](https://github.com/OpenLakeForge/openlakeforge/issues/19) as early trust work.
- Audit [#18](https://github.com/OpenLakeForge/openlakeforge/issues/18) against
  `main`, close the completed local/Azure scope, and open a separate AWS full
  end-to-end issue for any remaining work.
- Replace the separate access and identity phases with a secure-foundation
  milestone: secrets, private networking, TLS, ingress, and authentication ship
  together.
- Reframe [#24](https://github.com/OpenLakeForge/openlakeforge/issues/24) through
  [#26](https://github.com/OpenLakeForge/openlakeforge/issues/26) around a
  provider-neutral `identity.oidc` contract. Keycloak is an optional local or
  self-hosted adapter; AWS can accept an existing enterprise issuer.
- Keep [#21](https://github.com/OpenLakeForge/openlakeforge/issues/21) through
  [#23](https://github.com/OpenLakeForge/openlakeforge/issues/23) as an operability
  milestone, with backend-neutral metrics and log-export contracts.
- Split [#14](https://github.com/OpenLakeForge/openlakeforge/issues/14) into product
  contract, metadata reconciliation, profiling, and governance outcomes.
- Treat [#28](https://github.com/OpenLakeForge/openlakeforge/issues/28) through
  [#30](https://github.com/OpenLakeForge/openlakeforge/issues/30) as incremental
  product features rather than prerequisites for platform safety.
- Break [#31](https://github.com/OpenLakeForge/openlakeforge/issues/31) into
  independently verifiable security, recovery, and supply-chain work. Move
  remote state, backups, image pinning, and per-workload IAM before beta.
- Defer [#8](https://github.com/OpenLakeForge/openlakeforge/issues/8) and full Azure
  managed-service parity until the AWS reference profile is stable.

## Milestone 0 — Rebaseline and Govern (partially delivered)

Goal: make repository state, roadmap state, and release intent agree.

Delivered:

- Publish lifecycle definitions for alpha, beta, release candidate, stable,
  deprecated, and unsupported versions.
- Add release, priority, risk, effort, owner, and blocking-dependency fields to
  the roadmap.
- Replace component phases with the release milestones in this document.
- Split umbrella issues and give each item measurable acceptance criteria.
- Reconcile issue and roadmap status after every merged pull request.

Outstanding — this milestone stays open until these land:

- Define contribution, security reporting, support, ownership, and review
  policies. No `CONTRIBUTING`, `SECURITY`, `SUPPORT`, `GOVERNANCE`, or
  `CODEOWNERS` file exists in the repository; `.github/` contains only
  `workflows/`. Tracked in #37.
- Enforce branch protection, required CI and review, and dependency update
  automation. `main` is currently unprotected and there is no `dependabot.yml`.
  Vulnerability alerts are enabled.

Exit gate: every roadmap item belongs to a named release, has independently
testable acceptance criteria, and accurately reflects the repository; and the
policy, review, and dependency-automation controls above are in place.

## Milestone 1 — Reproducible Product and Release Trust (`v0.1-alpha`, delivered)

Goal: create a reproducible, versioned alpha that proves the product contract.

Delivered as `v0.1.0-alpha.1`. Two items were deferred to Milestone 2 and are
marked below.

- Complete #19 by hashing all generated Floe artifacts, stamping the
  project-code image and uploaded manifest set, and rejecting mismatched
  revisions before rollout.
- Keep the #17 decision in `main`: `dbt-trino` is the Gold engine after the
  local atomic-replacement, canonical-identity, and full-pipeline proof. AWS
  compatibility validation belongs to the secure AWS reference-profile gate,
  not this local-only alpha release.
- Introduce an independently versioned `domain.yaml` schema with `apiVersion`
  and `kind`.
- Remove provider-specific Polaris names from domain descriptors and derive
  physical names from provider contracts.
- Keep the three current seed products as the alpha product boundary. Do not
  claim self-service product onboarding or generic discovery in this release.
  Descriptor-driven discovery (#39) and the golden-path scaffold (#40) are
  deferred to Milestone 2.
- Establish a version catalog covering OpenLakeForge, charts, Terraform
  providers, Python dependencies, runner images, and base images.
- Lock project-code dependencies; pin container bases and GitHub Actions by
  digest or commit; make release tags immutable; publish signed images with
  SBOMs and provenance.
- Publish tagged GitHub releases with checksums, changelog, migration notes,
  compatibility matrix, and the exact component manifest.

Exit gate: a tagged alpha installs from a clean checkout, reproduces the same
artifact digests, and passes the full local result for the three seed products.

## Milestone 2 — Small-Team Adoption (`v0.2-alpha`)

Goal: make the platform something a small team can install, understand, extend
with its own data, and trust not to break — before adding production surface
area to secure.

Ordering within the milestone follows the `priority` labels on each issue.

**P0 — blocking.**

- Build one typed domain inventory from validated descriptors and remove every
  seed-product allowlist from shared platform code (#39).
- Collapse Dagster code locations to one by default and make the per-domain
  split configuration. Per-domain locations currently cost a pod each without
  providing independent deployability, because all locations share one image
  tag (#76).
- Persist the Polaris catalog so a pod restart does not lose table identity and
  require a full platform re-apply (#79).

**P1 — core release payload.**

- Make OpenMetadata and Superset optional and ship a slim local profile at no
  more than 9 steady-state pods, with e2e assertions skipped rather than failed
  when a layer is absent (#78).
- Add a golden-path scaffold that generates a runnable data product from
  documented inputs, and prove a fourth product onboards with no shared-code
  change (#40).
- Add a kind smoke gate on every pull request: one product pipeline through to a
  queryable Gold table, within an enforced time budget (#81).

**P2 — completes the release.**

- Add the fresh local full end-to-end gate on a main or nightly cadence, with
  assertions discovered from the domain inventory (#60).
- Publish an installable release artifact so a consumer can deploy a tagged
  release into an existing cluster without cloning the repository (#80).
- Document what Floe contributes — contract-based validation, reject handling,
  and Silver materialization — relative to assembling dbt tests, Soda, or raw
  dlt. Floe remains the default ingestion and validation layer (#82).

Exit gate: a small team installs a published release into a cluster without
cloning the repository, scaffolds a data product, and reaches a queryable Gold
table; the slim profile fits the documented footprint; every pull request is
gated on a real deployment; and a Polaris restart is non-destructive.

## Milestone 3 — Deployment Profiles, Stages, and Promotion (`v0.3-alpha`)

Goal: introduce a versioned Deployment Profile, isolate shared DEV and PROD
stages on one Kubernetes cluster, and promote one immutable project revision
without rebuilding source or copying data/runtime state, before secure
identity, secret management, ingress/networking, and full observability are
attached to it (#111).

Dependency order within the milestone, not release priority:

- Close the remaining external-project boundary gaps without renaming the
  canonical `dashboards/`/`pipelines/` paths (#112).
- Parse and resolve the minimal Deployment Profile v1 — provider, stage, and
  preset as distinct typed concepts (#113, ADR 0011).
- Define provider-contract v3 and prove AWS Glue stage isolation (#153).
- Make the shared-cluster platform root stage-aware (#133): one shared
  `olf-system` namespace, one `olf-<stage>` namespace per enabled stage, and
  stage-scoped Dagster/Superset derived from the resolved topology.
- Provision stage data-plane isolation through provider adapters (#114).
- Build and verify a complete immutable project revision, independent of any
  target stage (#154, ADR 0012).
- Activate an existing project revision in a selected stage without a full
  Terraform apply or a source rebuild (#115).
- Seed deterministic raw DEV Bronze from project CSV fixtures (#116).
- Deploy isolated Dagster instances per shared stage (#134).
- Add stage-aware Superset authoring and report promotion (#130).
- Add stage-qualified OpenMetadata reconciliation (#131).
- Prove runtime isolation and exact-revision reuse end to end (#155).
- Publish the final reference promotion CI/CD workflow (#119).

Exit gate: a fresh local Slim profile creates isolated DEV and PROD stages;
optional UAT can be added without changing DEV/PROD identities; DEV
credentials and configuration cannot read or write PROD through normal
generated paths, and vice versa; the exact same project revision digest
deploys to DEV and PROD without a rebuild; promotion copies no lakehouse data
or mutable service state; and the v0.2 single-stage CLI has a documented
compatibility path.

## Milestone 4 — Secure AWS Reference Profile (`v0.5-beta`)

Goal: establish a secure, recoverable AWS deployment suitable for controlled
beta use.

- Add encrypted remote Terraform state with locking and separate foundation and
  platform states.
- Use private worker and database subnets, private RDS, encrypted and versioned
  S3 buckets, private-by-default ingress, and configurable DNS.
- Replace the shared AWS workload policy with least-privilege Pod Identity roles
  per service account.
- Deploy External Secrets Operator with AWS Secrets Manager, remove secret
  values from Terraform outputs where possible, and test rotation. Local
  development may retain Kubernetes Secrets.
- Enforce namespace RBAC, default-deny NetworkPolicies, Pod Security Standards,
  non-root workloads, resource limits, and restricted administrative access.
- Add Traefik and cert-manager behind `access.ingress`; keep port-forwarding only
  as a development fallback.
- Add a provider-neutral OIDC contract. Restrict Dagster to platform operators
  through `oauth2-proxy`, use native OIDC where supported, and separate Trino
  human OAuth from service authentication.
- Do not block AWS on Polaris external-identity-provider support because AWS uses
  Glue. Track that requirement for the self-hosted profile.
- Enable RDS backup and point-in-time recovery, S3 lifecycle/versioning,
  Terraform-state recovery, and documented metadata/search rebuild procedures.

Exit gate: a fresh AWS deployment passes full end-to-end validation without
default credentials, public worker/database endpoints, or port-forwarding,
then passes secret-rotation and backup/restore drills.

## Milestone 5 — Operability and Lifecycle (`v0.9-rc`)

Goal: prove the release can be operated, upgraded, recovered, and supported.

- Version `observability.metrics`, `observability.logs`, and alert-routing
  contracts.
- Provide Prometheus, Grafana, Loki, and Alloy as the OSS reference stack while
  allowing compatible external backends.
- Manage dashboards and alerts as code for platform health, Dagster failures
  and queues, Trino performance, product freshness, Floe rejections,
  certificate expiry, and storage growth.
- Set the reference objectives to single-region, 99.5% monthly control-plane
  availability, metadata RPO of 24 hours, and RTO of 4 hours.
- Add load and concurrency tests and publish the measured capacity and AWS cost
  envelope. Do not advertise unsupported scale.
- Automate clean install, upgrade from the previous minor release, rollback,
  teardown, component restart, worker loss, and backup/restore scenarios.
- Publish an operations handbook for diagnosis, credential rotation, scaling,
  upgrades, disaster recovery, and dependency escalation, including the Floe
  contingency.
- Add CodeQL, Gitleaks, container and IaC scanning, dependency audits, and a
  release-blocking severity policy.

Exit gate: the release candidate meets its reference objectives and survives
upgrade, rollback, recovery, and failure drills.

## Milestone 6 — Governed Stable Distribution (`v1.0`)

Goal: ship a documented, governed distribution with a stable product contract.

- Make OpenMetadata reconciliation authoritative: distinguish intended metadata
  from discovered assets, detect missing or stale entities, and prevent
  duplicate table identities.
- Add ownership, lifecycle, classification, SLA, and quality expectations to the
  domain contract and use them in OpenMetadata and dynamic end-to-end checks.
- Enable profiling and sample data only through explicit product policy; keep it
  off by default for unclassified or sensitive assets.
- Re-enable Bronze-to-Silver, Silver-to-Gold, and dashboard lineage
  independently, with canonical-identity and stale-edge tests. A failing lineage
  adapter stays disabled without blocking the stable platform release.
- Publish complete installation, onboarding, operations, security,
  compatibility, and migration documentation.

Exit gate: fresh local and AWS installations, AWS full end-to-end validation,
upgrade, rollback, restore, security, failure-recovery, and fourth-product
onboarding gates all pass.

After `v1.0`, harden Azure through the same conformance suite: ADLS Gen2,
managed PostgreSQL, workload identity, Key Vault/External Secrets Operator,
private networking, ingress/TLS, and restore validation.

## Supported Interfaces and Versioning

- `olf` remains the only supported interface for developing and operating from
  a checkout, and from Milestone 2 also carries the product-scaffolding and
  consumer install paths (#40, #80) that do not assume a repository clone.
- Add explicit preflight, conformance, backup, restore, and release-check
  targets as those capabilities are delivered.
- `provider_contracts` v3 separates shared platform bindings from explicit
  DEV/UAT/PROD stage bindings. The local platform root is stage-aware (#133)
  but still exports the v2 surface, which adapts to one DEV stage until #114
  gives every stage its own storage and catalog; unknown versions fail closed.
  Minor releases may add compatible fields; removal or rename requires a major
  release.
- Version the lakehouse descriptor schemas (`lakehouse.yaml` / `source.yaml`)
  independently and provide validation and migration tools for schema changes.
- Publish a compatibility matrix for OpenLakeForge, Kubernetes, Terraform,
  charts, cloud services, and supported upgrade paths.

## Verification Policy

| Cadence | Required verification |
| --- | --- |
| Pull request | Existing static/unit checks, contract-schema validation, image build, SBOM, vulnerability/IaC/secret scans, and — from `v0.2-alpha` — the 45-minute `olf smoke run` kind gate on the slim profile (#81) |
| Main/nightly | Fresh local full end-to-end run on the full profile from `v0.2-alpha` onward (#60); `v0.1.0-alpha.1` records one clean local release run instead |
| Scheduled AWS | Ephemeral deployment, all product pipelines, restore drill, and teardown after the secure beta profile is introduced |
| Release | `v0.1.0-alpha.1`: digest-mismatch negative test, clean local install, full three-product result, and release-bundle verification. `v0.2-alpha` adds consumer install from a published release (#80) and fourth-product onboarding (#40). Later releases add lifecycle and recovery gates cumulatively. |

Release gates are cumulative. A feature can be deferred, but an unmet security,
recovery, reproducibility, or lifecycle gate cannot be waived merely to meet a
target date.

## Lifecycle Definitions

| Stage | Intended use | Compatibility commitment |
| --- | --- | --- |
| Alpha | Development and product-contract validation; `v0.1.0-alpha.1` is validated on local kind only | Breaking changes allowed with migration notes |
| Beta | Controlled AWS evaluation | Best-effort forward migration within the beta line |
| Release candidate | Operational and upgrade qualification | No planned breaking changes before the associated stable release |
| Stable | Supported production use within the published reference envelope | Compatible changes in minor releases; breaking changes only in major releases |
| Deprecated | Still functional but scheduled for removal | Removal release and migration path published |
| Unsupported | Outside the maintained version window | No fixes or compatibility guarantees |

The exact stable support window should be declared before `v1.0`; until then,
only the latest pre-release is maintained.
