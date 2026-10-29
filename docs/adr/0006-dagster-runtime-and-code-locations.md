# ADR 0006: Dagster runtime and code locations

## Status

Binding.

## Context

Dagster needs to load user code to build the asset graph, and to execute that
code in isolated runs. How user code reaches the Dagster pods determines the
runtime footprint, the deploy loop, and how much of the platform a small team
has to understand.

## Decision

### Platform control plane and project-code runtime

A project image carries everything Dagster needs to load and run user code:
Dagster definitions, the `dagster-floe` and `dagster-dbt`
connectors, the Floe Python core (`floe-python`), dlt pipelines, the dbt
projects, the Floe project, the `openlakeforge-domain-model` package, and the
shared `libs/`.

The Terraform-owned Dagster control plane owns the webserver, daemon, database,
RBAC, and a stable external gRPC workspace. It does not own a project image
or code location. `olf project deploy` owns the separate
`openlakeforge-project` user-deployment Helm release, which points the stable
workspace at its stage's verified image and revisions. A platform apply can
therefore reconcile infrastructure without resetting active user code.

The control plane runs a digest-pinned upstream Dagster image from
`release/component-catalog.yaml`: neither the webserver nor the daemon imports
`lakehouse_code`, so pointing them at a project image made a platform apply
depend on a project build having happened first. The K8sRunLauncher is given no
image at all, so Dagster launches each run with its code location's own
container image -- the digest that stage activation set. A value configured at
platform-apply time would otherwise outrank every later promotion.

Anything that does need project code rides the activation release instead. The
compute-log archiver (`libs.k8s_log_archive`) is rendered into it through the
user-deployments subchart's `extraManifests`, which also keeps it on the digest
its stage actually runs.

### Floe runs one Kubernetes Job per domain, from the project's own contracts

Floe 0.8 is a Python library, and the project-code image installs it. The code
location builds the Floe manifest in memory from the Floe project under
`lakehouse_code/silver/` and an EnvironmentProfile it renders for its own
stage (`libs/floe_profile.py`). No manifest is generated, published or
versioned, and there is no separate Floe runner image.

The profile's runner is `kubernetes_job` with `job_scope: domain`: dagster-floe
builds one subsettable asset per domain, and a run that materializes it
launches one Kubernetes Job per domain in the stage namespace, under the
`dagster` service account the run launcher already uses. The Job runs on the
same project-code image digest as the run pod, through the `olf-floe-job`
entrypoint (`libs/floe_job.py`), and validates the selected entities one after
another (`sequential` orchestration). Only the entities the run selected are
passed to it.

The profile is rendered on the pod rather than at activation because it is
the only stage-bound Floe input: rendering it from the contract values the
activation release already sets keeps one image digest valid for every stage.
The Job re-renders the same profile from the non-secret contract values
forwarded in its environment; credentials reach it only as Secret references,
so no credential value enters the profile, the manifest or the Job spec.

The trade-off is a pod per domain per run against isolation: Floe does not
share the run pod's memory or mounted credentials, and each domain's Job holds
only the Secrets its runner names. [Floe
validation](../architecture/floe-validation.md) describes the layout and the
catalogs Floe 0.8 supports.

### dbt reads its stage from the pod environment

The image holds one dbt profile, rendered at build from
`libs/dbt/profiles/trino.yml` into `generated/dbt-profiles/`. Every
connection value in it (host, port, TLS, user, catalog, schema prefix) is an
`env_var()` reference with no stage default. The build also parses each
product's manifest once, with placeholder values
(`libs/dbt/render_profiles.py`). Dagster reads that manifest for the asset
graph. Its asset keys come from domain, product and node names, never from a
catalog or schema, so the graph is the same in every stage.

Each `dbt build` gets its own target path from dagster-dbt and re-parses with
the run pod's environment. The image ships no partial-parse state, so the SQL
a run executes, its lineage and the run artifacts it uploads name that
stage's relations. A stage that leaves a required variable unset fails the
run. It cannot fall back to another stage's catalog. The cost is a full dbt
parse per run, which is seconds per product.

### One merged code location by default

The deployed configuration is a single location named `openlakeforge-dagster`,
loading `lakehouse_code.definitions`, which aggregates every product's Dagster
definitions from `lakehouse_code/pipelines/dagster/`.

An earlier design deployed one location per domain. Every location used the same
image and image tag, so they were always rebuilt and redeployed together: the
split cost one always-on pod per domain and delivered none of the independent
release cadence or dependency isolation that would justify it.

`code_locations` remains a list in the orchestration contract. A deployment that
genuinely builds domains separately, or that wants load and restart failures
isolated to one pod, can configure the split explicitly — the trade-off is a
merged location's blast radius (one bad module fails the whole graph) against a
pod per domain.

### The contract owns the code-location set

The set has two consumers with no shared code between them: Terraform renders
the webserver's `workspace.yaml` from it, and `olf project deploy` renders one
user deployment per entry. `dagster-user-deployments` names each Service after
its deployment, so a location name is also its in-cluster host — the two sides
must produce the same names or the webserver points at Services that do not
exist, and the stage comes up with no code server reachable and no error at
apply time.

The provider contract is therefore the single owner:
`stages.<name>.orchestration.code_locations` carries `{name,
definitions_module}` per entry, emitted by every environment root from the same
Terraform local it passes to the Dagster module, and read back by `olf` through
`olf.provider_contracts`. Neither consumer holds its own copy.

### Kubernetes run launcher

Runs execute in isolated pods using the project-code image, so a run's
dependencies and failures are contained.

Locally the image is loaded into kind with `imagePullPolicy: Never`, which means
Kubernetes never pulls a new image on pod creation. The artifacts phase
therefore performs an explicit coordinated rollout
(`olf k8s set-project-code-image`) after loading the image; without it, running
pods keep serving the previous build.

## Consequences

The v0.3 promotion loop is `olf project build` followed by `olf project deploy`
for each stage. The deprecated `olf deploy --phase artifacts` remains only for
single-DEV compatibility.

Durable product jobs are named `<product>_pipeline` and derived from the
descriptor (ADR 0005), so adding a product adds a job with no platform edit.

## History

Merges the decisions previously recorded as ADR 0003 (the local Dagster
project-code runtime), 0004 (manifest-first Floe ingestion, generalized from the
Sales seed product), 0014's code-location half, and 0019 (the merged code
location default, which superseded ADR 0014's per-domain split).

2026-09-03: Moved project user-deployment ownership from the Terraform Dagster
release to stage activation (#115); the control plane keeps a stable external
workspace endpoint. The control plane and run launcher stopped referencing the
project-code image in the same change, and the compute-log archiver moved into
the activation release.

2026-09-09: Recorded the provider contract as the owner of the code-location
set (#185). The #115 split left Terraform and activation deciding it
independently, agreeing only because both repeated the module variable's
default; a rename or a configured split broke the stage silently. The shipped
default — one merged location — is unchanged.

2026-10-28: Floe moved into the project-code image (#533). Floe 0.8's Python
core lets dagster-floe build the manifest in memory, so the separate runner
image and the `s3://` manifests published at activation are gone. Floe still
runs in Kubernetes Jobs, now one per domain on the project-code image rather
than one per entity.

2026-10-28: dbt reads its stage from the pod environment (#546). The
provider-specific profiles (`local`, `aws`, `azure`) and the image's
`DBT_PROFILE_ENV` choice are replaced by one profile of `env_var()`
references, and the manifest baked at build is parsed with placeholders
instead of the local DEV catalog.
