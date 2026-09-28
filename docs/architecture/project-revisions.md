# Project Revisions

A project revision is a frozen, content-addressed identity for one complete
promotable data project: descriptors, Floe contracts, dbt, Dagster
orchestration code, report assets when present, and the project-code image
digest. `olf project build` computes and publishes it; `olf project revision
inspect|verify` read it back without rebuilding source. See
[ADR 0012](../adr/0012-project-revisions-and-promotion.md) for the decision
this implements, and [`#154`](https://github.com/OpenLakeForge/openlakeforge/issues/154)
for the issue that introduced it.

This is a different object from `olf floe revision`, the v0.2 immutable
Floe **runtime-artifact** revision. That one identifies one stage's rendered
manifest set after a provider contract has resolved it. A project revision
identifies the project **before** any stage exists.

## Build once, promote unchanged

| Concept | Answers |
| --- | --- |
| Project revision | What does this project contain, and what image runs it? |
| Deployment Profile (`openlakeforge.yaml`) | Where does it run, and which stages are enabled? |
| Floe runtime-artifact revision | What did Floe render for one stage's contract environment? |

Promotion (`#115`) activates one already-built project revision in DEV,
validates it, then activates the same revision — same digest, no rebuild — in
optional UAT and then PROD. A project revision that changed meaning between
stages would defeat that: enabling UAT, or moving a project to another
provider, must never invalidate a revision that already exists.

## Stage activation

```bash
olf platform apply -f openlakeforge.yaml
olf project image  -f openlakeforge.yaml
olf project build  --project . --image <repository>@sha256:<digest>
olf project deploy -f openlakeforge.yaml --stage dev --revision sha256:<digest>
olf project deploy -f openlakeforge.yaml --stage prod --revision sha256:<digest>
olf project status -f openlakeforge.yaml --json
```

`olf project image` builds and pushes the project-code image and prints the
digest-pinned reference `olf project build` requires.

A revision is identified partly by that image, so the reference has to mean the
same thing from every stage -- which makes a **registry** part of the contract,
not an optional convenience. A registry digest is the only pullable identity an
image has; a local image's config Id is not one. Cloud providers use the
foundation's own ECR/ACR. Local has no registry of its own and deliberately
does not stand one up: it pushes to whatever `PROJECT_CODE_IMAGE_REPOSITORY`
names, GHCR or Docker Hub by default. See
[docs/setup/local.md](../setup/local.md) for that loop.

Activation verifies every object of the published `ProjectRevision`, restores it
to a temporary root, and renders Floe only from that root and the selected v3
provider contract. It writes an immutable `ProjectActivation` beneath
`activations/<stage>/revisions/sha256/` and advances `ACTIVE.json` only after
the stage's `openlakeforge-project` Helm release is ready. Project revisions
remain equal across stages; activation and Floe revisions intentionally differ.

## Schedules are not a promotion side effect

The same revision is deployed to every stage, so nothing about a schedule can
be decided at build time. `libs.product_dagster.build_stage_schedules` decides
it at load time from `OPENLAKEFORGE_STAGE`, which activation puts on the
code-server container:
`DeploymentContext.command_env` sets it, `applied_contract_environment` carries
it into the contract environment, and `_user_values` forwards every
`OPENLAKEFORGE_*` key onto the deployment.

| Stage the pod reports | Schedules in the code location |
| --- | --- |
| `prod` | One `<product>_daily` per product, every one `STOPPED` |
| `dev`, `uat` | None |
| Unset or unrecognised | None |

Two properties matter, and `olf check project-code` fails the build if either
stops holding. DEV and UAT define no schedule at all, so the production-shaped
daily job is not present to be switched on by mistake against the DEV catalog.
The PROD schedules default to `STOPPED`, so deploying or promoting a revision
never starts a run — enabling one is a deliberate act in the Dagster UI, and
Dagster keeps that choice in the stage's own metadata database, which is not
part of the revision.

Asset keys are identical in every stage regardless: the stage is a runtime and
catalog boundary, never part of an asset's identity.

## What is frozen

| Component | Source | Notes |
| --- | --- | --- |
| `descriptors` | `lakehouse_code/lakehouse.yaml`, `bronze/<source>/source.yaml` | Provider-neutral project truth |
| `floe` | `silver/<domain>/contracts/floe/<domain>.yml` | The contract, never the rendered manifest |
| `dbt` | `gold/<product>/dbt/**` | Excludes `target/` and `dbt_packages/` |
| `dagster` | `pipelines/dagster/<product>.py`, `bronze/<source>/dlt/<source>.py` | Orchestration and extract code |
| `reports` | `dashboards/superset/<dashboard>/**` | Only when at least one dashboard is declared; validated first |
| `image` | The project-code repository, plus an immutable `@sha256:` digest | A mutable tag is rejected |
| `distribution` | The running distribution's version | Gates activation against an incompatible distribution |

## The report bundle contract

A Superset report bundle is the one component promotion never re-derives:
PROD imports what DEV exported, and nothing exports from PROD to repair it.
`olf project build` therefore refuses a bundle that could not survive that
trip, and `olf report validate` runs the same rules on demand:

- **`metadata.yaml` declares `type: assets` and a format version.** That type
  is what selects the import command the deploy path runs; anything else
  fails in the pod instead of here.
- **The bundle exports at least one dashboard.** A scaffold from `olf product
  new --with-report` carries a database and its datasets but no dashboard
  until an analyst authors one in Superset and exports it back, and a
  revision frozen in that state fails only once a stage imports it.
- **Every database, dataset, chart, and dashboard carries a real `uuid`.**
  Superset matches assets across instances by UUID, so an asset without one
  is recreated rather than updated in each stage, and an arbitrary string
  only fails once a stage tries to import it.
- **No chart, dataset, or dashboard UUID appears twice across the declared
  bundles.** Every bundle is imported into the same stage's Superset, so a
  reused identity makes the second import overwrite the first asset. Database
  UUIDs are the exception and are meant to be shared: one Trino connection
  serves every dashboard — but the bundles sharing one must define it
  identically, apart from the `sqlalchemy_uri` that is rewritten at import.
- **Every reference resolves inside the bundle** — a dataset's
  `database_uuid`, a chart's `dataset_uuid`, and each `CHART` position UUID
  in a dashboard. A dangling one imports into DEV and breaks in PROD.
- **No `ws_<user>_` identifier.** A personal workspace is not a promotable
  dependency (#111).
- **No `lakehouse_<stage>` or `olf-<stage>` name.** Target connectivity is
  resolved at import time, so a checked-in stage name would survive
  promotion and point PROD at another stage.

`s3://` paths, `http://` endpoints, and credential literals are rejected for
every component, reports included, by the revision's own scan.

## What is never frozen

- `openlakeforge.yaml` — deployment intent (provider, stages, capabilities),
  not project content.
- Every stage-rendered Floe artifact: generated manifests, rendered profiles,
  and anything a provider contract resolved. These embed physical bucket
  names, Kubernetes namespaces, catalog endpoints, and Secret references —
  the opposite of what a promotable revision is allowed to contain. `#115`
  regenerates them per stage from the frozen `floe` component.
- Build output: `target/`, `dbt_packages/`, `__pycache__/`, `.venv/`.

## Building and verifying

```bash
olf project build --project . --image ghcr.io/example/project-code@sha256:<digest>
```

Refuses to build from an invalid project — `olf project build` runs the same
checks as `olf project validate` first. The image reference must already
carry a digest, or resolve to one through the image's registry `RepoDigest`
(so it has been pushed to, or pulled from, a registry); a bare mutable tag
and an unpushed local-only image are both rejected, because a revision that
could point at a moving tag, or at a digest nothing can pull, is not
actually immutable.

```bash
olf project revision verify --revision sha256:<digest>
```

Re-derives every component's digest from the published objects and compares
it against what the manifest declares — tampering with one published file, or
a partial publish that never finished, both fail closed. `verify` also checks
the manifest's recorded distribution version against the running
distribution, refusing to activate a revision built against an incompatible
one.

## Content address

Each component is a `{relative project path: sha256 digest}` map. The overall
revision hashes every `<component>/<path>` entry, sorted, through the same
primitive `olf floe revision` uses
(`revision.aggregate_revision`) — a project with no components cannot produce
a revision, and the hash is stable regardless of file discovery order or
which path on disk the project was built from.

## Publishing

`olf project build` publishes under `project/revisions/sha256/<digest>/`,
either to the ops bucket (`--via port-forward` for in-cluster S3-compatible
storage, `--via direct` for cloud S3) or to a local directory with `--output
DIR`. Every published object is immutable: republishing the same revision is
a no-op, and publishing different content under an already-published key is a
hard error rather than a silent overwrite.
