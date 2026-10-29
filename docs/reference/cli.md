# `olf` CLI reference

The `olf` commands you use to create, deploy, operate and validate an
OpenLakeForge project, grouped by task. `olf <command> --help` prints every
option of a command.

`olf` is installed with the `openlakeforge` Python package. It provisions its
own pinned Terraform, Helm, kubectl and kind under `OLF_HOME`
(`~/.openlakeforge` by default) and uses the AWS and Azure SDKs, so none of
those tools or cloud CLIs need to be installed. Run one `olf` command at a
time: concurrent invocations are not supported. A local, an AWS and an Azure
deployment can exist side by side, each with its own state and kubeconfig
under `OLF_HOME/state/<provider>/`.

## Options shared by lifecycle commands

`deploy`, `destroy`, `status`, `forward`, `doctor` and `plan` take:

| Option | Default | Meaning |
| --- | --- | --- |
| `--provider` | `local` | `local`, `aws` or `azure` |
| `--phase` | `all` | One phase; see each command |
| `--stage` | `dev` | `dev`, `uat` or `prod` |
| `--project-root` | current directory | The project holding `openlakeforge.yaml` |
| `--cluster-name` | `openlakeforge-local` | Local kind cluster name |
| `--kubeconfig-path` | under `OLF_HOME/state/<provider>/` | Where `olf` keeps the cluster's kubeconfig |
| `--var-file` | none | A Terraform variables file (cloud) |

`--profile slim|full` is a deprecated shorthand for a single-DEV profile; use
`spec.preset` in `openlakeforge.yaml` instead.

## Create a project

| Command | Does |
| --- | --- |
| `olf init` | Write `openlakeforge.yaml` and `lakehouse_code/` with the demo products. Options: `--stages dev,prod`, `--preset full`, `--name`, `--empty`, `--profile-only`. |
| `olf source new <source> --resource <r>` | Scaffold a Bronze source: `source.yaml`, a dlt loader and a placeholder CSV per resource. |
| `olf domain new <domain> --input <source>/<resource>` | Scaffold a domain and its Floe contract, one Silver table per `--input`. |
| `olf product new <domain>/<product> --gold-table <mart>` | Scaffold a dbt project and Dagster module. Inputs: `--silver-input <table>` or `--input <source>/<resource>`; `--with-report` adds a Superset draft. |

All `new` commands validate the result before writing and accept
`--display-name`.

## Deploy and operate

| Command | Does |
| --- | --- |
| `olf deploy` | Deploy all phases, or one with `--phase foundation`, `prefetch` (local), `platform` or `artifacts`. |
| `olf destroy` | Tear down the platform, then the foundation; `--phase platform` or `foundation` for one. `--force` destroys the foundation with platform resources left. |
| `olf status` | Service URLs, then pods, services and PVCs per namespace. `--json` for JSON. |
| `olf forward` | Port-forward every service to `localhost` until Ctrl-C. |
| `olf doctor` | Check the host, tools, project and cloud authentication without changing anything. |
| `olf plan` | Show the Terraform plan of the foundation or platform. |
| `olf query "<sql>"` | Run SQL against a stage's catalog (`--stage`, `--format table\|csv\|json`, `--file`). Local and Azure only. |
| `olf access trust` | Export the local CA and print the commands to trust it. |
| `olf github connect` | Create the GitHub App for sign-in and store its credentials. |
| `olf auth login\|status\|logout --provider aws\|azure` | Authenticate to a cloud through its SDK. |
| `olf diagnostics collect <dir>` | Write host and cluster evidence for a bug report (`--namespace`, `--kube-context`). |

## Validate

| Command | Checks |
| --- | --- |
| `olf profile validate --project .` | `openlakeforge.yaml` |
| `olf profile resolve --project . --json` | Prints the resolved stages and capabilities |
| `olf check contracts` | `lakehouse.yaml`, every `source.yaml`, and the Floe project's namespaces |
| `olf project graph --project .` | Prints the Source → Domain → Product → Dashboard lineage as Mermaid text |
| `olf check dbt` | Every product's dbt project compiles |
| `olf check security` | Every entry in `.trivyignore.yaml` has an owner and an unexpired date within 90 days; not part of `olf check all` |
| `olf report validate [<dashboard>]` | Superset bundles are promotable |
| `olf e2e run --env local\|aws\|azure` | The deployed platform end to end. `--suite smoke` runs the preflight checks only; `--stage` and `-f <profile>` select what to check. |

## Stages and revisions

| Command | Does |
| --- | --- |
| `olf platform apply -f openlakeforge.yaml` | Create the cluster, shared services and every enabled stage, without your code. |
| `olf project image -f openlakeforge.yaml` | Build and push the project-code image; prints a digest-pinned reference. |
| `olf project build --project . --image <ref>` | Build and publish an immutable revision; prints its id. |
| `olf project deploy -f openlakeforge.yaml --stage <s> --revision <id>` | Activate a revision in a stage without rebuilding. |
| `olf project status -f openlakeforge.yaml` | The revision active in each stage. |
| `olf project revision inspect\|verify --revision <id>` | Read or verify a published revision. |
| `olf report export --stage dev` | Export Superset dashboards into their bundles. |
| `olf report import` | Import the bundles into a stage's Superset. |

## Installation and toolchain

| Command | Does |
| --- | --- |
| `olf version` | The installed version. |
| `olf toolchain list\|install\|path\|clean` | Inspect, download or remove the managed Terraform, Helm, kubectl and kind. |
| `olf distribution path\|verify` | The platform payload the package ships, such as the Terraform roots. |

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLF_HOME` | `~/.openlakeforge` | Toolchain, platform payload, state and kubeconfigs |
| `OLF_TOOLCHAIN_MODE` | `managed` | `host` uses Terraform, Helm, kubectl and kind from `PATH` |
| `OLF_PORT_FORWARD` | unset | `1` makes `olf` port-forward instead of using the local ingress |
| `PROJECT_CODE_IMAGE_REPOSITORY`, `PROJECT_CODE_IMAGE_TAG` | `ghcr.io/openlakeforge/openlakeforge/project-code`, `local` | Where `olf project image` pushes on the local provider |
| `AWS_*`, `AZURE_*` | | Cloud settings; see [Deploy to AWS](../guides/cloud/deploy-to-aws.md) and [Deploy to Azure](../guides/cloud/deploy-to-azure.md) |

## Contributor commands

A source checkout runs the same commands through
`uv run --project tools/olf --locked olf`. Commands that only make sense there,
such as `olf check all`, `olf smoke run` and `olf release`, are described in
[AGENTS.md](../../AGENTS.md) and [tools/olf/README.md](../../tools/olf/README.md).
