# Local installation

This guide explains how to deploy OpenLakeForge on your machine using a local Kubernetes cluster.

The local environment is the easiest way to evaluate OpenLakeForge, develop data products, and work on the platform itself.

OpenLakeForge creates an isolated [`kind`](https://kind.sigs.k8s.io/) cluster backed by your existing Docker engine. It does **not** modify your global Kubernetes context.

## What gets deployed?

The preset is a field of the Deployment Profile at `openlakeforge.yaml`
(`spec.preset`), so the commands below take no preset flag — see
[ADR 0011](../adr/0011-deployment-profile-and-stages.md) and
`olf profile validate`/`resolve`. Switching preset means editing that one
field and redeploying.

`--profile slim|full` still works as the deprecated v0.2 shorthand, but it
resolves a topology named `legacy` that no `olf e2e run -f` can name, so a
deployment made that way cannot be validated with the commands below.

The local environment is available in two presets:

| Profile  | Includes                                                                    | Recommended for                       |
| -------- | ---------------------------------------------------------------------------- | ------------------------------------- |
| **Slim** | Ingestion, Floe validation, Apache Iceberg, Polaris, Trino, dbt and Dagster | First evaluation and data engineering |
| **Full** | Slim + OpenMetadata + Superset                                              | Governance, lineage and dashboards    |

Both profiles use the same ingestion-to-Gold data path.

The Slim profile only disables the optional governance and analytics layers:

```text
Sources
   ↓
dlt
   ↓
Bronze
   ↓
Floe
   ↓
Silver Iceberg
   ↓
dbt-trino
   ↓
Gold Iceberg
   ↓
Trino
```

Dagster orchestrates the complete pipeline.

---

## Requirements

You need a Docker-compatible engine and Python 3.12 or later. Git, uv, Make,
Terraform, Helm, kubectl, and kind are not host prerequisites.

Make sure Docker is available from your shell:

```bash
docker ps
```

If this command fails, fix your Docker installation before continuing.

---

## Install and deploy

Create a project directory, install the release, and initialize its writable user code:

```bash
mkdir my-lakehouse
cd my-lakehouse

pip install openlakeforge
olf init
```

`olf init` verifies the packaged platform payload, installs or reuses the
release-pinned Terraform, Helm, kubectl, and kind toolchain under
`~/.openlakeforge`, checks Docker, and creates the writable project files
`openlakeforge.yaml` and `lakehouse_code/` from the demo. Validate the
resulting project boundary with `olf project validate --project .`. Set
`OLF_TOOLCHAIN_MODE=host` to use your own host-installed Terraform, Helm,
kubectl, and kind instead of the managed toolchain.

`olf init` writes a DEV-only Slim `openlakeforge.yaml` named after the
project directory; `--stages dev,prod` (or `dev,uat,prod`), `--preset full`
and `--name` choose others, and `olf init --profile-only` adds just the
profile to an existing project, such as one created by 0.2.

`olf init --empty` creates a transitional project with no source, domain, or
product. Scaffold its first source and product before deploying:

```bash
olf source new crm --resource accounts
olf product new sales/accounts_report --input crm/accounts --gold-table mart_accounts
```

The empty descriptor deliberately does not pass strict descriptor validation
until that first product exists.

---

# Deploy the Slim profile

For a first installation, **Slim is recommended**.

It keeps the complete data-engineering path while leaving OpenMetadata and Superset out of the deployment.

`olf init` writes a Slim profile, so this needs no preset flag:

```bash
olf deploy --provider local
```

This command performs the complete local deployment. It will:

1. Create the local kind Kubernetes cluster.
2. Pre-fetch the large runtime images used by the platform.
3. Deploy the Slim OpenLakeForge platform.
4. Build the OpenLakeForge project-code image.
5. Load the project-code image into kind.
6. Generate and deploy runtime artifacts.
7. Register the included data products with Dagster.

The initial installation can take several minutes because Kubernetes and application container images need to be downloaded.

---

## Check the deployment

Once installation finishes, inspect the platform:

```bash
olf status --provider local
```

This shows:

* Kubernetes pods
* services
* persistent volume claims

You can also inspect Kubernetes directly using OpenLakeForge's isolated kubeconfig. The
path depends on how `olf` was run — a source checkout writes it under the
checkout, an installed (pip) project writes it under `OLF_HOME`:

| Layout | Kubeconfig path |
| --- | --- |
| Checkout (source) | `.tmp/kubeconfigs/local.yaml` |
| Installed (pip) | `~/.openlakeforge/state/local/kubeconfig.yaml` |

```bash
KUBECONFIG=.tmp/kubeconfigs/local.yaml \
kubectl --context kind-openlakeforge-local get pods -n olf-system
```

The local deployment uses:

```text
Cluster:     openlakeforge-local
Context:     kind-openlakeforge-local
Namespaces:  olf-system (shared services) plus olf-<stage> per enabled stage
Kubeconfig:  .tmp/kubeconfigs/local.yaml (checkout) or ~/.openlakeforge/state/local/kubeconfig.yaml (installed)
```

Shared PostgreSQL, SeaweedFS, Polaris, Trino, and OpenMetadata run once in
`olf-system`. Each stage the Deployment Profile enables gets its own
namespace -- `olf-dev`, optional `olf-uat`, `olf-prod` -- holding that
stage's Dagster and, when analytics is enabled, its Superset (ADR 0011).

The generated kubeconfig is intentionally separate from your normal `~/.kube/config`.

---

# Validate the platform

OpenLakeForge includes example data products that can be executed end to end.

For the Slim profile, run:

```bash
olf e2e run --env local --suite full
```

The validation executes the data pipelines and verifies the core path:

```text
Bronze
  ↓
Silver Iceberg
  ↓
Gold Iceberg
  ↓
Trino query
```

Assertions related to OpenMetadata and Superset are skipped because those layers are disabled in Slim.

A successful end-to-end run is the best way to confirm that your installation is working correctly.

---

# Access OpenLakeForge

The local stack publishes its user-facing services through one ingress
(Traefik on `127.0.0.1:80/443`, ADR 0013) at stable HTTPS URLs under
`spec.access.base_domain`, `olf.localhost` by default. `olf status` lists them:

```bash
olf status --provider local          # a "URLs" section, keyed by contract ref
olf status --provider local --json   # {"urls": {...}, "sections": {...}}
```

v0.4 is private evaluation infrastructure: these URLs carry no
authentication of their own. Authentication arrives in v0.5
([#176](https://github.com/OpenLakeForge/openlakeforge/issues/176)); do not
expose them beyond your workstation.

## Trust the local CA once

cert-manager signs every route from a local CA that `olf deploy` creates once;
a re-deploy keeps it. Export it and print the trust commands for your machine:

```bash
olf access trust
```

It writes `openlakeforge-local-ca.crt` under the work directory (`.tmp/` in a
source checkout; `--output` to choose) and prints the commands below for the
detected system. Each needs your own privileges, so `olf` prints rather than
runs them.

| System | Store | Command |
| --- | --- | --- |
| Linux (Debian/Ubuntu) | system | `sudo cp <crt> /usr/local/share/ca-certificates/openlakeforge-local-ca.crt && sudo update-ca-certificates` |
| Linux (Fedora/RHEL) | system | `sudo cp <crt> /etc/pki/ca-trust/source/anchors/openlakeforge-local-ca.crt && sudo update-ca-trust` |
| Linux | Chrome/Chromium (NSS) | `certutil -d sql:$HOME/.pki/nssdb -A -t C,, -n "OpenLakeForge local CA" -i <crt>` |
| macOS | System keychain | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain <crt>` |
| WSL2 | Windows (current user) | `powershell.exe -NoProfile -Command "Import-Certificate -FilePath '$(wslpath -w <crt>)' -CertStoreLocation Cert:\CurrentUser\Root"` |

Firefox keeps its own store: Settings > Privacy & Security > View
Certificates > Authorities > Import.

On WSL2 the browser runs on Windows, so the CA must go into the Windows store;
trusting it inside the Linux distribution only covers tools run there.

`olf` itself needs none of this: its clients read the CA from the cluster and
verify against it.

## Name resolution

Browsers resolve every `*.localhost` name to loopback (RFC 6761), so no
`/etc/hosts` entry is needed. Command-line resolvers often do not (glibc
without systemd-resolved, macOS); `olf`'s own clients pin `*.localhost` to
`127.0.0.1` themselves. For `curl`, pass `--resolve`:

```bash
curl --resolve dagster.dev.olf.localhost:443:127.0.0.1 https://dagster.dev.olf.localhost/server_info
```

## Port-forward fallback

`olf forward --provider local` still starts `kubectl port-forward`s to fixed
localhost ports, and is the only way to reach the services that have no
route (Polaris, SeaweedFS). `olf`'s own clients use the ingress; set
`OLF_PORT_FORWARD=1` to make them port-forward instead, for example while
debugging the ingress:

```bash
OLF_PORT_FORWARD=1 olf e2e run --env local
```

## Core services

### Dagster

```text
https://dagster.dev.olf.localhost
```

Dagster is the main interface for inspecting and running OpenLakeForge data pipelines.

From the Dagster UI you can:

* inspect the asset graph
* view domains and data products
* launch pipelines
* inspect individual materializations
* inspect execution logs

### Trino

```text
https://trino.olf.localhost
```

Trino is the query engine used by OpenLakeForge and by dbt to build Gold models.

### Apache Polaris

Through `olf forward` only:

```text
http://localhost:8181
```

Polaris provides the local Iceberg catalog.

### SeaweedFS

Local object storage is provided by SeaweedFS, through `olf forward` only.

| Interface | Address                 |
| --------- | ----------------------- |
| S3 API    | `http://localhost:9000` |
| Filer UI  | `http://localhost:8888` |
| Master UI | `http://localhost:9333` |

The Filer UI is useful for inspecting the local buckets and data written by OpenLakeForge.

---

# Run the Full profile

The Full profile adds:

* **OpenMetadata** for governance, catalog discovery and lineage
* **Superset** for analytics dashboards

If you currently have a Slim deployment, tear it down first:

```bash
olf destroy --provider local
```

Then set `preset: full` in `openlakeforge.yaml` and deploy:

```bash
olf deploy --provider local
```

Validate it — the same profile file decides the topology for both commands,
so they cannot disagree:

```bash
olf e2e run --env local --suite full
```

The additional services are then available at:

| Service      | URL                                  | Development credentials           |
| ------------ | ------------------------------------ | --------------------------------- |
| OpenMetadata | `https://openmetadata.olf.localhost` | `admin@open-metadata.org / admin` |
| Superset     | `https://superset.dev.olf.localhost` | `admin / admin`                   |

These credentials are intended for the local development environment only.

---

# What `olf deploy` actually does

The local deployment is divided into three logical phases.

```text
Foundation
    ↓
Platform
    ↓
Artifacts
```

The top-level command:

```bash
olf deploy --provider local
```

is equivalent to running each phase in order with `--phase`:

```bash
olf deploy --provider local --phase foundation
olf deploy --provider local --phase prefetch
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

Understanding these phases is useful when developing or troubleshooting OpenLakeForge.

---

## 1. Foundation

```bash
olf deploy --provider local --phase foundation
```

The foundation creates the local Kubernetes environment.

The default kind topology contains:

```text
1 control-plane
2 workers
```

Terraform manages the cluster foundation and writes its kubeconfig to
`.tmp/kubeconfigs/local.yaml` in a checkout, or
`~/.openlakeforge/state/local/kubeconfig.yaml` when `olf` is installed from
PyPI.

Running the command again is safe: Terraform reconciles the existing foundation rather than requiring you to recreate it manually.

---

## 2. Platform

```bash
olf deploy --provider local --phase platform
```

This phase uses Terraform and Helm to deploy the long-lived OpenLakeForge platform services.

Depending on the profile's preset, these include:

* SeaweedFS
* PostgreSQL
* Apache Polaris
* Trino
* Dagster
* OpenMetadata
* Superset

Slim disables the last two.

The platform phase only manages the relatively static infrastructure and platform services.

---

## 3. Artifacts

```bash
olf deploy --provider local --phase artifacts
```

The artifact phase deploys the parts of OpenLakeForge that change with data-product code.

This includes operations such as:

* building the project-code image
* loading it into kind
* discovering domain descriptors
* reconciling catalog namespaces
* generating Floe manifests
* publishing runtime artifacts
* deploying Dagster data-product code
* deploying OpenMetadata metadata when enabled
* importing Superset reports when enabled

Keeping platform infrastructure separate from data-product artifacts allows product changes to be deployed without rebuilding the entire environment.

See [Architecture](../architecture/README.md) for the deeper deployment model.

---

# Common workflows

## Check platform status

```bash
olf status --provider local
```

## Refresh data-product artifacts

If you changed domain code, contracts, pipelines or dbt models without changing the infrastructure:

```bash
olf deploy --provider local --phase artifacts
```

## Promote a project revision between stages

The v0.3 workflow (`olf platform apply` plus `olf project deploy`) works
locally, but it needs a **container registry**. Local runs deliberately do not
stand one up: an image built straight into kind has only a local config Id,
and that is not a pullable digest, so it cannot identify a revision that a
second stage is supposed to activate unchanged. Point the build at GHCR or
Docker Hub instead.

```bash
export PROJECT_CODE_IMAGE_REPOSITORY=ghcr.io/<your-account>/project-code
export PROJECT_CODE_IMAGE_TAG=dev
docker login ghcr.io                     # or docker.io

olf platform apply -f openlakeforge.yaml
olf project image  -f openlakeforge.yaml            # builds, pushes, loads into kind
olf project build  --project . --image ghcr.io/<your-account>/project-code@sha256:<digest>
olf project deploy -f openlakeforge.yaml --stage dev --revision sha256:<digest>
```

`olf project image` prints the digest-pinned reference the next command wants.
It also loads the image into kind, so the default `Never` pull policy still
finds it without a second pull.

Promoting to another enabled stage reuses that revision and never rebuilds it:

```bash
olf project deploy -f openlakeforge.yaml --stage prod --revision sha256:<digest>
olf project status -f openlakeforge.yaml --json
olf e2e run --env local -f openlakeforge.yaml --stage prod --suite full
```

`olf e2e run -f` takes the profile the platform was applied from, so the
topology it validates is the one the v3 contract recorded.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROJECT_CODE_IMAGE_REPOSITORY` | `ghcr.io/openlakeforge/project-code` | Where `olf project image` pushes. Must be a repository you can push to. |
| `PROJECT_CODE_IMAGE_TAG` | `local` | Tag to build and push. |

`olf project image --no-push` skips the push for a quick inner loop. It prints
a tag rather than a digest, so its output cannot seed `olf project build`.

## Reapply platform infrastructure

```bash
olf deploy --provider local --phase platform
```

This is useful after changing Terraform or Helm configuration. It reapplies
whatever `openlakeforge.yaml` resolves to, so it reconciles the stack you
have rather than switching its preset underneath you.

## Run the complete validation suite

```bash
olf e2e run --env local --suite full
```

This works for both Slim and Full: assertions for a disabled layer (OpenMetadata, Superset) are skipped rather than failed.

## Run the Slim smoke test

```bash
uv run --project tools/olf --locked olf smoke run
```

The smoke test creates the Slim environment and validates one discovered data product through to a queryable Gold table.

It is intended as a bounded deployment validation rather than a replacement for the full end-to-end suite. It is currently a contributor-checkout command (see below).

---

# Local configuration

The most commonly configurable values are exposed as `olf` options.

Default local configuration:

```text
Cluster name:     openlakeforge-local
Shared namespace: olf-system
Stage namespaces: olf-<stage>, one per enabled stage
Kube context:     kind-openlakeforge-local
Kubeconfig:   .tmp/kubeconfigs/local.yaml (checkout) or ~/.openlakeforge/state/local/kubeconfig.yaml (installed)
```

For example, to use another cluster name:

```bash
olf deploy --provider local --cluster-name my-openlakeforge
```

Or deploy and inspect a specific stage:

```bash
olf deploy --provider local --stage prod --phase platform
```

The local root exports a native provider-contract v3 payload. The artifacts
phase resolves its selected stage's storage, catalog, credentials, and ops
activation prefix, so deploying PROD artifacts cannot fall back to DEV's data
plane.

### Upgrading a v0.2 local deployment

The deprecated `--provider local --profile slim|full` command remains a
single-DEV Deployment Profile. Apply the platform phase before artifacts so
Terraform retains the existing DEV Bronze, Silver, Gold, ops, and
`lakehouse_dev` identities while it writes the v3 contract. The platform
recreates stage services in `olf-dev`; pause active runs during that apply.
After `olf profile resolve --json` and `olf deploy --provider local --phase
platform` succeed, deploy artifacts with an explicit `--stage dev`. Enabling
UAT or PROD then creates new stage-qualified medallion buckets and catalogs;
it does not copy DEV data.

The local kubeconfig path can also be overridden:

```bash
olf deploy --provider local --kubeconfig-path /path/to/openlakeforge-kubeconfig.yaml
```

For normal evaluation, the defaults are recommended.

---

# Troubleshooting

## Docker is unavailable

Check:

```bash
docker ps
```

If this fails, OpenLakeForge cannot create the kind cluster or build local images.

Make sure your Docker-compatible engine is running and accessible from the same shell in which you run `olf`.

## A required command is missing

`olf` provisions its own managed Terraform, Helm, kubectl, and kind, so this
usually means Docker or Python (the actual host prerequisites) is missing:

```text
ERROR: 'docker' not found on PATH
```

Install the missing tool and run the command again.

If `olf` itself reports it cannot provision a managed tool, run `olf doctor`
for the actionable reason:

```bash
olf doctor --provider local
```

If you set `OLF_TOOLCHAIN_MODE=host` to use your own host-installed
Terraform, Helm, kubectl, or kind, make sure that tool is on `PATH`.

## Kubernetes cluster is unreachable

Check the local kubeconfig (`.tmp/kubeconfigs/local.yaml` in a checkout,
`~/.openlakeforge/state/local/kubeconfig.yaml` installed):

```bash
KUBECONFIG=.tmp/kubeconfigs/local.yaml \
kubectl --context kind-openlakeforge-local cluster-info
```

If the cluster does not exist, recreate the foundation:

```bash
olf deploy --provider local --phase foundation
```

Then continue with:

```bash
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

Or simply rerun:

```bash
olf deploy --provider local
```

## A pod is not starting

First inspect the platform:

```bash
olf status --provider local
```

Then inspect the failing pod. `KUBECONFIG` below is the checkout path; use
`~/.openlakeforge/state/local/kubeconfig.yaml` instead when `olf` is
installed from PyPI:

```bash
KUBECONFIG=.tmp/kubeconfigs/local.yaml \
kubectl --context kind-openlakeforge-local \
describe pod <pod-name> -n olf-system
```

Check its logs:

```bash
KUBECONFIG=.tmp/kubeconfigs/local.yaml \
kubectl --context kind-openlakeforge-local \
logs <pod-name> -n olf-system
```

If the pod contains multiple containers:

```bash
KUBECONFIG=.tmp/kubeconfigs/local.yaml \
kubectl --context kind-openlakeforge-local \
logs <pod-name> -c <container-name> -n olf-system
```

## Image pulls are slow or timing out

OpenLakeForge provides a local prefetch step:

```bash
olf deploy --provider local --phase prefetch
```

This downloads larger runtime images ahead of Helm deployment and loads them into the kind nodes.

`olf deploy --provider local` already executes this automatically.

It can still be useful to rerun it independently after a failed image pull.

## Corporate proxy or custom TLS certificates

If your network intercepts HTTPS traffic, your Docker engine and kind nodes may reject external registries with an error similar to:

```text
x509: certificate signed by unknown authority
```

This is a container-runtime trust issue rather than an OpenLakeForge issue.

Configure your Docker engine to trust your organization's CA certificate, verify that images can be pulled normally, then retry:

```bash
olf deploy --provider local --phase prefetch
olf deploy --provider local
```

The exact certificate configuration depends on the Docker engine used on your machine.

## A local port is already in use

`olf forward` uses the following local ports:

| Port | Service          |
| ---: | ---------------- |
| 3000 | Dagster          |
| 8080 | Trino            |
| 8088 | Superset         |
| 8181 | Polaris          |
| 8585 | OpenMetadata     |
| 8888 | SeaweedFS Filer  |
| 9000 | SeaweedFS S3     |
| 9333 | SeaweedFS Master |

Check which process currently owns a port and stop it before running the forwarding command again.

## Terraform or Kubernetes state has become inconsistent

The local deployment includes recovery logic for some common state-drift situations.

First try rerunning:

```bash
olf deploy --provider local
```

Terraform will reconcile resources where possible.

If you want to recreate only the platform while keeping the kind cluster:

```bash
olf destroy --provider local --phase platform
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

For a completely clean environment, use the full teardown described below.

---

# Tear down OpenLakeForge

## Remove the platform

```bash
olf destroy --provider local
```

The default teardown removes:

1. OpenLakeForge platform services.
2. The local kind foundation.

If you only want to remove platform services while keeping the Kubernetes cluster:

```bash
olf destroy --provider local --phase platform
```

You can later reinstall them with:

```bash
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

---

# Contributor checkout

Contributors working from a source checkout of the repository use the same
`olf` commands above, run through `uv`:

```bash
uv run --project tools/olf --locked olf deploy --provider local
```

That workflow additionally needs Git and uv on `PATH`:

| Tool           | Purpose                              |
| -------------- | ------------------------------------- |
| Git            | Clone OpenLakeForge                   |
| uv             | Python dependency and CLI execution   |

Clone the repository and run commands from its root:

```bash
git clone https://github.com/OpenLakeForge/openlakeforge.git
cd openlakeforge
```

`olf` is the primary supported orchestration surface; a checkout's `Makefile`
provides deprecated one-line delegates to the same `olf` commands and is
being removed (#222) — see [AGENTS.md](../../AGENTS.md) for the full
contributor workflow and gates.

---

# Next steps

Once the local platform is running, the next step is to replace the included examples with one of your own data products.

➡️ [Build your first data product](../getting-started/first-data-product.md)

You may also want to read:

* [Architecture overview](../architecture/overview.md)
* [Provider contracts](../architecture/provider-contracts.md)
* [`olf` CLI documentation](../../tools/olf/README.md)
* [Architecture Decision Records](../adr/README.md)
* [AWS and Azure deployment](cloud-poc-setup.md)
