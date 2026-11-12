# Project Code Image

`images/project-code/` is the custom runtime image boundary for OpenLakeForge
domain Dagster code.

The image is built locally as:

```text
ghcr.io/openlakeforge/openlakeforge/project-code:local
```

Build and load the image into kind with:

```bash
uv run --project tools/olf --locked olf images build project-code
uv run --project tools/olf --locked olf infra images load project-code
```

The image contains Dagster, the Floe Python core (`floe-python`) with
`dagster-floe`, dlt extract code, the Floe project under
`lakehouse_code/silver/`, the Gold dbt project, domain Python code, and shared
OpenLakeForge libraries. The code location builds the Floe manifest in memory
from the project and the EnvironmentProfile `openlakeforge_runtime/floe_profile.py` renders from
the stage's contract values, so the image carries no generated manifest and no
stage binding for Floe. Floe itself runs in one Kubernetes Job per domain on
this same image; dagster-floe passes that profile to the Job as text, so the
Job runs plain `floe`. See [Floe validation](../../docs/architecture/floe-validation.md).
Floe's merge write modes run through DuckDB, which loads its `httpfs`, `aws`,
`iceberg` and `avro` extensions on first use and would download them from
extensions.duckdb.org. Floe Job pods have no egress, so the build installs them
for the locked DuckDB into the image user's `~/.duckdb/extensions/`, then
`duckdb_extensions.py verify` loads them with downloads disabled; a
missing extension fails the build. `olf check project-code` runs the same
install and verify against its own environment.
The image runs as the non-root user `olf` (UID and GID 10001). It owns
`DAGSTER_HOME` and `lakehouse_code/` (where dbt writes `target/` and `logs/`);
the installed packages are root-owned. Every pod that runs the image pins the
same UID with `runAsNonRoot`, `allowPrivilegeEscalation: false` and all
capabilities dropped. A derived image that needs root for a build step must
switch back with `USER 10001:10001`.
The base image is configurable with `PROJECT_CODE_PYTHON_BASE_IMAGE`; AWS
builds default this to `public.ecr.aws/docker/library/python:3.12-slim` to avoid
Docker Hub during `olf infra deploy --provider aws --phase artifacts`.
dbt is bound the same way. The Docker build renders one `profiles.yml` from
`openlakeforge_runtime/dbt/profiles/trino.yml` into `generated/dbt-profiles/` under
`OPENLAKEFORGE_PROJECT_ROOT` (`/opt/openlakeforge`), outside `lakehouse_code/`. Every connection value in it is an `env_var()` reference
that the run pod's stage environment fills. The runtime package is installed
with `pip`, and sets `OPENLAKEFORGE_DBT_PACKAGES_DIR` to its installed `dbt/`
directory for every dbt invocation, through which every product's
`packages.yml` reaches the shared dbt package. The build also bakes each
product's `target/manifest.json` with placeholder catalog and schema names.
Dagster reads that manifest for the asset graph, and each run re-parses with
its own stage's values, so one image serves every stage and provider.
