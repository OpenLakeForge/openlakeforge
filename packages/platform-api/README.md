# Platform API

The backend the portal, `olf`'s platform commands and agents share
([ADR 0021](../../docs/adr/0021-platform-api.md)). It serves `/api/healthz`,
`/api/v1/whoami` and the read endpoints below.

| Route | Reads | Identity on the service call |
| --- | --- | --- |
| `GET /api/v1/stages/{stage}/jobs` | The stage's Dagster jobs, without Dagster's implicit asset jobs | None: Dagster checks nothing, so the matrix row is the only check |
| `GET /api/v1/stages/{stage}/runs?job=&limit=` | The stage's most recent runs, newest first; `limit` 1 to 100, default 20 | As above |
| `GET /api/v1/stages/{stage}/runs/{run_id}` | One run's status and times | As above |
| `GET /api/v1/stages/{stage}/logs?date=&hour=&pod=&container=&limit=` | The stage's archived pod log lines (`libs/k8s_log_archive.py`) for a day or one hour of it, oldest archive first; `limit` 1 to 5000, default 500, and `truncated` when it was reached | The API's read-only object-store key for the log prefix, from `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; the matrix row decides who reads |
| `GET /api/v1/domains` | OpenMetadata domains | The caller's bearer token, forwarded unchanged; OpenMetadata authorizes by the person's own roles |
| `GET /api/v1/data-products` | OpenMetadata data products and their domains | As above |

A service that refuses the forwarded token answers 401 or 403, passed through;
a service that cannot be reached or answers with an error is a 502 whose
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
| `openmetadata_url` | Optional; OpenMetadata's in-cluster base URL. Without it the OpenMetadata routes answer 404 |
| `log_archives` | Optional; each enabled stage's archive, `{"base_uri": "s3://…", "namespace": "…"}`: where its archiver writes and the namespace it archives. A stage without one answers 404 on the logs route; the namespace is never taken from a request |
| `s3_endpoint_url`, `s3_region` | The object store the archives are read from; the endpoint is null for AWS S3, and the region defaults to `us-east-1` |

The API refuses to start on a settings file or matrix that would weaken a
check: no audience, a role outside `precedence`, a matrix row with an unknown
role.

## Run the tests

```bash
uv run --project packages/platform-api --locked pytest packages/platform-api/tests
uv run --project packages/platform-api --locked mypy --config-file packages/platform-api/pyproject.toml \
  packages/platform-api/openlakeforge_platform_api packages/platform-api/tests
```
