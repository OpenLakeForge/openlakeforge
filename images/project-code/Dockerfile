ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DAGSTER_HOME=/opt/dagster/dagster_home

WORKDIR /opt/openlakeforge

COPY images/project-code/pyproject.toml images/project-code/requirements.lock ./
COPY packages/domain-model ./packages/domain-model

# The lock contains the complete transitive set.  Avoid resolver re-evaluation
# of upstream metadata ranges (for example SQLAlchemy's greenlet constraint).
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock
RUN pip install --no-cache-dir --no-build-isolation --no-deps ./packages/domain-model

# Floe's merge write modes load DuckDB extensions, which DuckDB would otherwise
# download at run time; Floe Job pods have no egress (#725). Installed here for
# the locked DuckDB, into the image user's default extension directory, and
# loaded once with downloads disabled so a missing one fails the build.
COPY libs/__init__.py libs/duckdb_extensions.py ./libs/
RUN python -m libs.duckdb_extensions install && python -m libs.duckdb_extensions verify

COPY lakehouse_code ./lakehouse_code
COPY libs ./libs

# setuptools is pinned in requirements.lock, so re-use it instead of creating a
# network-backed build environment for each provider image.
RUN pip install --no-cache-dir --no-build-isolation --no-deps .

# packages.yml reaches the shared dbt package through this variable, so a
# product resolves it the same way here, in olf's checks and in a customer
# repository with no libs/ beside lakehouse_code/.
ENV OPENLAKEFORGE_DBT_PACKAGES_DIR=/opt/openlakeforge/libs/dbt

# One dbt profile for every stage and provider, made only of env_var()
# references the pod's stage environment fills at run time, plus a manifest per
# product parsed once with placeholder values (libs/dbt/render_profiles.py), so
# code-server pods neither render nor parse on rollout.
RUN python -m libs.dbt.render_profiles --bake

RUN mkdir -p "${DAGSTER_HOME}"

CMD ["dagster", "api", "grpc", "--module-name", "lakehouse_code.definitions", "--host", "0.0.0.0", "--port", "3030"]
