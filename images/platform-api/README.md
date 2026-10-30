# Platform API Image

`images/platform-api/` is the image of the platform API
([ADR 0021](../../docs/adr/0021-platform-api.md)), whose source is
`packages/platform-api/`. It runs the package, and the service clients it
imports from `packages/lakehouse-services/` and `packages/domain-model/`, from source on `PYTHONPATH`
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
  --no-emit-package openlakeforge-lakehouse-services --no-emit-package openlakeforge-domain-model \
  --format requirements-txt -o images/platform-api/requirements.lock
```
