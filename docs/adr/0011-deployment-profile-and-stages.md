# ADR 0011: Deployment Profile v1beta1 — provider, stage, and preset

| | |
| --- | --- |
| Status | Binding; `aws` and `azure` are preview in v1beta1 |
| Built | local ✔ · AWS ✔ · Azure ✔ (the resolver is provider-neutral; the clouds render one shared Trino, see Consequences) |

## Context

Shared lifecycle stages (`dev`, optional `uat`, `prod`) with exact-revision
promotion between them on one Kubernetes cluster need one versioned,
user-facing description of what a deployment should look like, resolved
before any Terraform, Helm, or Kubernetes object exists. The v0.2 command
line, `olf deploy --provider local --profile slim|full`, conflated separate
concepts behind two flags: where the deployment runs, which lifecycle
boundary it is, which optional capabilities it carries, and which query
engine it uses. `--profile` named a capability preset but read as an
environment. This record gives each concept its own type and fixes how the
project-root `openlakeforge.yaml` resolves. Every field, type and default is
in the [Deployment Profile reference](../reference/deployment-profile.md).

## Decision

**D1. Provider, stage, preset and sizing are distinct typed concepts; runtime
is not a field.** They stay separate types throughout the resolver:

| Concept | Values (v1beta1) | Lives in |
| --- | --- | --- |
| Provider | `local`, `aws`, `azure` | `spec.provider.type` |
| Stage | `dev`, `uat` (optional), `prod` | `spec.stages.<name>` |
| Preset | `slim`, `full` | `spec.preset` |
| Sizing | `small`, `medium`, `large` (D6) | `spec.query.size`, `spec.stages.<stage>.query.size`, `spec.storage.size` |

A provider is not a stage. A stage is not a Kubernetes cluster: every stage
runs on one shared cluster. A preset is a default/override set for optional
per-stage capabilities, not a second architecture. Runtime choice stays out
of the schema while Trino is the only shared-stage runtime; adding it later is
an additive change. Enforced by: `Provider`, `Preset`, `Size` and `StageSpec`
in `tools/olf/olf/common/profile.py`.

