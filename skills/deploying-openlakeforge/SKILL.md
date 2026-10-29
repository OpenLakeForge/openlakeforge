---
name: deploying-openlakeforge
description: >-
  Install the OpenLakeForge `olf` CLI, create a project, and deploy, inspect,
  reach, troubleshoot or tear down an OpenLakeForge lakehouse on local kind,
  AWS or Azure. Use when the user mentions OpenLakeForge, `olf`,
  `openlakeforge.yaml`, `olf deploy`/`destroy`/`status`/`doctor`/`forward`,
  presets (slim, full), stages (dev, uat, prod), or a failing OpenLakeForge
  deployment.
license: Apache-2.0
---

# Deploying OpenLakeForge

OpenLakeForge is a self-hosted lakehouse on Kubernetes: dlt loads CSV into
Bronze, Floe validates it into Silver Iceberg tables, dbt-trino builds Gold
marts, Dagster orchestrates, Trino queries. `olf` is the only interface: it
runs Terraform, Helm, kubectl and kind itself, from pinned copies it downloads
under `~/.openlakeforge` (`OLF_HOME`).

## Rules for agents

- **One `olf` command at a time.** Concurrent invocations are not supported.
- **Ask the person before** `olf destroy` (it deletes every table and run
  history), `--force`, `--allow-stage-removal`, or any deploy with
  `--provider aws` or `--provider azure` (it creates billed resources).
- **Never edit** anything under `~/.openlakeforge`: it holds Terraform state
  and kubeconfigs. Change `openlakeforge.yaml` or `lakehouse_code/` and
  deploy again.
- **Commands that wait on a person or never exit:** `olf forward` runs until
  Ctrl-C; `olf github connect` and `olf auth login` open a browser. Run them
  only when the person asks, and tell them what to do in the browser. With
  `spec.identity.issuer: github`, pass `--no-github-connect` to `olf deploy`
  so it fails instead of waiting for a browser.
- **Read state as JSON**, not from the table output: `olf status --json`,
  `olf profile resolve --project . --json`, `olf version --json`.
- Deploying again is safe: every phase reconciles what already exists. It is
  the first fix to try after a failure.

In a source checkout of the `openlakeforge` repository, run every command
through `uv run --project tools/olf --locked olf ...` from the repository root
instead of an installed `olf`.

## Prerequisites

| Need | Check |
| --- | --- |
| A running Docker engine | `docker ps` |
| uv, or Python 3.12 | `uv --version` |
| Ports 80 and 443 free on `127.0.0.1` (local) | `lsof -nP -iTCP:80 -sTCP:LISTEN` prints nothing |
| 4 CPUs and 16 GB of memory for Docker | `docker info` |

Install, pinning the version the project uses (see its releases):

```bash
uv tool install "openlakeforge==<version>" --python 3.12
olf version
```

## Create a project

```bash
mkdir my-lakehouse && cd my-lakehouse
olf init
```

This writes `openlakeforge.yaml` (the Deployment Profile) and `lakehouse_code/`
with three demo data products. `olf init` refuses to overwrite either.

| Option | Effect |
| --- | --- |
| `--preset full` | Adds OpenMetadata and Superset to the Slim data path |
| `--stages dev,prod` | Enables more stages: `dev`, `dev,prod` or `dev,uat,prod` |
| `--empty` | No demo products; the project cannot deploy until it has a source and a product |
| `--profile-only` | Writes only `openlakeforge.yaml` into an existing project |

Validate the profile without deploying:

```bash
olf profile validate --project .
```

Profile fields an agent most often changes, all under `spec`:

| Field | Values |
| --- | --- |
| `provider.type` | `local`, `aws`, `azure` |
| `preset` | `slim` or `full`; change it and deploy again, it applies in place |
| `stages` | `dev`, `uat`, `prod`, each `{enabled: true}`; a stage left out is disabled |
| `access.issuer` | `none` for plain HTTP; absent means HTTPS from a local CA |
| `identity` | absent means sign-in off; `issuer: github` needs a GitHub App |

## Deploy

```bash
olf doctor --provider local --phase foundation
olf deploy --provider local
```

