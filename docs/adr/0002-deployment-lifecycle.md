# ADR 0002: Deployment lifecycle — foundation, platform, artifacts

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS and Azure ✔ at proof-of-concept maturity ([ADR 0010](0010-cloud-provider-implementations.md)) |

## Context

Deployment answers two questions at once. What runs, and in what order:
creating a cluster, applying the platform services and deploying code are
three operations with different inputs and failure modes. And what is safe to
re-run on a code commit: Terraform-managed infrastructure and code-derived
artifacts have different lifetimes, and a change to a dbt model must not put
a Terraform apply in the CI path. Earlier records answered these separately
and left it unclear whether a deploy had two steps or three. It has three
phases in two lifecycle categories; those are different axes.

## Decision

**D1. A deployment runs three ordered phases: `foundation`, `platform`,
`artifacts`.** `foundation` creates the Kubernetes cluster and container
registry (kind locally, EKS and ECR on AWS, AKS and ACR on Azure) with
Terraform. `platform` creates the namespaces and the long-lived platform
services with Terraform and Helm. `artifacts` builds a `ProjectRevision` from
`lakehouse_code/` and activates it for one stage. `olf infra deploy` runs them
in that order, and `--phase` selects one; `olf infra platform apply -f
openlakeforge.yaml` runs the static prefix. `DeploymentPhase` also has
`prefetch`, a local image warm-up between foundation and platform and a no-op
on cloud providers; it is not a fourth phase. Phases are idempotent:
re-running one reconciles existing state. Enforced by: `DeploymentPhase` in
`tools/olf/olf/infra/deployment/engine.py` and
`test_deploying_everything_runs_the_adr_0002_order` in
`tools/olf/tests/infra/test_provider_conformance.py`.

**D2. After its apply, the platform phase bootstraps Polaris and
OpenMetadata from `olf`, using only the applied provider contract.** The
Polaris bootstrap creates each stage catalog, the workload principals with
their credential Secrets and grants, and people's roles (ADR 0019). The
OpenMetadata bootstrap runs before artifacts, because `deploy-metadata` logs
in with the credentials it creates, then copies the ingestion-bot Secret into
each governed stage namespace and deletes it from enabled stages that are not
governed. `olf` stamps a digest of Trino's Polaris credentials on Trino's pod
template, so a changed credential rolls Trino, and waits for that rollout on
every deploy. Neither bootstrap reads user code. Enforced by:
`tools/olf/olf/infra/polaris_bootstrap.py` and
`tools/olf/olf/infra/deployment/governance.py`.

**D3. The platform phase derives every namespace from the resolved
topology.** One shared `olf-system` holds the services a deployment runs
once (PostgreSQL, SeaweedFS, Polaris, Trino, OpenMetadata); one `olf-<stage>`
per enabled stage holds that stage's Dagster and, with analytics on, its
Superset. Stage-scoped services are `for_each` instances over the enabled
stages, never copied module blocks, and each owns its metadata database on
the shared PostgreSQL server. Enforced by: `stage_namespace` in
`tools/olf/olf/common/deployment/context.py` and the `stage_namespaces`
checks in `tools/olf/olf/common/contracts_check/_hcl.py`.

**D4. Removing a stage needs `--allow-stage-removal`, and keeps the stage's
databases.** A platform apply compares the resolved topology with the root's
applied `stage_names` output and refuses one that drops a stage without the
flag. Removal deletes the stage's namespace, services and credentials; its
databases on the shared PostgreSQL server stay, so re-enabling the stage
reconnects to its run and report history. Dropping them is a deliberate,
separate act. Enforced by: `tools/olf/tests/infra/test_local_platform.py` and
the stage drill in `.github/workflows/local-full-e2e.yml`.

**D5. Foundation and platform are static infrastructure; artifacts is
dynamic, and that split is the CD boundary.** Terraform owns the state and
drift of the static phases, and a Terraform run is a deliberate platform
action. A commit that changes only `lakehouse_code/` runs the artifacts phase
and nothing else; CI never invokes Terraform for a code change. The dynamic
lifecycle is build once, then activate per stage (ADR 0012). Enforced by:
`tools/olf/olf/infra/deployment/engine.py`.

