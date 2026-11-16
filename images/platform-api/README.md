# Platform API Image

`images/platform-api/` is the image of the platform API
([ADR 0021](../../docs/adr/0021-platform-api.md)), whose source is
`packages/platform-api/`. It runs the package, and the service clients it
imports from `packages/lakehouse-services/` and `packages/core/`, from source on `PYTHONPATH`
under uvicorn on port 8080, as `nobody`, and writes nothing, so the pod can
mount its root filesystem read-only.

The release workflow builds, signs and publishes it by digest with the other
first-party images. No release has published it yet, so
`release/component-catalog.yaml` holds no pin for it. Until one does,
`olf infra deploy --provider local` builds it and loads it into kind whenever
the profile has `spec.identity`, as it builds Superset on a non-amd64 engine.
The same two steps by hand:

```bash
uv run --project tools/olf --locked olf images build platform-api
uv run --project tools/olf --locked olf infra images load platform-api
```

After the first release that publishes it, copy the `platform-api` digest from
that release's `component-manifest.json` (`resolved_images`) into the catalog,
and deploy the pin instead of a local build.

`requirements.lock` is exported from `packages/platform-api/uv.lock`; regenerate
it whenever that lock changes. The first-party path dependencies are left out,
since their source is copied into the image and has no hash to pin:

```bash
uv export --project packages/platform-api --frozen --no-dev --no-emit-project \
  --no-emit-package openlakeforge-lakehouse-services --no-emit-package openlakeforge-core \
  --format requirements-txt -o images/platform-api/requirements.lock
```

## Alert evaluator entrypoint

The same image runs the alert evaluator (ADR 0023): the CronJob overrides the command with
`python -m openlakeforge_platform_api.alerting`. It reads its settings from `/config/alerts.json`
(or `OPENLAKEFORGE_ALERTS_SETTINGS`), each receiver URL from the environment variable the settings name, and the
ops-bucket key from `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. It exits 1 when the bucket cannot be read or
written, 2 when its settings or environment are wrong, and 0 otherwise, including when only a delivery failed.
`--now` and `--state-prefix` exist for the e2e; the CronJob never passes them.
