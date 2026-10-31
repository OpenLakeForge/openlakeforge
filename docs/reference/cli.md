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

## Where commands live

Commands that need cluster or cloud permissions are under `olf infra`. Commands
that need none, such as `olf init`, `olf check` and `olf project validate`, stay
at the top level. `olf --help` lists the top-level groups and `olf infra --help`
the infrastructure ones. The earlier top-level paths of the infra commands
(`olf deploy`, `olf project deploy`) still work as hidden aliases. Each prints one
line on stderr naming its new path and then runs unchanged, so scripts that parse
stdout keep working. `olf query` is the exception: it is a platform command of
its own, a person's queries, while `olf infra query` runs as the stage's
workload user. See
[ADR 0020](../adr/0020-infrastructure-common-and-platform-cli-surfaces.md).

## Options shared by lifecycle commands

`olf infra deploy`, `destroy`, `status`, `forward`, `doctor` and `plan` take:

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
`spec.preset` in `openlakeforge.yaml` instead. A stack deployed with it is
validated with the same flag: `olf infra e2e run --env local --profile slim`.

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
| `olf infra deploy` | Deploy all phases, or one with `--phase foundation`, `prefetch` (local), `platform` or `artifacts`. |
| `olf infra destroy` | Tear down the platform, then the foundation; `--phase platform` or `foundation` for one. `--force` destroys the foundation with platform resources left. |
| `olf infra status` | Service URLs, then pods, services and PVCs per namespace. `--json` for JSON. |
| `olf infra forward` | Port-forward every service to `localhost` until Ctrl-C. |
| `olf infra doctor` | Check the host, tools, project and cloud authentication without changing anything. |
| `olf infra plan` | Show the Terraform plan of the foundation or platform. |
| `olf infra product run <product>` | Launch a product's pipeline in a stage and wait for it (`--stage`, `-f`). `--full-refresh bronze\|gold` asks that run only for a full refresh; see [Full refresh](descriptors.md#full-refresh). |
| `olf infra query "<sql>"` | Run SQL against a stage's catalog (`--stage`, `--format table\|csv\|json`, `--file`). Local and Azure only. |
| `olf infra access trust` | Export the local CA and print the commands to trust it. |
| `olf infra github connect` | Create the GitHub App for sign-in and store its credentials. |
| `olf infra auth login\|status\|logout --provider aws\|azure` | Authenticate to a cloud through its SDK. |
| `olf infra diagnostics collect <dir>` | Write host and cluster evidence for a bug report (`--namespace`, `--kube-context`). |

## Validate

| Command | Checks |
| --- | --- |
| `olf profile validate --project .` | `openlakeforge.yaml` |
| `olf profile resolve --project . --json` | Prints the resolved stages and capabilities |
| `olf check contracts` | `governance.yml`, every `source.yaml`, and the Floe project's namespaces |
| `olf project graph --project .` | Prints the Source → Domain → Product → Dashboard lineage as Mermaid text |
| `olf check dbt` | Every product's dbt project compiles |
| `olf check security` | Every entry in `.trivyignore.yaml` has an owner and an unexpired date within 90 days; not part of `olf check all` |
| `olf report validate [<dashboard>]` | Superset bundles are promotable; needs no cluster or cloud credentials |
| `olf infra e2e run --env local\|aws\|azure` | The deployed platform end to end. `--suite smoke` runs the preflight checks only; `--stage` and `-f <profile>` select what to check. |

## Stages and revisions

| Command | Does |
| --- | --- |
| `olf infra platform apply -f openlakeforge.yaml` | Create the cluster, shared services and every enabled stage, without your code. |
| `olf infra project image -f openlakeforge.yaml` | Build and push the project-code image; prints a digest-pinned reference. For the unmodified public example it prints the release-pinned reference and builds nothing. |
| `olf infra project build --project . --image <ref>` | Build and publish an immutable revision; prints its id. |
| `olf infra project deploy -f openlakeforge.yaml --stage <s> --revision <id>` | Activate a revision in a stage without rebuilding. |
| `olf infra project status -f openlakeforge.yaml` | The revision active in each stage. |
| `olf infra project revision inspect\|verify --revision <id>` | Read or verify a published revision. |
| `olf infra report export --stage dev` | Export Superset dashboards into their bundles. |
| `olf infra report import` | Import the bundles into a stage's Superset. |

## Sign in from the CLI

`olf login`, `olf whoami` and `olf logout` keep a Dex or external-issuer session
for the deployment in `openlakeforge.yaml`. They need no kubeconfig and read no
Secret.

| Command | Does |
| --- | --- |
| `olf login` | Authorization Code with PKCE in your browser. `--no-browser` prints the URL instead. |
| `olf whoami` | Instance, issuer, username, email, groups and ID token expiry. Refreshes an expired ID token. |
| `olf logout` | Deletes the stored session. |

Each takes `--profile-file` (`-f`, default `openlakeforge.yaml`). The issuer is
`https://auth.<base_domain>` with `identity.issuer: github`, or `issuer_url`
with `external`. The client id is `identity.client_ids['olf-cli']`, else
`olf-cli`. With access `issuer: none` the Dex issuer is `http://`.

