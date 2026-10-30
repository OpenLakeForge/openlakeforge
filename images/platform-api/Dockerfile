ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_BASE_IMAGE}

# The package runs from source on PYTHONPATH: installing it would need
# setuptools, which the slim base does not ship, for no gain.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/openlakeforge

WORKDIR /opt/openlakeforge

COPY images/platform-api/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.lock && rm /tmp/requirements.lock

COPY packages/platform-api/openlakeforge_platform_api ./openlakeforge_platform_api

# nobody, as the pod's securityContext also requires; the service writes
# nothing, so the root filesystem is mounted read-only.
USER 65534:65534
EXPOSE 8080

CMD ["uvicorn", "--factory", "openlakeforge_platform_api.app:main", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
