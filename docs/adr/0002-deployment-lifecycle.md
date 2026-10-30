# ADR 0002: Deployment lifecycle — foundation, platform, artifacts

## Status

Binding.

## Context

Deployment has to answer two different questions at once, and conflating them
has been a recurring source of confusion:

- **What runs, in what order?** Creating a cluster, applying Terraform services,
  and deploying code are genuinely three different operations with different
  inputs and different failure modes.
- **What is safe to re-run in CI on a code commit?** Terraform-managed
  infrastructure and code-derived artifacts have different lifetimes. A change
  to a dbt model must not put a Terraform apply in the CI path.

Earlier records answered these separately and left the count ambiguous — one ADR
described a "two-phase deploy", later ones described foundation/platform/
artifacts sequencing, and a reader had no way to tell whether that was two steps
or three.

It is three phases, in two lifecycle categories. Those are different axes.

## Decision

### Three ordered phases

`olf platform apply -f openlakeforge.yaml` runs the static prefix in order.
The deprecated `olf deploy --provider P` retains the old single-DEV path.

| Phase | Creates | Engine |
| --- | --- | --- |
| `foundation` | Kubernetes cluster and container registry — kind locally, EKS + ECR on AWS, AKS + ACR on Azure | Terraform |
| `platform` | Kubernetes namespaces plus the long-lived platform services: SeaweedFS, PostgreSQL, Polaris, Trino, Dagster, and (analytics/governance enabled) Superset and OpenMetadata | Terraform + Helm |
| `artifacts` | Deprecated single-DEV compatibility path derived from `lakehouse_code/` | `olf` |

`DeploymentPhase` (`tools/olf/olf/deployment/engine.py`) also carries a
`prefetch` value that runs between `foundation` and `platform`. It is a local
kind image warm-up — it pulls the large runtime images and loads them into the
kind nodes so the Helm releases do not each wait on a cold pull. On cloud
providers it is a deliberate no-op (`deployment/cloud/provider.py`). It is an
optimization inside the foundation→platform transition, not a fourth phase, and
is named here only so a reader counting `DeploymentPhase` members is not misled.

### The platform phase owns namespaces, and which services repeat

The resolved `DeploymentTopology` (ADR 0011) is a typed input to the platform
root, which derives every namespace from it: one shared `olf-system` holding
the services a deployment runs once -- PostgreSQL, SeaweedFS, Polaris, Trino,
OpenMetadata -- and one `olf-<stage>` per enabled stage holding that stage's
Dagster and, where analytics is enabled, its Superset. Stage-scoped services
are `for_each` instances over the enabled stages, never copied module blocks,
and each owns its own metadata database on the shared PostgreSQL server.

Because a stage's namespace holds its services, disabling a stage is
destructive. `olf platform apply -f` (and the deprecated `olf deploy`)
compares the resolved topology against the root's applied `stage_names` output
and refuses an apply that would drop a stage unless `--allow-stage-removal`
says so. The multi-stage nightly proves both halves: the refusal, and removal
with the opt-in.

Removal deletes the stage's namespace, its services, and their credentials.
Its databases on the shared PostgreSQL server are retained: dropping a stage's
run and report history as a side effect of a profile edit is not an apply's
decision to make, so re-enabling a stage reconnects to the state it had. A
deployment that wants that history gone drops those databases deliberately.

### Two lifecycle categories

The three phases divide in two:

```text
foundation + platform   ->  static infrastructure   (Terraform owns state and drift)
artifacts               ->  dynamic, code-derived   (rebuilt from lakehouse_code/)
```

**This split is the CD boundary.** A commit that changes only `lakehouse_code/`
triggers the artifacts phase and nothing else; CI never invokes Terraform for a
code change. Terraform runs are a deliberate platform action.

In v0.3 the dynamic lifecycle is split into immutable project build and stage
activation. `olf project build` publishes a `ProjectRevision`; `olf project
deploy -f openlakeforge.yaml --stage STAGE --revision REVISION` verifies it,
renders stage-bound Floe output, and changes that stage only. It never invokes
Terraform or reads the caller's `lakehouse_code/`. `ACTIVE.json` is committed
only after the user-code Helm release is ready, so an unsuccessful activation
leaves the prior executable revision active.

The corollary is a rule: **a platform apply must never wait on an artifact.**
Anything requiring domain code to exist belongs in the artifacts phase. Catalog
namespaces are the worked example — they are derived from descriptors, so
`olf catalog sync-namespaces` runs first inside artifacts, before any table is
written, rather than being templated into a Terraform-managed bootstrap job.
That keeps `platform` from reading user code on either catalog provider.

### Teardown is not symmetric

`olf destroy` accepts `all`, `platform`, and `foundation` only. There is no
artifacts teardown: artifacts have no independent lifetime to reclaim: they live
inside the platform services and the object store, and are replaced wholesale on
the next artifacts run. `all` tears down platform then foundation.

## Consequences

`olf deploy --provider local --phase artifacts` is the inner development loop.
It is the only command needed after a change to a dbt model, a Floe contract, a
Dagster definition, or a descriptor.

Phases are idempotent. Re-running `foundation` or `platform` reconciles existing
state rather than requiring a teardown.

`--profile` is not a phase, and it is not an environment or a stage either — it
names the *preset* axis of the Deployment Profile (ADR 0011): which optional
services `platform` deploys and which layers `artifacts` targets. It must be
passed consistently: running `--phase artifacts` with the default `full`
profile against a Slim platform regenerates governance-enabled manifests for
services that are not deployed. `--provider local --profile slim|full` is the
deprecated single-DEV-stage shorthand for the general profile/stage model; ADR
0011 defines the typed resolver it is a special case of.

## History

2026-08-29: Recorded that the platform phase owns the shared and stage
namespaces derived from the resolved topology, and that removing a stage needs
an explicit opt-in (#133). The three phases and their ordering are unchanged.

2026-09-03: Split the v0.3 dynamic lifecycle into immutable project build and
stage activation (#115). Platform commands are profile-only and never inspect
project source; the legacy artifacts phase remains a single-DEV compatibility
path.

2026-09-28: `olf platform apply -f` gained `--allow-stage-removal` (#155).
Before, the profile path ran the removal guard with no way to opt in, so a
stage could only be removed through the deprecated `olf deploy`.

Merges the decisions previously recorded as ADR 0008 (two-phase deploy), 0017
(shell/Python split), 0022 (Phase 2 catalog namespace reconciliation), 0025
(`olf` owns the local lifecycle), and 0027 (`olf` owns the cloud lifecycles).
Ownership of the orchestration itself is ADR 0008 in the current numbering.
