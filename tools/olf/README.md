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
| `olf check structure\|components\|contracts\|infra\|project-code\|dbt\|lockfiles\|all` | Repository validation gates. |
| `olf contracts env\|check` | Print the resolved provider contract environment, or validate it against the active provider/profile. |
| `olf dbt parse [--project-dir D]` | Render dbt profiles and run `dbt deps`/`dbt parse` for one or every product Gold project. |
| `olf source new NAME --resource R` / `olf domain new NAME --input SRC/RESOURCE` / `olf product new DOMAIN/PRODUCT --input ... --gold-table T` | Golden-path scaffolding for a Bronze source, Silver domain, or Gold product. |
| `olf layers enabled --profile P` | List which medallion/governance/analytics layers a profile enables. |
| `olf images build\|load project-code\|superset` | Local Docker build and Kind load operations. |
| `olf diagnostics collect OUTPUT_DIR` | Collect bounded host, Docker, Kubernetes, event, and pod-log evidence. |
| `olf catalog sync-namespaces [--dry-run] [--prune]` | Reconcile catalog namespaces (Polaris) or databases (Glue) with domain descriptors (ADR 0002). Runs first in the artifacts phase, before any table is written. It creates, adopts matching legacy namespaces, and relocates OpenLakeForge-managed namespaces. `--prune` removes only OpenLakeForge-managed catalog metadata and retains object-store files; foreign namespaces are never changed. Unsupported providers fail explicitly. |
| `olf floe render-profile\|generate-manifests` | Render the Floe EnvironmentProfile YAML for the active contract env, or generate domain Floe manifests. |
| `olf artifacts upload-manifests --via port-forward\|direct` | Publish domain Floe manifests to the ops bucket (in-cluster S3 or cloud S3). |
| `olf floe revision compute\|publish\|verify --runtime-root D` | Compute, publish, or verify an immutable Floe runtime-artifact revision. Publication writes `floe/revisions/sha256/<digest>/...` and does not activate that revision. |
| `olf project build --project P --image REF` | Build and publish the immutable `ProjectRevision` covering descriptors, Floe contracts, dbt, Dagster, reports, and the project-code image digest for one writable project (#154). |
| `olf project revision inspect\|verify --revision R` | Inspect or verify a published `ProjectRevision` manifest without rebuilding source. |
| `olf superset deploy-reports` / `export-reports` | Build/import or export Superset report bundles. |
| `olf openmetadata deploy-metadata` | Seed OpenMetadata domains, data products, and medallion containers over REST. |
| `olf k8s set-project-code-image --image X` | Point every Dagster surface at a pushed project-code image, trigger one coordinated restart, and wait for its rollout. |
| `olf smoke run` | Deploy a bounded Slim environment and validate one product pipeline through to a queryable Gold table. |
| `olf e2e run --env local\|azure\|aws [-f <profile>] [--stage <stage>] [--suite full\|smoke]` | Run shared end-to-end validation. All environments default to `full`; use `--suite smoke` for preflight-only checks. `-f` names the Deployment Profile the deployment was applied from, as `olf platform apply -f` does; without it the project-root `openlakeforge.yaml` is authoritative. |
| `olf release manifest\|checksums\|compatibility-matrix\|check\|build-bundle\|verify-install` | Build release metadata, run the release-readiness gate, assemble the release bundle, and verify a clean-checkout install. |
| `olf distribution list\|path\|verify\|clean` | Inspect or verify the embedded platform payload shipped inside the `openlakeforge` wheel/sdist. |

## Development

```sh
uv sync --project tools/olf
uv run --project tools/olf pytest
uv run --project tools/olf ruff check tools/olf
```

Make is optional, deprecated checkout compatibility. Its targets are one-line
delegates to the same commands and are not the supported product interface.