**D2. `spec.provider.type` selects a deployment target and the default
adapter for each capability; the adapter selectors are reserved and accept
only those defaults.** `cluster.adapter`, `storage.adapter`,
`catalog.adapter`, `metadata_database.adapter` and `state.backend` exist
before the beta freeze so that existing Kubernetes, external S3, external
PostgreSQL and remote state arrive as additive selections without changing
what `provider.type` means. A selection other than the provider's default
fails and names the issue that implements it; it never silently deploys the
default. Explicit defaults resolve identically to omitted selectors, and the
resolver prints the chosen adapters (`adapters` in `olf profile resolve
--json`) either way. The per-provider defaults are in the
[reference](../reference/deployment-profile.md#specprovider). Enforced by:
`_DEFAULT_ADAPTERS` and `_ADAPTER_TRACKERS` in
`tools/olf/olf/common/profile.py`.

**D3. In v1beta1, `aws` and `azure` are preview.** The schema accepts them,
but the beta compatibility promise covers `local` only, and `olf profile
validate` prints a notice for a preview provider. Enforced by:
`tools/olf/olf/common/commands/profile.py`.

**D4. The profile is `openlakeforge.io/v1beta1` `DeploymentProfile`, and what
the user wrote is never the same object as what it resolves to.**
`DeploymentProfile` is the desired shape; `DeploymentTopology` is the one
effective, fully defaulted result, and every consumer reads the topology.
Validation is layered as for the descriptors (ADR 0005): a hand-written,
fail-closed parser that gives actionable errors and is the authority, and
`docs/schema/deployment-profile.schema.json` for the versioned,
tool-checkable shape; neither substitutes for the other. A `v1alpha1` file is
not read: validation names `olf project migrate`, which rewrites it and
refuses anything it cannot move safely. Enforced by:
`tools/olf/olf/common/profile.py`, the schema, and `olf check contracts`,
which exercises both.

**D5. An optional block that a profile may omit is added to v1beta1 without a
version bump.** Profiles that omit it resolve as before. `spec.access`,
`spec.identity`, `spec.project`, the sizing blocks and `spec.observability`
were added this way. Enforced by: `PROFILE_API_VERSION` in
`tools/olf/olf/common/profile.py`, which none of them changed.

**D6. Sizing uses only the `small | medium | large` vocabulary, and only a
tier with a tested backend is accepted.** The profile never carries CPU,
memory, worker, heap or volume quantities. `spec.query.size` is the default
for every stage and `stages.<stage>.query.size` overrides it; storage has one
platform-wide size; both default to `small`, so a profile without them
renders as before. A named tier without a tested backend fails validation and
names the issue that will implement it, rather than being accepted as a
promise nothing keeps; today `query` accepts only `small` (#324) and
`storage` accepts all three. `storage.size` is rejected on `aws`, whose
managed S3 has no volume to size. A size selects a row of
`release/query-sizes.yaml` or `release/storage-sizes.yaml` and so moves only
Helm values, never a `ProjectRevision` or the project image. Enforced by:
`_IMPLEMENTED_SIZES` in `tools/olf/olf/common/profile.py`.

**D7. A storage volume only grows.** Before any mutation, `olf` refuses a
size below a claim's current request, and a larger size when the claim's
StorageClass does not allow volume expansion. It never deletes and recreates
a volume to apply a size. How it expands, and how to move a deployment that
cannot, is in the
[reference](../reference/deployment-profile.md#changing-the-storage-size).
Enforced by: `tools/olf/olf/infra/deployment/storage_resize.py`.

**D8. `spec.project` holds references, never credentials, and the image
repository resolves in a fixed order.** It declares the source repository
(provenance only: `olf` builds the checked-out directory and never trusts a
ref's mutable head), the image repository (`host[:port]/path`, no tag or
digest) and the name of an out-of-band pull Secret. Unknown fields,
credentials in a URL and a registry-less image are rejected. The image
repository is the profile's, then the `PROJECT_CODE_IMAGE_REPOSITORY`
override, then the provider registry for a declared project, and the public
example only when `spec.project` is absent: a declared project never
resolves to the example, and a local project must name its image because
kind has no registry. `openlakeforge.yaml` stays out of a `ProjectRevision`
(ADR 0012). Enforced by: `resolve_project` in
`tools/olf/olf/common/profile.py`.

**D9. Resolution is deterministic.**

| Input | Effective result |
| --- | --- |
| Stage absent from `spec.stages` | Disabled; PROD is never created implicitly |
| Stage present, `enabled` omitted | `enabled: true` |
| Stage present, no `capabilities` | Preset default (`slim` → both `false`, `full` → both `true`) |
| Stage present, capability stated | Explicit value wins over the preset |
| Disabled stage | Capabilities always resolve to `false`, whatever was written |

Enforced by: `resolve_topology` in `tools/olf/olf/common/profile.py`.

**D10. Validation fails closed.** Unknown fields at any level, an unsupported
`apiVersion` or `kind`, an unknown stage name, an unsupported `preset` or
`provider.type`, no stage enabled, a `region` on `provider.type: local`, and
`uat` or `prod` enabled while `dev` is disabled (every promotion sources from
DEV) are all rejected before a topology is produced. The full rule list is
the reference's [cross-field rules](../reference/deployment-profile.md#cross-field-rules).
Enforced by: `tools/olf/olf/common/profile.py` and
`tools/olf/tests/common/test_profile.py`.

**D11. The v1 exclusions are frozen, and the topology carries only logical
service identities.** A field with no implemented, testable provider mapping
becomes a no-op promise, so these are not profile fields: `runtime`,
`query.engine` (DuckDB stays out until its Slim conformance gates pass,
#547), `spec.expose` (public exposure waits for the cloud beta), personal
workspace policy, raw CPU, memory, worker or volume sizing, availability, raw
namespaces, replicas, resources, Helm values, Terraform modules or backends,
IAM policies, credentials, images, PVCs, buckets, catalogs and generated
endpoints. The image repository and pull Secret name of `spec.project` (D8)
are the declared exception. `DeploymentTopology` names services only
logically (`catalog`, `query`, `metadata_database`, `governance` shared;
`orchestration`, `reporting` per stage), never namespaces, Helm releases or
endpoints; its one user-owned fact is `identity` (`issuer_url`, `role_claim`,
`role_mapping`), which no provider can generate and the provider contract
must match (ADR 0014). The provider-contract resolver derives the physical
bindings from the topology (ADR 0003), and namespaces (`olf-system` shared,
`olf-<stage>` per stage) are derived outside it. Enforced by:
`DeploymentTopology` in `tools/olf/olf/common/profile.py` and
`stage_namespace` in `tools/olf/olf/common/deployment/context.py`.

**D12. The profile names the provider, and `--profile slim|full` is a
deprecated shorthand for one DEV stage.** `DeploymentContext` carries the
resolved topology and the selected stage. The provider is the profile's
`spec.provider.type`; `--provider` is optional and must match it. With no
`--profile`, the profile is authoritative; `--profile slim|full` resolves to
one enabled DEV stage with that preset's capability defaults on the
profile's provider, the single-DEV case of the general model. A profile file
that is invalid or names another provider fails closed, with or without
`--profile`; with no profile file at all, the shorthand resolves on
`--provider`, or `local`. Enforced by: `resolve_topology` in
`tools/olf/olf/infra/commands/_shared.py` and
`legacy_single_stage_topology` in `tools/olf/olf/common/profile.py`.

## Consequences

- `olf profile validate` and `olf profile resolve --json` expose the
  effective topology before any mutation, for people and CI.
- The lifecycle commands consume `DeploymentTopology`, not flags:
  `olf infra platform apply -f` and `olf infra project deploy -f --stage` take
  the profile file.
- Each stage gets its own Trino at its own size on the local root (ADR 0019
  D8); AWS and Azure still render one shared Trino, at the largest
  enabled stage's size.
- `query.size` `medium` and `large` stay rejected until workers have network
  flows, authentication across nodes (#367) and a kind run (#324). The
  storage `medium` and `large` rows are provisional until #329 measures them.
- kind's default `standard` StorageClass does not expand, so a local
  deployment is sized at creation; changing it means a rebuild.
- `olf infra deploy` does not read `spec.project` yet (#539).

## Boundaries

| Not decided here | Where |
| --- | --- |
| Every field, type, default and cross-field rule | [Deployment Profile reference](../reference/deployment-profile.md) |
| `spec.access`: ingress, routes and TLS | [ADR 0013](0013-service-access-and-ingress.md) |
| `spec.identity`: the issuer, roles and their mapping | [ADR 0014](0014-identity-and-authorization.md) |
| `spec.observability`: what each capability deploys | [ADR 0022](0022-pod-log-collection-and-archive-format.md), [ADR 0023](0023-alert-evaluation-and-webhook-delivery.md), [ADR 0024](0024-metrics-prometheus-reference-and-read-path.md) |
| How the topology becomes provider contracts | [ADR 0003](0003-provider-contracts.md) |
| The deploy phases and the CD boundary | [ADR 0002](0002-deployment-lifecycle.md) |
| What a revision contains and how a stage activates it | [ADR 0012](0012-project-revisions-and-promotion.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Keep `--provider` and `--profile` as the deployment description | Conflates target, stage and preset; `--profile` reads as an environment |
| One object for the desired and the resolved profile | Defaults and user intent become indistinguishable |
| A `runtime` or `query.engine` field now | Only Trino exists; a field with no mapping is a no-op promise |
| Raw CPU, memory, worker or volume values in the profile | Couples the profile to one adapter's rendering |
| Accept `medium` and `large` before a backend is tested | A promise nothing keeps |
| A platform-wide query size frozen in the beta | Could not become per stage without a breaking change |
| Delete and recreate a volume to resize it | Loses data |

## History

- 2026-08 (#113): Created: provider, stage and preset as distinct types and
  Deployment Profile v1alpha1 (D1, D4, D9, D10, D11, D12).
- 2026-08-28: D11: the provider-contract v3 resolver derives the physical
  bindings from the topology.
- 2026-08-29 (#133): D11: namespaces are derived outside the topology.
- 2026-10-02 (#264): D5: optional `spec.access` added (owned by ADR 0013).
- 2026-10-09: D5, D11: optional `spec.identity` added; `identity` is the
  topology's one user-owned fact.
- 2026-10 (#538): D8 added; the "images" exclusion in D11 narrowed to it.
- 2026-10-30 (#323, #326): D6 added; the "sizing" exclusion in D11 narrowed to
  raw values; `storage.size` accepts `medium` and `large`.
- 2026-10-31 (#326): D7: volumes grow in place on an expandable StorageClass.
- 2026-11-06: D12: `--provider` defaults to the profile's provider, and an
  invalid or mismatched profile fails closed even with `--profile`.
- 2026-11-07 (#960, #972): D6: `query.size` can be set per stage.
- 2026-11-07 (#996): D2, D3: what `provider.type` promises; `aws` and `azure`
  are preview.
- 2026-11-10 (#1056): D2: adapter selectors reserved with provider defaults.
- 2026-11-11 (#532): D4: the profile is `v1beta1` (`spec.access.issuer`
  became `tls`); D11: `query.engine` and `spec.expose` are not fields.
- 2026-11-16 (#1117): D5: optional `spec.observability` added without a
  version bump.
- 2026-11-17 (#23): D5: `spec.observability.alerts.platform_receivers` added
  without a version bump.
- 2026-11-23: Rewritten in the standard form with D1 to D12; no decision
  changed.
