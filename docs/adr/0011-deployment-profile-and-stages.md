# ADR 0011: Deployment Profile v1 — provider, stage, and preset

## Status

Binding.

## Context

v0.3 (`#111`) introduces shared lifecycle stages (`dev`, optional `uat`,
`prod`) and exact-revision promotion between them on one Kubernetes cluster.
Building that requires one versioned, user-facing description of what a
deployment should look like, resolved before any Terraform, Helm, or
Kubernetes object exists.

Before this record, `olf deploy --provider local --profile slim|full`
conflated separate concepts behind two flags:

- **provider** — where the deployment runs (`local`, `aws`, `azure`);
- **stage** — a shared lifecycle boundary (`dev`, `uat`, `prod`);
- **preset** — a named default/override set for optional capabilities
  (`slim`, `full`);
- **runtime** — the query engine a stage uses (only Trino exists today).

A fourth profile concept, **sizing**, was added later (#323): how large the
shared query engine and the self-hosted object storage are.

`--profile` names the preset but reads as an environment; ADR 0002 already
warns "`--profile` is not a phase," and ADR 0009 called the project-root
`openlakeforge.yaml` "a structural marker pending its semantic resolver."
Neither record gave the four concepts distinct types. This ADR does.

## Decision

### The concepts stay distinct types throughout the resolver

| Concept | Values (v1) | Lives in |
| --- | --- | --- |
| Provider | `local`, `aws`, `azure` | `spec.provider.type` |
| Stage | `dev`, `uat` (optional), `prod` | `spec.stages.<name>` |
| Preset | `slim`, `full` | `spec.preset` |
| Sizing | `small` (`medium`, `large` are named but rejected) | `spec.query.size`, `spec.storage.size` |
| Runtime | not exposed — Trino only | not a v1 field |

A provider is not a stage. A stage is not a Kubernetes cluster — v0.3 runs
every stage on one shared cluster. A preset is a default/override set for
optional per-stage capabilities, not a second architecture. Runtime choice
stays out of the schema entirely while Trino is the only supported
shared-stage runtime; adding it later is an additive schema change, not a
retrofit.

### Deployment Profile v1

The project-root `openlakeforge.yaml` describes product intent only:

```yaml
apiVersion: openlakeforge.io/v1alpha1
kind: DeploymentProfile

metadata:
  name: acme-data

spec:
  provider:
    type: aws
    region: eu-west-3

  preset: slim

  stages:
    dev:
      enabled: true

    prod:
      enabled: true
      capabilities:
        analytics: true
        governance: true
```

`tools/olf/olf/common/profile.py` models this as frozen typed value objects
(`DeploymentProfile`, `ProviderSpec`, `StageSpec`, `StageCapabilities`, plus
`AccessSpec` and `IdentitySpec` for the optional `spec.access` and
`spec.identity`) and
resolves it into a separately typed `DeploymentTopology`. The desired and
resolved shapes are never the same object: `DeploymentProfile` is what the
user wrote, `DeploymentTopology` is the one effective, fully-defaulted result.
Validation is layered the same way as the Lakehouse/Source descriptors (ADR
0005): a hand-rolled parser in `olf/common/profile.py` for actionable, fail-closed
errors, and `docs/schema/deployment-profile.schema.json` for the versioned,
tool-checkable shape. Neither substitutes for the other, and both are
exercised by `olf check contracts`.

### Sizing: `spec.query.size` and `spec.storage.size`

Two optional fields choose a size for the shared Trino (`query`) and for
self-hosted object storage (`storage`, SeaweedFS). The vocabulary is
`small | medium | large`; the profile never carries CPU, memory, worker, heap or
volume quantities, and there is no per-stage size, because one Trino serves
every stage. Both default to `small`, which is today's footprint, so a profile
without the fields resolves and renders exactly as before.

Only tiers with a tested backend are accepted. `medium` and `large` are part of
the vocabulary but fail validation, naming the issue that will implement them
(#324 for Trino workers, #326 for SeaweedFS volumes), rather than being
accepted as a promise nothing keeps. Growing the set is the change that
implements and tests the tier; existing profiles stay valid. `storage` is also
rejected on `aws`, whose object storage is managed S3 with no volume to size;
it applies on `local` and `azure`, where SeaweedFS is self-hosted.

The resolved sizes are carried on `DeploymentTopology` (`query_size`,
`storage_size`) and printed by `olf profile resolve`. `query_size` selects a
row of `release/query-sizes.yaml`, which every provider root receives as its
`query_sizing` variable; a size change therefore moves only the Trino Helm
values, never a ProjectRevision or the project image. `storage_size` is read by
#326, and the capacity preflight is #327.

### `spec.project`: the customer's source and image registry

An optional `spec.project` declares `repository` (the source URL, provenance
only: `olf` builds the checked-out directory and never trusts a ref's mutable
head), `image.repository` (`host[:port]/path`, no tag or digest) and
`image.pull_secret_ref.name` (a Kubernetes Secret created out-of-band). The
profile carries references, never credentials: unknown fields, credentials in a
URL and a registry-less image reference are rejected. `spec.identity.github`
stays platform human roles; it never holds registry credentials. ADR 0012 still
keeps `openlakeforge.yaml` out of a `ProjectRevision`.

`olf.common.profile.resolve_project` picks the image repository in a fixed order:
explicit profile, then `PROJECT_CODE_IMAGE_REPOSITORY` (compatibility
override), then the provider registry (ECR or ACR) for a declared project
without an image, and the public example only when `spec.project` is absent.
A declared project never resolves to the example, and a local project must name
its image because kind has no registry. Existing profiles are unchanged. The
resolver exposes the result; wiring it into the deploy is #539 and #540.

### Resolution is deterministic

| Input | Effective result |
| --- | --- |
| Stage absent from `spec.stages` | Disabled; PROD is never created implicitly |
| Stage present, `enabled` omitted | `enabled: true` |
| Stage present, no `capabilities` | Preset default (`slim` → both `false`, `full` → both `true`) |
| Stage present, capability stated | Explicit value wins over the preset |
| Disabled stage | Capabilities always resolve to `false`, regardless of what was written |

### Fail-closed validation

Unknown fields at any level, an unsupported `apiVersion`/`kind`, an unknown
stage name, an unsupported `preset` or `provider.type`, no stage enabled at
all, a `region` set for `provider.type: local`, and a `uat`/`prod` stage
enabled while `dev` is disabled (every promotion in the v0.3 model sources
from DEV) are all rejected before a resolved topology is produced.

### v1 frozen exclusions

The following are not v1 profile fields, because no provider adapter maps
them to anything real yet — a field with no implemented, testable mapping
becomes a no-op promise, which is worse than deferring it:

`runtime`, personal workspace policy, raw CPU/memory/worker/volume sizing
(only the `small | medium | large` vocabulary above exists), availability, raw namespaces, replicas, resources, Helm values, Terraform
modules or backends, IAM policies, credentials, images, PVCs, buckets,
catalogs, and generated endpoints (the project image repository and pull secret
*name* of `spec.project` are the declared exception: user-owned references). `DeploymentTopology` carries only logical
service identities (`catalog`, `query`, `metadata_database`, `governance` as
shared services; `orchestration`, `reporting` per stage) — never namespaces,
Helm releases, or endpoints. The one exception is `identity` (`issuer_url`,
`role_claim`, `role_mapping`): an external issuer's URL and claim vocabulary
are user-owned facts that no provider can generate, and the provider contract
must be checked against them (ADR 0014). It is never a credential. The typed
provider-contract v3 resolver derives those from this topology (ADR 0003):
every platform root emits and provisions the resolved stage bindings through
its provider adapter (#133, #114). None belongs in this profile.

### The v0.2 compatibility path

`olf deploy --provider local --profile slim|full` keeps its exact current
behavior as a deprecated shorthand: `olf.common.profile.legacy_single_stage_topology`
resolves it to one enabled DEV stage using the preset's capability defaults,
proving the shorthand is exactly the single-DEV-stage case of the general
model.

`DeploymentContext` now carries the resolved topology and the selected stage.
With no `--profile`, the project-root Deployment Profile is authoritative; an
explicit `--profile slim|full`, or a project that has no profile file at all,
resolves the single-DEV shorthand instead. A profile file that exists but is
invalid fails closed. Namespaces are derived from the topology but are not
part of it: `olf-system` for shared services and `olf-<stage>` per enabled
stage live in `olf.common.deployment.context`, since ADR 0011 keeps the resolved
topology free of namespaces, Helm releases, and endpoints.

## Consequences

- `olf profile validate` and `olf profile resolve --json` expose the
  effective topology before any mutation, for both humans and CI.
- The stage-aware platform, data plane, project revision and activation
  (`#133`, `#114`, `#154`, `#115`) consume `DeploymentTopology`, not
  `--profile`/`--provider` flags directly: `olf platform apply -f` and
  `olf project deploy -f --stage` take the profile file.
- Local, AWS, and Azure POC shapes are representable without credentials,
  raw resource values, or endpoints — those stay behind provider contracts (ADR 0003).

## History

New record. No prior ADR modeled provider, stage, and preset as distinct
types; ADR 0002 and ADR 0009 are corrected in place to stop describing
`--profile` as an environment or the profile file as inert.

2026-08-28: Updated the provider-contract handoff after v3 established the
typed topology-to-provider boundary; no profile fields were added.

2026-08-29: The topology reaches Terraform (#133). `DeploymentContext`
resolves it, the local platform root consumes it as typed variables, and
namespace derivation is recorded as living outside the topology. No profile
fields were added.

2026-10-01: Rewritten as current behavior for the 0.3.0-alpha.1 release: the
roots provision the resolved bindings, and the lifecycle commands consume the
topology. No profile fields were added.

2026-10-02: Added optional `spec.access` (`base_domain`, `issuer`) for
service access (#264). It is deployment configuration owned by ADR 0013, not a
fifth topology concept; omitted, it defaults to the local evaluation install.

2026-10-09: Added optional `spec.identity` (`issuer`, and for an external
issuer `issuer_url`, `role_claim`, `role_mapping`) selecting the OIDC issuer.
The seam is owned by ADR 0014; the resolved topology carries it and the
provider contract must match it.

`spec.project` (#538) added in place: the customer's source and image registry
as references, with a fixed resolution order and no credentials. The earlier
"images" exclusion is narrowed to what the paragraph above allows.

2026-10-30: Sizing (#323) added as the fourth profile concept: optional
`spec.query.size` and `spec.storage.size`, default `small`. Per the v0.6
lightweight-beta decision of 2026-10-27, `medium` and `large` fail validation
until a tested backend lands (#324, #326) and `storage` is rejected on `aws`.
The "sizing" exclusion is narrowed to raw resource values; the contract slice
changes no rendered Terraform or Helm input.

2026-10-30: `query.size` renders Trino from `release/query-sizes.yaml` through
each root's `query_sizing` variable (#324). `small` renders the same values as
before. The `medium` and `large` rows are defined but still fail validation:
workers need network flows, authentication across nodes (#367) and a kind run
before the tier counts as tested.
