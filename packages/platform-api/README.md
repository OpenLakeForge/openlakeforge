# Platform API

The backend the portal, `olf`'s platform commands and agents share
([ADR 0021](../../docs/adr/0021-platform-api.md)). It serves `/api/healthz`
and `/api/v1/whoami` and nothing else yet.

Every route has one row in `openlakeforge_platform_api/permissions.toml`: the
lowest canonical role allowed and the stages the operation applies to. A route
with no row is refused. Identity comes from the `Authorization: Bearer` token
alone, checked against the issuer's published keys for signature, issuer,
expiry and an audience from the configured list; the perimeter's
`X-Auth-Request-*` headers are never read.

## Settings

One JSON file, at `$OPENLAKEFORGE_PLATFORM_API_SETTINGS` (default
`/config/settings.json`), rendered from the `identity.oidc` contract:

| Field | Meaning |
| --- | --- |
| `issuer_url` | The issuer; its `/.well-known/openid-configuration` must name the same issuer |
| `audiences` | Client ids whose tokens are accepted; at least one |
| `role_claim`, `role_mapping` | As in `identity.oidc` (ADR 0014): the claim, and canonical role to claim values |
| `precedence` | Canonical roles, highest first (`release/identity-roles.yaml`) |
| `stages` | Enabled stage names |
| `ca_file` | Optional CA bundle added to the system roots for the issuer's TLS |

The API refuses to start on a settings file or matrix that would weaken a
check: no audience, a role outside `precedence`, a matrix row with an unknown
role.

## Run the tests

```bash
uv run --project packages/platform-api --locked pytest packages/platform-api/tests
uv run --project packages/platform-api --locked mypy --config-file packages/platform-api/pyproject.toml \
  packages/platform-api/openlakeforge_platform_api packages/platform-api/tests
```
