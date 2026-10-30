# Platform API Image

`images/platform-api/` is the image of the platform API
([ADR 0021](../../docs/adr/0021-platform-api.md)), whose source is
`packages/platform-api/`. It runs the package from source on `PYTHONPATH`
under uvicorn on port 8080, as `nobody`, and writes nothing, so the pod can
mount its root filesystem read-only.

The release workflow builds, signs and publishes it by digest with the other
first-party images. No release has published it yet, so
`release/component-catalog.yaml` holds no pin for it. Until one does, it is
built and loaded into kind locally, as Superset is on a non-amd64 engine:

```bash
uv run --project tools/olf --locked olf images build platform-api
uv run --project tools/olf --locked olf infra images load platform-api
```

After the first release that publishes it, copy the `platform-api` digest from
that release's `component-manifest.json` (`resolved_images`) into the catalog,
and deploy the pin instead of a local build.

`requirements.lock` is exported from `packages/platform-api/uv.lock`; regenerate
it whenever that lock changes:

```bash
uv export --project packages/platform-api --frozen --no-dev --no-emit-project \
  --format requirements-txt -o images/platform-api/requirements.lock
```
