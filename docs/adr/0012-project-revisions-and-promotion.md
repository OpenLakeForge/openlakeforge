# ADR 0012: Project revisions — a build-once, content-addressed promotion unit

## Status

Binding.

## Context

v0.3 (`#111`) promotes one immutable project revision unchanged across shared
DEV, optional UAT, and PROD (`#154`). Until Floe 0.8, v0.2's immutable
revision (`olf floe revision`) covered Floe runtime artifacts rendered from the
live provider contract environment -- configs, profiles and generated
manifests.

That rendered output is stage-bound. A generated manifest embedded the stage's
report bucket, Kubernetes namespace, catalog endpoints and Secret names, so a
manifest generated for DEV could not be replayed unmodified in PROD, and
hashing it would have put target-stage endpoints inside the unit #154 requires
to promote unchanged. The project-code image inherited the problem: it baked
the Floe revision it was built against, so "the same digest deploys to DEV and
PROD without a rebuild" was unreachable.

## Decision

### A project revision freezes inputs, never rendered stage output

`ProjectRevision` (`tools/olf/olf/project_revision.py`) is the one
content-addressed revision, covering the complete promotable project:

| Component | Source | Excluded because |
| --- | --- | --- |
| `descriptors` | `lakehouse_code/lakehouse.yaml`, `bronze/<source>/source.yaml` | — |
| `floe` | `silver/floe.yml`, `silver/sources.yml`, `silver/<domain>/contracts/floe/*.yml` | — |
| `dbt` | `gold/<product>/dbt/**` (no `target/`, `dbt_packages/`) | build output, not input |
| `dagster` | `pipelines/dagster/<product>.py`, `bronze/<source>/dlt/<source>.py` | — |
| `reports` | `dashboards/superset/<dashboard>/**`, when any dashboard exists | — |
| `image` | The project-code repository plus an immutable `@sha256:` digest | mutable tags are rejected |
| `distribution` | `release/component-catalog.yaml` distribution version | compatibility gate at activation |

`openlakeforge.yaml` (the Deployment Profile), the Floe EnvironmentProfile the
code location renders for its stage, and any URI a provider contract resolves
are never in a project revision. Enabling UAT, or moving a project between
providers, must not invalidate an already-built revision; and a revision that
embedded a rendered artifact could not be replayed against a different stage's
contract environment, which is exactly what `#115` needs it to do.

The Floe runtime version (`floe-python` and `dagster-floe`, pinned in
`images/project-code/pyproject.toml`) is part of the image, and so is covered
by the `image` component's digest rather than duplicated inside `floe`.

### Content address, publish, and verify

Each component is a `{relative project path: sha256}` map. The aggregate
revision is a sorted `<component>/<path>\0digest\n` hash over the flattened
entries, so an empty project and a tampered entry set fail the same way.
Publication is immutable: an existing key with different content is a hard
collision error, never a silent overwrite.

`tools/olf/olf/artifact_store.py` owns the bucket/client resolution revisions
publish through (`RevisionStore`, `S3RevisionStore` for in-cluster or direct
cloud S3, `FilesystemRevisionStore` for `olf project build --output DIR`).

### Validation is layered the same way as descriptors and profiles

`ProjectRevisionManifest` has a hand-rolled model (`to_json`/`from_json`/
`validate`) for actionable, fail-closed errors, and
`docs/schema/project-revision.schema.json` for the versioned, tool-checkable
shape (ADR 0005, ADR 0011). `olf project build` runs both: the manifest must
round-trip through its own aggregate check, declare every required component
(`descriptors`, `floe`, `dbt`, `dagster`, `image`, `distribution`), carry a
digest-pinned `project_code_image`, and pass a value scan before it is
published and before it is read back by `inspect`/`verify`.

