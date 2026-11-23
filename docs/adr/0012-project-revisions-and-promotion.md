# ADR 0012: Project revisions — a build-once, content-addressed promotion unit

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS and Azure: proof of concept (ADR 0010) |

## Context

The platform promotes one immutable project revision unchanged across shared
DEV, optional UAT and PROD (#111, #154). Before Floe 0.8, the only immutable
revision covered Floe runtime artifacts rendered from the live provider
contract, and that output is stage-bound: a generated manifest embedded the
stage's report bucket, namespace, catalog endpoints and Secret names, so a DEV
manifest could not be replayed in PROD, and hashing it put target-stage
endpoints inside the unit that has to promote unchanged. The project-code image
baked that revision too, so "the same digest deploys to DEV and PROD without a
rebuild" was unreachable.

## Decision

**D1. A `ProjectRevision` freezes the complete promotable project's inputs,
never rendered stage output.** It is one content-addressed revision with the
components `descriptors` (including `alerts.yml` when present), `floe`, `dbt`
(`gold/**` without `profiles.yml` or build output), `dagster`, `reports` (when
a dashboard exists), `image` (a repository plus an immutable `@sha256:` digest;
mutable tags are rejected) and `distribution`. The Floe runtime version is
covered by the image digest, not duplicated in `floe`. Each component's paths:
[What is frozen](../architecture/project-revisions.md#what-is-frozen).
Enforced by: `tools/olf/olf/infra/project_revision.py`.

**D2. A revision excludes deployment intent and everything a stage resolves.**
`openlakeforge.yaml`, the Floe EnvironmentProfile the code location renders,
and any URI a provider contract resolves are never in a revision. Enabling UAT
or moving a project to another provider must not invalidate a built revision,
and a revision that embedded a rendered artifact could not be replayed against
another stage's contract. Enforced by: the component globs and the value scan
(D5) in `tools/olf/olf/infra/project_revision.py`.

**D3. A revision is content-addressed, and publication is immutable.** Each
component is a `{relative project path: sha256}` map, and the revision is a
sorted `<component>/<path>\0digest\n` hash over the flattened entries, so an
empty project and a tampered entry set fail the same way. Publishing an
existing key with different content is a hard collision error, never an
overwrite. Enforced by: `packages/core/openlakeforge_core/project_revision.py`
and `RevisionStore` in `tools/olf/olf/infra/artifact_store.py`.

**D4. A revision is compatible by runtime contract version, not by
distribution version.** The runtime contract is the environment the code
server receives (`RUNTIME_ENV`, `RUNTIME_ALIASES`).
`release/component-catalog.yaml` records `distribution.runtime_contract`: the
current `version`, the `accepts` list this distribution still runs, and a
`binding_fingerprint` of that surface. A `schema_version` 3 sidecar records
`runtime_contract_version` and binds it into the `distribution` component;
`verify` accepts it when the version is in the running distribution's
`accepts`, and otherwise fails naming both versions and asks for a rebuild.
Sidecars at `schema_version` 1 and 2 keep the exact distribution-version
match. A change that breaks existing revisions bumps `version` and drops what
no longer works from `accepts`; a change they survive only refreshes the
fingerprint. The fingerprint covers names and mount paths, not the Dagster
version or what a value means, so such a change bumps the contract by
decision. Enforced by: `olf release check` (fingerprint) and
`tools/olf/olf/common/release/_compatibility.py`.

**D5. Validation is layered, like descriptors and profiles.**
`ProjectRevisionManifest` has a hand-written model for actionable,
fail-closed errors, and `docs/schema/project-revision.schema.json` for the
versioned shape (ADR 0005, ADR 0011). `olf infra project build` requires both:
the manifest round-trips through its aggregate check, declares every required
component (`descriptors`, `floe`, `dbt`, `dagster`, `image`, `distribution`),
carries a digest-pinned `project_code_image`, and passes a value scan for
physical locations and credential literals before it is published and before
`inspect` or `verify` read it back. The scan is defense in depth, not a
secret scanner: the guarantee is that frozen inputs are provider-neutral
sources. What it rejects and exempts: [The value scan](../architecture/project-revisions.md#the-value-scan).
Enforced by: `_FORBIDDEN_VALUE_PATTERNS` in `tools/olf/olf/infra/project_revision.py`.

**D6. Stage binding happens on the pod.** Nothing a revision freezes names a
stage, so one image digest serves every stage. Floe's profile is rendered when
the code location loads (ADR 0006 D5) and dbt reads its connection from the run
pod (ADR 0006 D6). A dlt source's `source.yaml` names the keys its loader
reads; the stage binds their values in ConfigMap `olf-source-<id>` and Secret
`olf-source-<id>-credentials` in its own namespace, a product job's run pod
receives them as `SOURCES__<ID>__<KEY>`, and incremental state lives in the
stage's Bronze bucket under pipeline `<stage>__<source>`. The code server
receives only the contract values the runtime reads (`_RUNTIME_ENV`) and the
stage; source bindings go to run pods, never the code server. Changing a
stage's endpoint, credential or bucket changes neither the digest nor the
image. `olf infra deploy` refuses an activation whose sources the stage has not
bound, reading Secret key names only. Paths and limits: [where stage bindings
reach a workload](../architecture/provider-contracts.md#where-stage-bindings-reach-a-workload).
Enforced by: `_RUNTIME_ENV` in `tools/olf/olf/infra/deployment/activation.py`.

**D7. An activation is a separate, immutable record, and `ACTIVE.json` moves
only after the rollout.** `ACTIVATION.json` (schema 2), under
`activations/<stage>/revisions/sha256/<digest>/`, binds the project revision,
the stage's provider-contract digest, its capabilities, the image and the
running distribution version, and carries no Floe revision. The mutable
`ACTIVE.json` pointer changes only after the stage's Helm rollout succeeds.
Because the running distribution is recorded, re-activating a revision after a
platform upgrade rolls out again rather than matching the earlier activation.
Schema-1 records still read and hash as published but are not a rollback
target. Enforced by: `packages/core/openlakeforge_core/project_activation.py`.

## Consequences

- Promotion activates an already-built revision's image and component digests
  in each stage without rebuilding from source, and a revision is
  byte-identical whichever stage it is activated in or path it was built
  from.
- A runtime contract bump means revisions built before it must be rebuilt and
  promoted again before the upgraded platform activates them.
- Editing a source binding applies to the next run with no redeploy; changing
  a provider-contract value means activating the same revision again.
- The move to Floe 0.8 is one-way: a deploy whose pointer fails to commit over
  a schema-1 activation keeps its new release and asks to be rerun.

## Boundaries

| Not decided here | Where |
| --- | --- |
| The Dagster runtime that loads a revision, Floe Jobs and dbt parsing | ADR 0006 |
| Deployment intent and stages | ADR 0011 |
| The lifecycle phase an activation belongs to | ADR 0002 |
| The distribution payload's identity | ADR 0009 |
| How the alert evaluator reads a stage's policy | ADR 0023 |
| Command usage (`olf infra project build\|deploy\|status\|revision`) | [CLI reference](../reference/cli.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Freezing rendered stage artifacts (v0.2 `olf floe revision`) | Stage-bound; cannot be replayed in another stage |
| Including the Deployment Profile in the revision | Enabling a stage or changing provider would invalidate built revisions |
| Exact distribution-version match | Every patch release forced a rebuild of the revision active in PROD |
| Treating each Dagster pin move as a contract change | Brings back rebuild-on-every-patch |
| A broad scan for any `http(s)://`, `AWS_*` or `SECRET` | Flags env-var names and Secret references, the sanctioned way a credential reaches a pod |

## History

- Undated: New record, composing with ADR 0009's distribution-payload identity
  and ADR 0011's Deployment Profile.
- 2026-09-03 (#115): D7 added: the `ProjectActivation` record and the
  `ACTIVE.json` pointer.
- 2026-10-28 (#533): D1 and D7 changed: the Floe runtime-artifact revision is
  removed, `floe` covers the Floe project, and activations move to schema 2.
- 2026-10-28 (#546): D1 changed: `profiles.yml` leaves the `dbt` component.
- 2026-10-29 (#545): D1 changed: `descriptors` freezes `governance.yml` in
  place of `lakehouse.yaml`.
- 2026-10-29 (#546): D6 added: dbt, dlt and the code location bind on the pod.
- 2026-11-07 (#992): D4 changed: compatibility follows the runtime contract
  version (sidecar `schema_version` 3).
- 2026-11-12 (#1099, #1101), 2026-11-13 (#1106): D1 changed: `dbt` is the
  single Gold project and `floe` follows the Floe project layout.
- 2026-11-17 (#1208): D1 changed: `descriptors` also freezes `alerts.yml`.
- 2026-11-23: Rewritten in the standard form; no decision changed.