A profile with `spec.access` and no `spec.identity` has authentication off:
`olf login` and `olf whoami` say that sign-in is disabled and exit 0, `olf
logout` does nothing, and platform commands run without login. A profile
without `spec.access` publishes no routes, so these commands fail and point to
`olf infra`.

The session is `$OLF_HOME/sessions/<base_domain>.json`, mode 0600, and holds
the ID, access and refresh tokens. Run `olf login` again when `olf` reports the
session expired.

A local deployment serves the issuer with its own CA, which Python does not
read from the system store. Export it, then point `olf` at it:

```bash
olf infra access trust --output ./olf-local-ca.crt
export REQUESTS_CA_BUNDLE="$PWD/olf-local-ca.crt"   # SSL_CERT_FILE also works
olf login
```

## Query as yourself

`olf query` runs SQL against a stage's data as the signed-in person, through
Trino at `https://trino.<base_domain>/v1`. It needs `spec.identity.issuer:
github` and `olf login` first. What you may read or write on each stage and
layer is your role's `data_access` row in `release/identity-roles.yaml`, which
Polaris enforces ([ADR 0019](../adr/0019-data-plane-authorization-deferral.md)).
When Polaris or the object store refuses a query, `olf query` says why.

```bash
olf query --stage prod "SELECT count(*) FROM order_revenue_gold.mart_order_revenue_by_day"
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--stage` | `dev` | `dev`, `uat` or `prod`: the stage's people catalog is the session default, so SQL names `<schema>.<table>` |
| `--format` | `table` | `table`, `csv` or `json` (one object per row) |
| `--file` | — | Read the SQL from a file instead of the argument |
| `--profile-file`, `-f` | `openlakeforge.yaml` | Deployment Profile of the deployment |

`olf infra query` runs the same SQL as the stage's workload user through
`kubectl exec`, for operators: it needs cluster access, not a sign-in.

## Read the platform

These commands read the platform as the signed-in person, through the platform
API at `https://<base_domain>/api`
([ADR 0021](../adr/0021-platform-api.md)). They need `olf login` first, no
kubeconfig, and no infra credential: the API decides what your role may read, and
what it may launch.

| Command | Reads | Lowest role |
| --- | --- | --- |
| `olf jobs list` | The stage's pipeline jobs | `data-engineer` |
| `olf runs list` | The stage's most recent runs, newest first; `--job` for one job, `--limit` 1 to 100 (default 20) | `data-engineer` |
| `olf runs show <run-id>` | One run's status, times and tags | `data-engineer` |
| `olf runs launch <job>` | Launches the job as you, on `dev` only; the API stores an audit record first and launches nothing if it cannot | `data-engineer` |
| `olf logs` | The stage's archived pod log lines for a day (`--date`, default today in UTC) or one `--hour`; `--pod`, `--container`, `--limit` 1 to 5000 (default 500) | `data-engineer` |
| `olf domain list` | Governance domains, as OpenMetadata shows them to you | `viewer`, then your OpenMetadata roles |
| `olf product list` | Data products and their domains, as OpenMetadata shows them to you | `viewer`, then your OpenMetadata roles |

Each takes `--json` to print the API's JSON instead of a table, and
`--profile-file` (`-f`). `jobs`, `runs` and `logs` take `--stage` (default `dev`). When
the API refuses, the command says why: an expired or rejected session asks you
to run `olf login`, and a role below the operation's floor names the role it
needs.

## Installation and toolchain

| Command | Does |
| --- | --- |
| `olf version` | The installed version. |
| `olf toolchain list\|install\|path\|clean` | Inspect, download or remove the managed Terraform, Helm, kubectl and kind. |
| `olf distribution path\|verify` | The platform payload the package ships, such as the Terraform roots. |

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLF_HOME` | `~/.openlakeforge` | Toolchain, platform payload, state, kubeconfigs and sign-in sessions |
| `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` | unset | CA file `olf login` verifies the issuer against |
| `OLF_TOOLCHAIN_MODE` | `managed` | `host` uses Terraform, Helm, kubectl and kind from `PATH` |
| `OLF_PORT_FORWARD` | unset | `1` makes `olf` port-forward instead of using the local ingress |
| `PROJECT_CODE_IMAGE_REPOSITORY`, `PROJECT_CODE_IMAGE_TAG` | `ghcr.io/openlakeforge/openlakeforge/project-code`, `local` | Where `olf infra project image` pushes on the local provider |
| `AWS_*`, `AZURE_*` | | Cloud settings; see [Deploy to AWS](../guides/cloud/deploy-to-aws.md) and [Deploy to Azure](../guides/cloud/deploy-to-azure.md) |

## Contributor commands

A source checkout runs the same commands through
`uv run --project tools/olf --locked olf`. Commands that only make sense there,
such as `olf check all`, `olf infra smoke run` and `olf release`, are described in
[AGENTS.md](../../AGENTS.md) and [tools/olf/README.md](../../tools/olf/README.md).
