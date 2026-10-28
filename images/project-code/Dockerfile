ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_BASE_IMAGE}
ARG DBT_PROFILE_ENV=local

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

COPY lakehouse_code ./lakehouse_code
COPY libs ./libs

# setuptools is pinned in requirements.lock, so re-use it instead of creating a
# network-backed build environment for each provider image.
RUN pip install --no-cache-dir --no-build-isolation --no-deps .

# Render environment-specific dbt profiles, then pre-generate dbt manifests with
# the lightweight local target so code-server pods do not parse on every rollout.
RUN set -e; \
    python -m libs.dbt.render_profiles --environment "${DBT_PROFILE_ENV}" --write; \
    for project_dir in $(find lakehouse_code/gold -path "*/dbt/dbt_project.yml" -type f -exec dirname {} \; | sort); do \
      dbt deps --project-dir "${project_dir}"; \
      dbt parse --project-dir "${project_dir}" --profiles-dir "${project_dir}" --target local; \
      echo "dbt manifest baked at ${project_dir}/target/manifest.json"; \
    done

RUN mkdir -p "${DAGSTER_HOME}"

CMD ["dagster", "api", "grpc", "--module-name", "lakehouse_code.definitions", "--host", "0.0.0.0", "--port", "3030"]
