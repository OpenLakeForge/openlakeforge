# Install and configure a local deployment

The options around a local OpenLakeForge deployment: how to install `olf`,
create a project, choose a preset, reach the services over HTTP or HTTPS, and
tear everything down. For the shortest path, follow the
[Quickstart](../get-started/quickstart.md) instead.

The local platform runs in a [kind](https://kind.sigs.k8s.io/) cluster on your
Docker engine. It does not touch your own `~/.kube/config`, and it is local
evaluation infrastructure: do not expose it beyond your workstation.

## Requirements

| You need | Check |
| --- | --- |
| A running Docker engine | `docker ps` succeeds |
| Python 3.12 or newer, or [uv](https://docs.astral.sh/uv/) | `python3.12 --version` or `uv --version` |
| Ports 80 and 443 free on `127.0.0.1` | Nothing else serves `http://localhost` |

Terraform, Helm, kubectl and kind are not prerequisites: `olf` downloads the
versions pinned in the release and keeps them under `~/.openlakeforge`.

## Install `olf`

With uv, which fetches Python 3.12 if your machine has none:

```bash
uv tool install "openlakeforge==0.4.0a1" --python 3.12
```

Or with pip, in a virtual environment:

```bash
python3.12 -m venv ~/.venvs/olf
source ~/.venvs/olf/bin/activate
python3.12 -m pip install "openlakeforge==0.4.0a1"
```

With pip, activate the environment again in each new shell. Check the install
with `olf version`.

### Toolchain mode

`OLF_TOOLCHAIN_MODE` decides where `olf` finds Terraform, Helm, kubectl and
kind. Docker is always yours.

| Mode | Behaviour |
| --- | --- |
| `managed` (default) | Downloads and verifies the pinned versions under `~/.openlakeforge`. Never changes your `PATH`. |
| `host` | Uses the tools on your `PATH`, for a machine that cannot download them. |

`OLF_HOME` moves the toolchain, the platform payload and `olf`'s state away
from `~/.openlakeforge`. `olf toolchain list` shows what is installed and
`olf toolchain install` downloads everything in advance.

## Create a project

Run `olf init` in an empty directory:

```bash
mkdir my-lakehouse && cd my-lakehouse
olf init
```

It writes `openlakeforge.yaml`, the Deployment Profile, and `lakehouse_code/`
with three demo data products. The profile enables one DEV stage on the Slim
preset, with plain HTTP and no sign-in. `olf init` refuses to overwrite either.

| Option | Effect |
| --- | --- |
| `--stages dev,prod` | Enable other stages: `dev`, `dev,prod` or `dev,uat,prod`. |
| `--preset full` | Add Superset and OpenMetadata to the Slim data path. |
| `--name <name>` | Name the profile something other than the directory. |
| `--profile-only` | Write only `openlakeforge.yaml`, into an existing project. |
| `--empty` | Skip the demo products. |

An `--empty` project cannot deploy until it holds a source and a product.
Scaffold them first:

```bash
olf source new crm --resource accounts
olf product new sales/accounts_report --input crm/accounts --gold-table mart_accounts
```

## Deploy

```bash
olf deploy --provider local
```

The first deploy takes several minutes while images download. `olf deploy`
runs four phases in order; `--phase` runs one of them:

| Phase | What it does | Rerun it when |
| --- | --- | --- |
| `foundation` | Creates the kind cluster `openlakeforge-local` (one control plane, two workers) | The cluster was deleted |
| `prefetch` | Pulls the large runtime images into the kind nodes | An image pull failed or timed out |
| `platform` | Deploys the shared services and one namespace per stage with Terraform and Helm | You changed the preset or the stages |
| `artifacts` | Builds the project-code image from `lakehouse_code/` and deploys your data products | You changed code under `lakehouse_code/` |

Every phase reconciles what already exists, so running it again is safe.

## Choose Slim or Full

| Preset | Includes |
| --- | --- |
| `slim` (default) | dlt, Floe, Iceberg, Polaris, Trino, dbt and Dagster: the whole ingestion-to-Gold path |
| `full` | Slim plus OpenMetadata (governance, lineage) and Superset (dashboards) |

Set `preset: full` under `spec` in `openlakeforge.yaml` and run
`olf deploy --provider local` again. The change applies in place: nothing
already deployed is removed. Setting `preset: slim` removes the two services
the same way.

## Local defaults

| Setting | Default | Change it with |
| --- | --- | --- |
| Cluster | `openlakeforge-local` | `--cluster-name` |
| Kube context | `kind-openlakeforge-local` | follows the cluster name |
| Kubeconfig | `~/.openlakeforge/state/local/kubeconfig.yaml` | `--kubeconfig-path` |
| Shared services | namespace `olf-system` | |
| Stage services | namespace `olf-<stage>`, one per enabled stage | `spec.stages` in the profile |
| Stage selected by `olf` commands | `dev` | `--stage` |

In a source checkout of the repository, the kubeconfig is
`.tmp/kubeconfigs/local.yaml` instead.

## Access the services

```bash
olf status --provider local
```

The `URLs` section lists every service. With the default profile:

| Service | URL |
| --- | --- |
| Portal, linking every service | `http://olf.localhost` |
| Dagster, per stage | `http://dagster-dev.olf.localhost` |
| Trino | `http://trino.olf.localhost` |
| Superset, per stage (Full) | `http://superset-dev.olf.localhost` |
| OpenMetadata (Full) | `http://openmetadata.olf.localhost` |

Browsers resolve every `*.localhost` name to your machine. Command-line tools
often do not; `olf` pins them itself, and for `curl` pass `--resolve`:

```bash
curl --resolve dagster-dev.olf.localhost:80:127.0.0.1 http://dagster-dev.olf.localhost/server_info
```

With sign-in off, Superset opens as its local Admin and OpenMetadata accepts
`admin@open-metadata.org` / `admin`. To require sign-in, see
[Authentication and permissions](../setup/github-oauth-app.md).

### Serve HTTPS

`olf init` writes `spec.access.issuer: none`, which serves plain HTTP. To serve
HTTPS from a local certificate authority (CA), remove the `issuer` line so it
takes its default, `local-ca`, and deploy again:

```yaml
spec:
  access: {}
```

`olf deploy` creates the CA once, in the cluster. Your browser rejects the
routes until it trusts that CA. Export it and print the trust commands for
your system:

```bash
olf access trust
```

It writes `openlakeforge-local-ca.crt` and prints the commands for Linux,
macOS or WSL2; each needs your own privileges, so `olf` prints rather than runs
them. Firefox keeps its own store: Settings, Privacy & Security, View
Certificates, Authorities, Import. On WSL2 the CA must go into the Windows
store, because the browser runs on Windows.

### Reach Polaris and SeaweedFS

Polaris and SeaweedFS have no route. `olf forward` port-forwards every service
to fixed ports on `localhost` until you press Ctrl-C:

```bash
olf forward --provider local
```

| Port | Service |
| ---: | --- |
| 3000 | Dagster |
| 8080 | Trino |
| 8088 | Superset (Full) |
| 8181 | Polaris API |
| 8585 | OpenMetadata (Full) |
| 8888 | SeaweedFS Filer UI |
| 9000 | SeaweedFS S3 API |
| 9333 | SeaweedFS Master UI |

`OLF_PORT_FORWARD=1` makes `olf`'s own commands, such as `olf e2e run`,
port-forward instead of using the ingress, for example while you debug it.

## Tear down

```bash
olf destroy --provider local
```

It destroys the platform services, then the kind cluster. The cluster holds
all state, so this deletes your Bronze, Silver and Gold data and Dagster's run
history. `openlakeforge.yaml` and `lakehouse_code/` are not touched, and the
next `olf deploy` rebuilds the same platform from them. With HTTPS, a rebuilt
cluster has a new CA: run `olf access trust` again.

| To remove | Run |
| --- | --- |
| Only the platform, keeping the cluster | `olf destroy --provider local --phase platform` |
| Only the cluster, after the platform | `olf destroy --provider local --phase foundation` |
| The managed toolchain under `~/.openlakeforge` | `olf toolchain clean --all` |

`--phase foundation` refuses to run while platform namespaces exist; `--force`
overrides that and loses them. After `--phase platform`, bring the platform
back with `olf deploy --provider local --phase platform`, then
`--phase artifacts`.

## Contributor checkout

To work on OpenLakeForge itself, clone the repository and run the same
commands through uv from its root:

```bash
git clone https://github.com/OpenLakeForge/openlakeforge.git
cd openlakeforge
uv run --project tools/olf --locked olf deploy --provider local
```

The contributor workflow and its gates are in
[AGENTS.md](../../AGENTS.md).

## Check it worked

```bash
olf e2e run --env local --suite full
```

It launches every product's job and checks the Silver and Gold tables through
Trino; on Full it also checks Superset and OpenMetadata. It ends with
`Local OpenLakeForge full e2e validation passed.`
