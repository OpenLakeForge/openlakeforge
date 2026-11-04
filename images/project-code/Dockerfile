ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DAGSTER_HOME=/opt/dagster/dagster_home

WORKDIR /opt/openlakeforge

COPY images/project-code/pyproject.toml images/project-code/requirements.lock ./

COPY packages/core ./packages/core

# The lock contains the complete transitive set.  Avoid resolver re-evaluation
# of upstream metadata ranges (for example SQLAlchemy's greenlet constraint).
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock
RUN pip install --no-cache-dir --no-build-isolation --no-deps ./packages/core

# The optional project layer (lakehouse_code/requirements.txt) goes on top of the
# platform lock, constrained to its exact pins so it cannot change them. The glob
# makes the file optional: without it only the lock is copied and the image is
# unchanged. Placed before the runtime and the project code so its layer is cached
# across code edits. Unlike the platform lock it is not hash-verified.
COPY images/project-code/requirements.lock lakehouse_code/requirements.tx[t] ./project-layer/
RUN if [ -f project-layer/requirements.txt ]; then \
        sed -n 's/ \\$//; /^[A-Za-z0-9]/p' project-layer/requirements.lock > project-layer/constraints.txt \
        && pip install --no-cache-dir -c project-layer/constraints.txt -r project-layer/requirements.txt; \
    fi

# Floe's merge write modes load DuckDB extensions, which DuckDB would otherwise
# download at run time; Floe Job pods have no egress (#725). Installed here for
# the locked DuckDB, into the image user's default extension directory, and
# loaded once with downloads disabled so a missing one fails the build. The
# module is standard-library only, so it runs as a script from its own copy and
# this layer stays cached while the rest of the runtime package changes.
COPY packages/runtime/openlakeforge_runtime/duckdb_extensions.py /tmp/duckdb_extensions.py
RUN python /tmp/duckdb_extensions.py install && python /tmp/duckdb_extensions.py verify && rm /tmp/duckdb_extensions.py

# Its dependencies (Dagster, dlt, dbt, Floe) are all in requirements.lock.
COPY packages/runtime ./packages/runtime
RUN pip install --no-cache-dir --no-build-isolation --no-deps ./packages/runtime

COPY lakehouse_code ./lakehouse_code

# setuptools is pinned in requirements.lock, so re-use it instead of creating a
# network-backed build environment for each provider image.
RUN pip install --no-cache-dir --no-build-isolation --no-deps .

# The project root is explicit: the baked profile and manifests live under it,
# beside lakehouse_code/, not next to the installed runtime package.
ENV OPENLAKEFORGE_PROJECT_ROOT=/opt/openlakeforge

# packages.yml reaches the shared dbt package through
# OPENLAKEFORGE_DBT_PACKAGES_DIR, which the runtime sets to its installed
# location for every dbt invocation (render_profiles.dbt_environment).

# One dbt profile for every stage and provider, made only of env_var()
# references the pod's stage environment fills at run time, plus a manifest per
# product parsed once with placeholder values (openlakeforge_runtime/dbt/render_profiles.py), so
# code-server pods neither render nor parse on rollout.
RUN python -m openlakeforge_runtime.dbt.render_profiles --bake

RUN mkdir -p "${DAGSTER_HOME}"

CMD ["dagster", "api", "grpc", "--module-name", "lakehouse_code.definitions", "--host", "0.0.0.0", "--port", "3030"]
