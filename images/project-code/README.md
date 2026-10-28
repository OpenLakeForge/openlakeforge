# Project Code Image

`images/project-code/` is the custom runtime image boundary for OpenLakeForge
domain Dagster code.

The image is built locally as:

```text
ghcr.io/openlakeforge/project-code:local
```

Build and load the image into kind with:

```bash
uv run --project tools/olf --locked olf images build project-code
uv run --project tools/olf --locked olf images load project-code
```

The image contains Dagster, the Floe Python core (`floe-python`) with
`dagster-floe`, dlt extract code, the Floe project under
`lakehouse_code/silver/`, product dbt projects, domain Python code, and shared
OpenLakeForge libraries. Floe runs in-process in the Dagster run pod: the code
location builds the Floe manifest in memory from the project and the
EnvironmentProfile `libs/floe_profile.py` renders from the stage's contract
values, so the image carries no generated manifest and no stage binding for
Floe. See [Floe validation](../../docs/architecture/floe-validation.md).
The base image is configurable with `PROJECT_CODE_PYTHON_BASE_IMAGE`; AWS
builds default this to `public.ecr.aws/docker/library/python:3.12-slim` to avoid
Docker Hub during `olf deploy --provider aws --phase artifacts`.
The dbt profile rendered into the image is selected with
`PROJECT_CODE_DBT_PROFILE_ENV` (`local`, `azure`, or `aws`). The Docker build
renders `libs/dbt/profiles/<env>.yml` into each product project before baking
dbt manifests.
