# olf — OpenLakeForge deployment tooling

`olf` is the supported OpenLakeForge interface. It owns repository
orchestration for local, AWS, and Azure: Terraform/Helm sequencing, provider
contract hydration, artifacts, checks, diagnostics, and release helpers.
Terraform remains the state/drift engine and Helm remains the chart/release
engine; `olf` invokes both with structured argv, retries, and diagnostics. See
[ADR 0008](../../docs/adr/0008-olf-owns-orchestration-and-toolchain.md).

`olf` also owns its own versioned Terraform, Helm, kubectl, and kind under
`OLF_HOME` (default `~/.openlakeforge`) - a host installation of those tools
is not required. `OLF_TOOLCHAIN_MODE=host` resolves them from `PATH` instead.
See [ADR 0008](../../docs/adr/0008-olf-owns-orchestration-and-toolchain.md).

## Commands

| Command | Purpose |
| --- | --- |
| `olf init [--empty] [--stages dev,prod] [--preset slim\|full] [--name N] [--profile-only]` | Create a writable `lakehouse_code/` project and its Deployment Profile in the current directory from the packaged demo, or an empty transitional project; `--profile-only` writes only `openlakeforge.yaml` into an existing project. |
| `olf doctor --provider P [--phase PHASE]` / `olf plan --provider P [--phase PHASE]` | Read-only preflight and Terraform planning with typed provider/profile/phase options; provisions the managed toolchain as a side effect. |
| `olf deploy\|destroy\|status\|forward --provider P` | Orchestrate a provider lifecycle without shell wrappers. |
| `olf auth login\|status\|logout --provider aws\|azure` | Authenticate through AWS IAM Identity Center or Microsoft Entra SDKs; no cloud CLI required (ADR 0008). |
| `olf toolchain list\|install\|path\|clean` | Inspect, provision, or remove the managed Terraform/Helm/kubectl/kind toolchain under `OLF_HOME`. |
| `olf check structure\|docs\|components\|contracts\|infra\|project-code\|dbt\|lockfiles\|all` | Repository validation gates; `docs` validates `docs/navigation.yaml` and every relative docs link. |
| `olf contracts env\|check` | Print the resolved provider contract environment, or validate it against the active provider/profile. |
| `olf dbt parse [--project-dir D]` | Render dbt profiles and run `dbt deps`/`dbt parse` for one or every product Gold project. |
| `olf source new NAME --resource R` / `olf domain new NAME --input SRC/RESOURCE` / `olf product new DOMAIN/PRODUCT --input ... --gold-table T` | Golden-path scaffolding for a Bronze source, Silver domain, or Gold product. |
| `olf layers enabled --profile P` | List which medallion/governance/analytics layers a profile enables. |
| `olf images build\|load project-code\|superset` | Local Docker build and Kind load operations. |
| `olf diagnostics collect OUTPUT_DIR` | Collect bounded host, Docker, Kubernetes, event, and pod-log evidence. |
| `olf catalog sync-namespaces [--dry-run] [--prune]` | Reconcile catalog namespaces (Polaris) or databases (Glue) with domain descriptors (ADR 0002). Runs first in the artifacts phase, before any table is written. It creates, adopts matching legacy namespaces, and relocates OpenLakeForge-managed namespaces. `--prune` removes only OpenLakeForge-managed catalog metadata and retains object-store files; foreign namespaces are never changed. Unsupported providers fail explicitly. |
| `olf project build --project P --image REF` | Build and publish the immutable `ProjectRevision` covering descriptors, Floe contracts, dbt, Dagster, reports, and the project-code image digest for one writable project (#154). |
| `olf project graph --project P` | Print the Source → Domain → Product → Dashboard lineage declared in the project's descriptors as a Mermaid `flowchart LR`, for pasting into GitHub Markdown or the Mermaid Live Editor. |
| `olf project revision inspect\|verify --revision R` | Inspect or verify a published `ProjectRevision` manifest without rebuilding source. |
| `olf report import\|export\|validate` | Build and import, export, or validate Superset report bundles. Export selects the dashboard by the checked-in bundle's uuid, or by `SUPERSET_DASHBOARD` (uuid or slug). `olf superset deploy-reports` / `export-reports` remain as deprecated aliases. |
| `olf openmetadata deploy-metadata` | Seed OpenMetadata domains, data products, and medallion containers over REST. |
| `olf openmetadata list-domains [--provider P] [--json]` | List the domains OpenMetadata exposes, sorted by name, as a table or JSON. Read-only: it sends only GET requests, authenticated with the automation bot token. |
| `olf k8s set-project-code-image --image X` | Point every Dagster surface at a pushed project-code image, trigger one coordinated restart, and wait for its rollout. |
| `olf bench footprint [--clean]` / `olf bench etl [--scale N] [--delta N]` | Benchmark a local deployment (#328): install phases, first query and pipeline, installed and idle footprint; or a deterministically scaled dataset through every product job. Each run writes a JSON result. See [docs/testing/benchmarks.md](../../docs/testing/benchmarks.md). |
| `olf smoke run` | Deploy a bounded Slim environment and validate one product pipeline through to a queryable Gold table. |
| `olf query [-f <profile>] [--stage <stage>] [--format table\|csv\|json] "<sql>"` (or `--file <sql file>`) | Run SQL against a stage's Trino catalog, with that catalog as the session default so SQL names `<schema>.<table>`. Runs as the stage's shared runtime workload user, which can write: for inspecting data, not loading it, and not per-user access. Local and Azure only; AWS fails until Glue schema prefixes are handled. |
| `olf e2e run --env local\|azure\|aws [-f <profile>] [--stage <stage>] [--suite full\|smoke]` | Run shared end-to-end validation. All environments default to `full`; use `--suite smoke` for preflight-only checks. `-f` names the Deployment Profile the deployment was applied from, as `olf platform apply -f` does; without it the project-root `openlakeforge.yaml` is authoritative. |
| `olf release manifest\|checksums\|compatibility-matrix\|check\|build-bundle\|verify-install` | Build release metadata, run the release-readiness gate, assemble the release bundle, and verify a clean-checkout install. |
| `olf distribution list\|path\|verify\|clean` | Inspect or verify the embedded platform payload shipped inside the `openlakeforge` wheel/sdist. |

## Development

You can change and test the `olf` CLI without deploying the platform or
starting a Kubernetes cluster. The unit tests under `tools/olf/tests/` run
locally. Not every integration test is offline, so check a test before
assuming it is.

Run every command from the repository root.

1. Install dependencies:

```sh
   uv sync --project tools/olf
```

2. While iterating, run only the test module for the code you changed. This
   example covers `tools/olf/olf/deployment/cloud/config.py`:

```sh
   uv run --project tools/olf pytest tools/olf/tests/test_cloud_config.py
```

   To list the tests in a module and run one of them:

```sh
   uv run --project tools/olf pytest tools/olf/tests/test_cloud_config.py --collect-only -q
   uv run --project tools/olf pytest tools/olf/tests/test_cloud_config.py -k test_image_platform_env_var_is_scope_prefixed
```

3. Lint the file you touched:

```sh
   uv run --project tools/olf ruff check tools/olf/olf/deployment/cloud/config.py
```

4. Before opening a pull request, run all the gates from
   [`AGENTS.md`](../../AGENTS.md):

```sh
   uv run --project tools/olf olf check all
   uv run --project tools/olf ruff check tools/olf
   uv run --project tools/olf mypy --config-file tools/olf/pyproject.toml tools/olf/olf
   uv run --project tools/olf pytest tools/olf/tests --cov --cov-config=tools/olf/pyproject.toml
```

See [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for the path from a fresh clone
to a pull request. [`AGENTS.md`](../../AGENTS.md) covers the architecture
rules, commit style, and the one-change-per-pull-request rule.