ARG SUPERSET_BASE_IMAGE=apache/superset:6.1.0@sha256:fb3464528ec7076f91195f0ff7835755aa023e281f1bb78a84782ce7a36b3705
FROM ${SUPERSET_BASE_IMAGE}

USER root

RUN uv pip install --python /app/.venv/bin/python --no-cache \
    "authlib>=1.3,<2" \
    "psycopg2-binary>=2.9,<3" \
    "trino>=0.336,<1"

COPY patches/patch_trino_iceberg_partitions.py /tmp/openlakeforge/patch_trino_iceberg_partitions.py
RUN /app/.venv/bin/python /tmp/openlakeforge/patch_trino_iceberg_partitions.py \
    && rm /tmp/openlakeforge/patch_trino_iceberg_partitions.py

COPY olf_oidc.py olf_security.py /app/olf/

USER superset
