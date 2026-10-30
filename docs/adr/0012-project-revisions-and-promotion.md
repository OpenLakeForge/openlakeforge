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

`ProjectRevision` (`tools/olf/olf/infra/project_revision.py`) is the one
content-addressed revision, covering the complete promotable project:

| Component | Source | Excluded because |
| --- | --- | --- |
| `descriptors` | `lakehouse_code/governance.yml`, `bronze/<source>/source.yaml` | — |
| `floe` | `silver/floe.yml`, `silver/sources.yml`, `silver/<domain>/contracts/floe/*.yml` | — |
| `dbt` | `gold/<product>/dbt/**` (no `profiles.yml`, `target/`, `dbt_packages/`, `logs/`) | stage connection and build output, not input |
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

`tools/olf/olf/infra/artifact_store.py` owns the bucket/client resolution revisions
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

dbt and dlt bind the same way (#546):

- **dbt.** The image carries one profile, `libs/dbt/profiles/trino.yml`,
  whose host, port, user and catalog are `env_var()` references with no
  default, so a pod missing one fails instead of reaching another stage.
  The image bakes a manifest parsed with placeholder values that no stage
  uses. It exists only so Dagster can load definitions. Each run re-parses
  in a fresh target path with its pod's values, and the run's own manifest
  is the one uploaded.
- **dlt.** A source's `source.yaml` names the keys its loader reads
  (`runtime.config` / `runtime.secrets`). The stage binds their values in the
  ConfigMap `olf-source-<id>` and the Secret `olf-source-<id>-credentials` in
  its own namespace. A product job's run pod receives them as dlt's own
  `SOURCES__<ID>__<KEY>` names. Incremental state lives in the stage's Bronze
  bucket under the pipeline `<stage>__<source>`, so no stage reads another's
  cursor.
- **What a pod receives.** The code location's container gets only the
  contract values the runtime reads (`_RUNTIME_ENV`), plus the stage itself.
  A source's bindings go to the run pods of the jobs that load it, never to
  the code server.

Changing a stage's endpoint, credential or bucket therefore changes neither
the revision digest nor the image. A source binding is read when a run pod
starts, so editing its ConfigMap or Secret takes effect on the next run
without a redeploy. A provider-contract value reaches the code location
through its Helm release, so changing one means activating the same revision
again. `olf deploy` refuses an activation whose sources the stage has not
bound, and reads Secret key names only. The [provider
contracts](../architecture/provider-contracts.md#where-stage-bindings-reach-a-workload)
document lists each path and its limits.

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
  stage activated before the change stays inspectable. They are not a
  rollback target: the move to Floe 0.8 is one-way, and the platform no
  longer serves the manifests their image needs, so a deploy whose pointer
  fails to commit over one keeps its new release and asks to be rerun.

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

2026-10-28: dbt `profiles.yml` leaves the `dbt` component (#546). The
reference products no longer track a rendered copy, and `olf dbt parse` /
`olf check dbt` render the profile outside the project, so a local parse
cannot change the revision.

2026-10-29: the `descriptors` component freezes `governance.yml` in place of
`lakehouse.yaml` (#545). Domains and products are the only business metadata
the file carries; sources and dashboards are found from their directories.

2026-10-29: dbt, dlt and the code location bind on the pod (#546). The
image carries one environment-variable-only dbt profile and a stage-neutral
manifest that every run re-parses. dlt sources declare their runtime keys in
`source.yaml` and receive per-stage ConfigMap and Secret bindings on their
jobs' run pods. dlt state is scoped to `<stage>__<source>` in the stage's
Bronze bucket, and the code location receives an explicit allowlist of
contract values instead of every `OPENLAKEFORGE_*` key.

New record. No prior ADR modeled a promotable project revision distinct from
the v0.2 Floe runtime-artifact revision; ADR 0009's distribution-payload
identity and ADR 0011's Deployment Profile are the two existing typed
identities this one composes with, not supersedes.
