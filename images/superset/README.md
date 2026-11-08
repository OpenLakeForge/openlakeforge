# Superset Image

`images/superset/` is the source of OpenLakeForge's Superset image. The release
workflow builds, signs and publishes it by digest, and `olf infra deploy --provider
local` pulls the digest pinned as `superset` in `release/component-catalog.yaml`
(the same reference sits in `infra/helm/values/local/superset.yaml`; `olf release
check` keeps them equal).

The published image is amd64 only. On any other Docker engine (an Apple Silicon
workstation, say) `olf infra deploy` builds it from this directory instead. Maintainers
changing this directory do the same on any engine:

```bash
BUILD_SUPERSET_IMAGE=true uv run --project tools/olf --locked olf infra deploy --provider local
```

That tags the build `ghcr.io/openlakeforge/superset:local-<image id>` and loads
it into kind, so a changed image rolls the pods. `olf images build superset`
and `olf infra images load superset` do the same two steps without a deploy.

After a release publishes the change, copy the `superset` digest from that
release's `component-manifest.json` (`resolved_images`) into the catalog and
the values file.

It extends the official Superset image with the Trino Python driver so Superset
can query product Gold Iceberg marts through Trino.
The base image is configurable with `SUPERSET_BASE_IMAGE`; build scripts pull it
with retries before running `docker build`.

The image also patches Superset's Trino engine spec for local Iceberg previews.
Superset otherwise treats Trino Iceberg `$partitions` metadata columns as table
partition columns and generates invalid SQL Lab preview filters.

`olf_trino.py` sends a signed-in user's Dex token to Trino (ADR 0019 section 9):
a `DB_CONNECTION_MUTATOR` that sets the `token` extra credential and the email
user, and a patch of Superset's refresh so a refresh token Dex refuses ends in a
new sign-in. `openlakeforge_oidc` wires both, with per-user result caching, when
people's access is on. A database acts on it only when it is imported with
`impersonate_user` and a Trino URI, so a stack without one behaves as before.
The `trino` client is pinned exactly because the mutator relies on how it
handles `http_session` and `extra_credential`.
