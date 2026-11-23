# ADR 0006: Dagster runtime and code locations

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS and Azure: proof of concept (ADR 0010) |

## Context

Dagster loads user code to build the asset graph and executes that code in
isolated runs. How user code reaches the Dagster pods decides the runtime
footprint, the deploy loop, and how much of the platform a small team has to
understand. The platform also promotes one built project unchanged from stage
to stage (ADR 0012), so nothing the Dagster runtime loads can be bound to a
stage at build time, and a platform apply must not depend on, or reset, the
user code a stage is running.

## Decision

**D1. One project-code image carries all user code, and stage activation, not
the platform, deploys it.** The image holds the Dagster definitions, the
`dagster-floe` and `dagster-dbt` connectors, Floe's Python core
(`floe-python`), the dlt pipelines, the dbt and Floe projects,
`openlakeforge-core` and `openlakeforge_runtime/`. The Terraform-owned Dagster
control plane owns the webserver, daemon, database, RBAC and a stable external
gRPC workspace, and no project image or code location. `olf infra project
deploy` owns the `openlakeforge-project` user-deployment Helm release, which
points that workspace at the stage's verified image, so a platform apply
reconciles infrastructure without resetting active user code. Enforced by:
`modules/orchestration/dagster` and `tools/olf/olf/infra/deployment/activation.py`.

**D2. The control plane runs the upstream Dagster image, and the run launcher
names no image.** The webserver and daemon run a digest-pinned upstream image
from `release/component-catalog.yaml`, since neither imports `lakehouse_code`.
The `K8sRunLauncher` is given no `job_image` on any entry point, `olf infra
deploy`'s artifacts phase included, so each run starts on its code location's
own image: the digest stage activation set. A value fixed at platform apply
would name a build no stage activated. Enforced by:
`infra/terraform/modules/orchestration/dagster`, which renders no `job_image`.

**D3. Activation ends only when every code location reports the activated
digest.** The webserver caches each location's snapshot, and a launched run
takes its image from that snapshot, not from the code server. After the
rollout, activation reloads every contracted location through the stage
webserver's GraphQL (`reloadRepositoryLocation`) and waits, for a bounded
time, until each reports the activated digest. A webserver that does not is an
activation failure, handled like a pointer that will not commit. The daemon
refreshes its own workspace every minute. Enforced by: the reload in
`packages/lakehouse-services/openlakeforge_services/dagster.py`, called by
activation.

**D4. Anything else that needs project code rides the activation release.**
It then runs on the digest its stage actually runs. The pod-log archiver
(`openlakeforge_runtime.k8s_log_archive`), where ADR 0022 still renders it, is
in that release through the user-deployments subchart's `extraManifests`.
Enforced by: `_user_values` in `tools/olf/olf/infra/deployment/activation.py`.

**D5. Floe runs as one Kubernetes Job per domain, on the project-code image,
from a profile rendered on the pod.** The code location builds the Floe
manifest in memory from the Floe project and an EnvironmentProfile it renders
for its own stage; no manifest is generated, published or versioned, and there
is no Floe runner image. The profile's runner is `kubernetes_job` with
`job_scope: domain`: a run launches one Job per domain in the stage namespace,
under the run launcher's `dagster` service account, on the run pod's image
digest, running `floe run` on the selected entities in `sequential` order.
dagster-floe hands the Job the profile text (`-p env:FLOE_PROFILE_CONTENT`);
credentials reach it only as Secret references the profile names as `${...}`.
Enforced by: `packages/runtime/openlakeforge_runtime/floe_profile.py`.

**D6. dbt reads its stage from the run pod's environment and re-parses every
run.** The image holds one dbt profile whose every connection value is an
`env_var()` with no stage default, and a Gold manifest parsed once at build
with placeholder values, which Dagster reads for the asset graph. Asset keys
come from domain, product and node names, never a catalog or schema, so the
graph is the same in every stage. Each `dbt build` gets its own target path and
re-parses with the pod's environment; the image ships no partial-parse state.
A stage that leaves a required variable unset fails the run rather than reach
another stage. The Gold project reaches `openlakeforge_dbt` through
`OPENLAKEFORGE_DBT_PACKAGES_DIR`, never a path relative to the project, so a
customer repository resolves it too. Enforced by:
`packages/runtime/openlakeforge_runtime/dbt/profiles/trino.yml` and
`render_profiles.py`.

**D7. One merged code location by default.** The deployed configuration is
one location, `openlakeforge-dagster`, loading
`lakehouse_code.pipelines.definitions`, which builds the Bronze, Silver and
maintenance definitions and one `<product>_pipeline` job per product in
`governance.yml`. `code_locations` stays a list: a deployment can configure a
split, trading a merged location's blast radius (one bad module fails the
whole graph) for a pod per location. Enforced by:
`openlakeforge_runtime.code_location.build_code_location` and the
`code_locations` local of each environment root.