`olf doctor` deploys nothing (it may download the managed toolchain) and
prints `PASS` or `FAIL` per check. Before the first deploy, check only
`--phase foundation`: the other phases check platform state that a first
deploy creates, so they fail until then. The first deploy takes several minutes. `olf deploy` runs these phases in order, and
`--phase` runs one:

| Phase | Does | Run it alone when |
| --- | --- | --- |
| `foundation` | Creates the kind cluster `openlakeforge-local` | The cluster was deleted |
| `prefetch` | Pulls the large images into the kind nodes (local only) | An image pull failed |
| `platform` | Shared services in `olf-system`, one `olf-<stage>` namespace per stage | The preset or stages changed |
| `artifacts` | Builds the project-code image from `lakehouse_code/` and deploys it | Only code under `lakehouse_code/` changed |

A change to `lakehouse_code/` needs only:

```bash
olf deploy --provider local --phase artifacts
```

To preview a foundation or platform change, `olf plan --provider local --phase platform`
prints the Terraform plan; `--detailed-exitcode` returns 2 when changes are
pending.

## Inspect and reach

```bash
olf status --provider local --json
```

It lists service URLs, then pods, services and PVCs per namespace. A healthy
deployment has every pod `Running` or `Completed` and every PVC `Bound`.
`--stage prod` selects another stage.

With the default local profile:

| Service | URL |
| --- | --- |
| Portal | `http://olf.localhost` |
| Dagster | `http://dagster-dev.olf.localhost` |
| Trino | `http://trino.olf.localhost` |
| Superset (Full) | `http://superset-dev.olf.localhost` |
| OpenMetadata (Full) | `http://openmetadata.olf.localhost` |

Browsers resolve `*.localhost`; `curl` needs
`--resolve dagster-dev.olf.localhost:80:127.0.0.1`. Polaris and SeaweedFS
have no route: `olf forward --provider local` port-forwards every service to
`localhost` (Dagster 3000, Trino 8080, Polaris 8181, SeaweedFS S3 9000) until
stopped.

`olf` keeps its own kubeconfig: `$OLF_HOME/state/local/kubeconfig.yaml` for an
installed `olf`, and `.tmp/kubeconfigs/local.yaml` under the repository root
in a source checkout. To use kubectl with an installed `olf`:

```bash
export KUBECONFIG="${OLF_HOME:-$HOME/.openlakeforge}/state/local/kubeconfig.yaml"
KUBECTL="$(olf toolchain path kubectl)"
"$KUBECTL" --context kind-openlakeforge-local get pods -A
```

## Verify end to end

```bash
olf e2e run --env local --suite full
```

It launches every product's Dagster job, checks the Silver and Gold tables
through Trino (and Superset and OpenMetadata on Full), and ends with
`Local OpenLakeForge full e2e validation passed.` `--stage prod` checks
another stage. Both suites run pipelines: on local, `--suite smoke` checks the
catalog, then launches one product's job and checks its tables. Neither is a
read-only health check; for that, use `olf status`.

## Tear down (ask first)

| To remove | Run |
| --- | --- |
| Everything, including all data | `olf destroy --provider local` |
| Only the platform, keeping the cluster | `olf destroy --provider local --phase platform` |
| The downloaded toolchain | `olf toolchain clean --all` |

`openlakeforge.yaml` and `lakehouse_code/` are never touched; the next
`olf deploy --provider local` rebuilds the same platform from them, with empty
tables.

## Cloud

AWS and Azure use the same commands with `--provider aws` or
`--provider azure`, after the person authenticates with
`olf auth login --provider aws` (or `azure`) in a browser. Check it with
`olf auth status --provider aws`. On AWS there is no ingress: services are
reached only through `olf forward --provider aws`, and `olf query` is not
supported. Follow the provider guide before a first cloud deploy:
[AWS](https://github.com/OpenLakeForge/openlakeforge/blob/main/docs/guides/cloud/deploy-to-aws.md),
[Azure](https://github.com/OpenLakeForge/openlakeforge/blob/main/docs/guides/cloud/deploy-to-azure.md).

## When something fails

Run `olf deploy` again once. If it fails the same way, read
[references/troubleshooting.md](references/troubleshooting.md) and match the
error text.
