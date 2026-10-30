# Platform API

The backend the portal, `olf`'s platform commands and agents share
([ADR 0021](../../docs/adr/0021-platform-api.md)). It serves `/api/healthz`,
`/api/v1/whoami` and the read endpoints below.

| Route | Reads | Identity on the service call |
| --- | --- | --- |
| `GET /api/v1/stages/{stage}/jobs` | The stage's Dagster jobs, without Dagster's implicit asset jobs | None: Dagster checks nothing, so the matrix row is the only check |
| `GET /api/v1/stages/{stage}/runs?job=&limit=` | The stage's most recent runs, newest first; `limit` 1 to 100, default 20 | As above |
| `GET /api/v1/stages/{stage}/runs/{run_id}` | One run's status and times | As above |

A service that cannot be reached or answers with an error is a 502 whose
detail names the service and nothing it said.

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
| `dagster_graphql_urls` | Optional; each enabled stage's Dagster GraphQL URL. A stage without one answers 404 on the Dagster routes |

The API refuses to start on a settings file or matrix that would weaken a
check: no audience, a role outside `precedence`, a matrix row with an unknown
role.

## Run the tests

```bash
uv run --project packages/platform-api --locked pytest packages/platform-api/tests
uv run --project packages/platform-api --locked mypy --config-file packages/platform-api/pyproject.toml \
  packages/platform-api/openlakeforge_platform_api packages/platform-api/tests
```