**D8. The provider contract owns the code-location set.** Terraform renders the
webserver's `workspace.yaml` from it and `olf infra project deploy` renders
one user deployment per entry; each Service is named after its deployment, so
both must produce the same names or the webserver points at Services that do
not exist, with no error at apply time. `stages.<name>.orchestration.code_locations`
carries `{name, definitions_module}` per entry, emitted by every root from the
local it passes to the Dagster module and read back by `olf`. Neither consumer
holds its own copy. Enforced by: `olf.common.provider_contracts`.

**D9. A full refresh is a property of one run, carried by a run tag.** The tag
`openlakeforge/full_refresh` (`bronze` or `gold`) is the contract between
whoever launches the run (the Dagster launchpad, `olf infra product run
--full-refresh`) and the runtime, and reaches every asset of the job without a
config schema. Both scopes run `dbt build --full-refresh`. `bronze` also
carries dagster-floe's `floe/full_refresh=true`, so Floe rebuilds Silver from
every Bronze file; the runtime rejects a `bronze` run without it at its first
step. A full refresh re-ingests Bronze and never re-extracts: cursors and
files stay. Enforced by: `packages/core/openlakeforge_core/runs.py`.

**D10. Runs execute in isolated pods on the project-code image, with default
resources.** Every run pod requests 250m CPU and 512Mi memory, with a 2Gi
memory limit and no CPU limit; the dlt extract and dbt run inside it, so this
sizes them too. The values are a safe Slim default, not a measurement: a run
that outgrows the limit is OOMKilled. Floe Jobs are sized by their own runner.
Enforced by: `run_pod_resources` in
`infra/terraform/modules/orchestration/dagster/variables.tf`.

## Consequences

- The promotion loop is `olf infra project build`, then `olf infra project
  deploy` per stage; `olf infra deploy --phase artifacts` builds the same
  revision and activates it on its one DEV stage.
- Adding a product adds a job with no platform edit (ADR 0005).
- Floe costs a pod per domain per run, in exchange for isolation: a Job shares
  neither the run pod's memory nor its credentials, and holds only the Secrets
  its runner names. Supported layouts and catalogs: [Floe
  validation](../architecture/floe-validation.md).
- Every run re-parses the whole Gold project, which takes seconds.
- A locally built, never-pushed image is loaded into kind and runs with
  `imagePullPolicy: Never`: [project revisions](../architecture/project-revisions.md#stage-activation).

## Boundaries

| Not decided here | Where |
| --- | --- |
| What a revision freezes, and how stages bind on the pod | ADR 0012 |
| The `lakehouse_code/` layout and dbt product selection | ADR 0004 |
| The lifecycle phases a platform apply and an activation belong to | ADR 0002 |
| Pod log collection and the archiver's future | ADR 0022 |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| One code location per domain | Every location used the same image and redeployed together: a pod per domain with no independent release or dependency isolation |
| Control plane on the project-code image | A platform apply would depend on a project build having happened |
| A `job_image` rendered at platform apply | It names a build no stage activated; a restarted webserver kept launching the previous revision (#740) |
| User deployments owned by the Terraform Dagster release | A platform apply reset active user code |
| A separate Floe runner image and per-stage published manifests | Stage-bound output inside a promotable revision; Floe 0.8 builds the manifest in memory |
| Per-provider dbt profiles chosen by `DBT_PROFILE_ENV` | One image digest could not serve every stage |
| Re-extracting incremental Bronze on a `bronze` refresh | Replaced a source's history with whatever its connector could still fetch |

## History

- Undated: New record, merging the former ADRs 0003 (local project-code
  runtime), 0004 (manifest-first Floe ingestion), 0014's code-location half
  and 0019 (the merged code location, which superseded 0014's per-domain
  split).
- 2026-09-03 (#115): D1, D2 and D4 added: activation owns the user deployment;
  the control plane and run launcher stop naming the project image.
- 2026-09-09 (#185): D8 added: the contract owns the code-location set.
- 2026-10-28 (#533): D5 changed: Floe runs in the project-code image, one Job
  per domain instead of one per entity on a runner image.
- 2026-10-28 (#546): D6 changed: one `env_var()` profile replaces the
  per-provider profiles.
- 2026-10-30 (#342, #344): D9 added: a run tag asks one run for a full refresh.
- 2026-11-01 (#740): D3 added: activation reloads every code location.
- 2026-11-01 (#627): D6 changed: the dbt package is found through
  `OPENLAKEFORGE_DBT_PACKAGES_DIR`.
- 2026-11-02 (#342): D9 changed: a `bronze` refresh no longer re-extracts.
- 2026-11-02 (#325): D10 changed: run pods get default requests and limits.
- 2026-11-04 (#558): D2 changed: `olf infra deploy`'s artifacts phase
  activates a revision too, so no entry point renders `job_image`.
- 2026-11-20 (#1339): D7 changed: the entry point is
  `lakehouse_code.pipelines.definitions`.
- 2026-11-23: Rewritten in the standard form; no decision changed.