**D6. A platform apply never waits on an artifact and never reads user
code.** Anything that needs domain code belongs in artifacts. Catalog
namespaces are derived from descriptors, so `olf infra catalog
sync-namespaces` reconciles them at the start of artifacts, before any table
is written, rather than in Terraform. Pruning an undeclared namespace is
opt-in and drops catalog metadata only. Enforced by: the namespace checks in
`tools/olf/olf/common/contracts_check/_hcl.py` and
`tools/olf/tests/infra/test_catalog.py`.

**D7. Teardown is not symmetric.** `olf infra destroy` accepts `all`,
`platform` and `foundation`; `all` tears down platform, then foundation.
There is no artifacts teardown: artifacts live inside the platform services
and the object store and are replaced on the next artifacts run. Enforced
by: `destroy` in `tools/olf/olf/infra/commands/deployment.py`.

## Consequences

- `olf infra deploy --phase artifacts` is the inner development loop: the only
  command needed after a change to a dbt model, a Floe contract, a Dagster
  definition or a descriptor.
- Disabling a stage in the profile is destructive for its services, which is
  why D4 asks for an explicit flag.
- On a fresh install the governed stages' Dagster pods wait in
  `CreateContainerConfigError` until D2 copies the ingestion-bot Secret; the
  Dagster release does not wait for its pods, so the apply is not held up.
- A new data product needs no platform apply: its namespaces arrive with
  artifacts (D6).

## Boundaries

| Not decided here | Where |
| --- | --- |
| How `olf` runs Terraform, Helm and kubectl, its toolchain, and the no-shell rule | [ADR 0008](0008-olf-owns-orchestration-and-toolchain.md) |
| Which surface a lifecycle command sits on, aliases, and the command list | [ADR 0020](0020-infrastructure-common-and-platform-cli-surfaces.md); [CLI reference](../reference/cli.md) |
| The Deployment Profile, topology resolution, `--provider` and the deprecated `--profile` shorthand | [ADR 0011](0011-deployment-profile-and-stages.md) |
| Revision build, verification, activation and when `ACTIVE.json` moves | [ADR 0012](0012-project-revisions-and-promotion.md) |
| Catalog namespace names and the medallion layout | [ADR 0004](0004-medallion-layout-and-catalog-namespaces.md) |
| The Polaris objects the bootstrap creates, and how a prune takes Gold grants with it | [ADR 0019](0019-data-plane-authorization-deferral.md) |
| `sync-namespaces` flags and prune rules | [CLI reference](../reference/cli.md#deploy-and-operate) |
| A client project's CD workflow | [ADR 0018](0018-github-is-the-forge.md) D5 |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Each Terraform root derives the namespace list from the descriptors at plan time (#88) | Makes `platform` read user code; reverted |
| Namespaces from a Terraform-templated bootstrap Job and `aws_glue_catalog_database` | A new product needed a platform apply |
| Polaris and OpenMetadata bootstraps as Terraform Jobs inside the apply | Replaced by `olf` steps after the apply (#562); Trino could not wait on credentials that exist only after it |

## History

- 2026-08-29 (#133): D3 and D4 added: the platform phase owns the
  topology-derived namespaces, and stage removal needs an opt-in.
- 2026-09-03 (#115): D5's dynamic lifecycle split into immutable build and
  stage activation.
- 2026-09-28 (#155): D4's `--allow-stage-removal` added to `olf infra
  platform apply -f`.
- 2026-11-05 (#89): D6: catalog namespaces left Terraform for the start of
  artifacts.
- 2026-11-06 (#562): D2: the Polaris and OpenMetadata bootstraps and the
  ingestion-bot Secret copy moved from Terraform Jobs into `olf` after the
  apply; Trino rolls on a credential digest.
- 2026-11-22 (#1385): Corrected: `olf infra deploy` and its artifacts phase
  are not deprecated; only the `--profile` shorthand is (ADR 0011).
- 2026-11-22: Rewritten in the standard form with D1 to D7; no decision
  changed.

Merges the earlier records 0008 (two-phase deploy), 0017 (shell/Python
split), 0022 (catalog namespace reconciliation), 0025 (`olf` owns the local
lifecycle) and 0027 (`olf` owns the cloud lifecycles).
