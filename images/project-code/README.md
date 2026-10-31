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
`lakehouse_code/silver/`, product dbt projects, domain Python code, and shared
OpenLakeForge libraries. The code location builds the Floe manifest in memory
from the project and the EnvironmentProfile `libs/floe_profile.py` renders from
the stage's contract values, so the image carries no generated manifest and no
stage binding for Floe. Floe itself runs in one Kubernetes Job per domain on
this same image; dagster-floe passes that profile to the Job as text, so the
Job runs plain `floe`. See [Floe validation](../../docs/architecture/floe-validation.md).
Floe's merge write modes run through DuckDB, which loads its `httpfs`, `aws`,
`iceberg` and `avro` extensions on first use and would download them from
extensions.duckdb.org. Floe Job pods have no egress, so the build installs them
for the locked DuckDB into the image user's `~/.duckdb/extensions/`, then
`python -m libs.duckdb_extensions verify` loads them with downloads disabled; a
missing extension fails the build. `olf check project-code` runs the same
install and verify against its own environment.
The base image is configurable with `PROJECT_CODE_PYTHON_BASE_IMAGE`; AWS
builds default this to `public.ecr.aws/docker/library/python:3.12-slim` to avoid
Docker Hub during `olf infra deploy --provider aws --phase artifacts`.
dbt is bound the same way. The Docker build renders one `profiles.yml` from
`libs/dbt/profiles/trino.yml` into `generated/dbt-profiles/`, outside
`lakehouse_code/`. Every connection value in it is an `env_var()` reference
that the run pod's stage environment fills. The build also bakes each
product's `target/manifest.json` with placeholder catalog and schema names.
Dagster reads that manifest for the asset graph, and each run re-parses with
its own stage's values, so one image serves every stage and provider.