The value scan (`_FORBIDDEN_VALUE_PATTERNS` in `project_revision.py`) is
deliberately narrower than "no `http(s)://`/`AWS_*`/`SECRET`": a bare env-var
*name* (`AWS_REGION`) or Kubernetes Secret *reference*
(`secret_name:`/`secretRef`/`client_secret_key`) is the sanctioned way a
credential reaches a pod and must never be flagged, only a concrete physical
value. It rejects `s3://` (always a bucket location, ADR 0004), bare
`http://` (this codebase's own convention for every in-cluster stage
endpoint — external APIs are `https://`, which is allowed), a concrete
`AKIA...` AWS access-key ID, and an assignment of a bare credential noun
(`password: ...`, `api_token = "..."`) to something that is not itself an
env-lookup or substitution expression (`os.environ[...]`, `os.getenv(...)`,
`${VAR}`) with no default -- a *default/fallback* argument on that same
lookup (`os.getenv("TOKEN", "sk-live-...")`, `${DB_PASSWORD:-hunter2}`) is
exactly where a real secret hides once the bare reference is exempted, so it
is checked and rejected explicitly rather than falling through the
exemption. It is a defense-in-depth layer, not a certified secret scanner —
the load-bearing guarantee is architectural: frozen inputs are
provider-neutral descriptors, checked-in Floe contracts, dbt/Dagster source,
and report assets (never a rendered artifact), so a physical endpoint or
credential should never appear in them to begin with.

### Stage binding happens on the pod

Nothing a revision freezes names a stage. The Floe project's storages and
catalog are filled in when the code location loads: `libs/floe_profile.py`
renders the EnvironmentProfile from the provider-contract values the
activation release sets on the pod, and dagster-floe builds the manifest in
memory from that profile and the frozen contracts. One project-code image
digest therefore serves every stage, and activation publishes no Floe
artifact of its own.

### CLI surface

| Command | Behavior |
| --- | --- |
| `olf project build --project P --image REF` | Validate the project, freeze its content, resolve/require a digest-pinned image, publish |
| `olf project revision inspect --revision R` | Read a published manifest without rebuilding source |
| `olf project revision verify --revision R` | Re-verify manifest self-consistency, the distribution-compatibility gate, and every published object's digest |
| `olf project deploy -f PROFILE --stage STAGE --revision R` | Materialize only verified published content and activate the stage |
| `olf project status -f PROFILE [--stage STAGE] [--json]` | Compare immutable activation records with the observed user deployment |


## Consequences

- `#115` consumes `ProjectRevision.project_code_image` and per-component
  digests to activate a stage without ever rebuilding from source.
- A revision built once is byte-identical regardless of which stage it is
  later activated in, or which path on disk the project was built from —
  `openlakeforge.yaml` and every stage binding are outside its content
  address by construction.
- An activation (`ACTIVATION.json`, schema 2) binds the project revision,
  the stage's provider-contract digest, its capabilities and the image; it
  carries no Floe revision. Schema-1 records, written while Floe manifests
  were rendered per stage, still read and hash exactly as published, so a
  stage activated before the change stays inspectable and a valid rollback
  target.

## History

2026-09-03: #115 added the derived `ProjectActivation` identity. It records
the project revision, stage contract binding, Floe runtime revision, enabled
capabilities, and digest-pinned image under
`activations/<stage>/revisions/sha256/<digest>/ACTIVATION.json`; the mutable
`ACTIVE.json` pointer changes only after the Helm rollout succeeds.

2026-10-28: Floe 0.8 builds its manifest in memory in the code location
(#533). The v0.2 Floe runtime-artifact revision, `olf floe revision` and the
per-stage manifest rendering at activation are removed, `ProjectActivation`
moves to schema 2 without a Floe revision, and the `floe` component covers
the multi-file Floe project.

New record. No prior ADR modeled a promotable project revision distinct from
the v0.2 Floe runtime-artifact revision; ADR 0009's distribution-payload
identity and ADR 0011's Deployment Profile are the two existing typed
identities this one composes with, not supersedes.
