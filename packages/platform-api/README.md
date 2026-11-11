# Platform API

The backend the portal, `olf`'s platform commands and agents share
([ADR 0021](../../docs/adr/0021-platform-api.md)). It serves `/api/healthz`,
`/api/v1/whoami` and the read endpoints below.

| Route | Reads | Identity on the service call |
| --- | --- | --- |
| `GET /api/v1/stages/{stage}/jobs` | The stage's Dagster jobs, without Dagster's implicit asset jobs | None: Dagster checks nothing, so the matrix row is the only check |
| `GET /api/v1/stages/{stage}/runs?job=&limit=` | The stage's most recent runs, newest first; `limit` 1 to 100, default 20 | As above |
| `GET /api/v1/stages/{stage}/runs/{run_id}` | One run's status and times | As above |
| `GET /api/v1/stages/{stage}/logs?date=&hour=&pod=&container=&limit=` | The stage's archived pod log lines (`openlakeforge_runtime/k8s_log_archive.py`) for a day or one hour of it, oldest archive first; `limit` 1 to 5000, default 500, and `truncated` when it was reached | The API's read-only object-store key for the log prefix, from `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; the matrix row decides who reads |
| `GET /api/v1/query-capabilities` | The stages that offer SQL (engine `trino`, export formats); empty unless `person_query` is on and `query_endpoints` names the stage | None: settings only, no host or secret in the answer |
| `GET /api/v1/stages/{stage}/schemas?q=&cursor=&limit=` | Schemas visible to the caller in the stage's catalog, name order; `limit` 1 to 200, default 50; `next_cursor` is opaque | The caller's token as Trino bearer and Polaris credential; Polaris decides. 403 on a denial, 502 when Trino fails, 404 where the stage offers no query |
| `GET /api/v1/stages/{stage}/schemas/{schema}/tables?q=&cursor=&limit=` | Tables and views of one schema, as above | As above |
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
| `jwks_url` | Optional. Where the issuer's keys are fetched, instead of discovery at `issuer_url`; `iss` is still compared with `issuer_url` as a string |
| `audiences` | Client ids whose tokens are accepted; at least one |
| `role_claim`, `role_mapping` | As in `identity.oidc` (ADR 0014): the claim, and canonical role to claim values |
| `precedence` | Canonical roles, highest first (`release/identity-roles.yaml`) |
| `stages` | Enabled stage names |
| `ca_file` | Optional CA bundle added to the system roots for the issuer's TLS |
| `dagster_graphql_urls` | Optional; each enabled stage's Dagster GraphQL URL. A stage without one answers 404 on the Dagster routes |
| `openmetadata_url` | Optional; OpenMetadata's in-cluster base URL. Without it the OpenMetadata routes answer 404 |
| `log_archives` | Optional; each enabled stage's archive, `{"base_uri": "s3://…", "namespace": "…"}`: where its archiver writes and the namespace it archives. A stage without one answers 404 on the logs route; the namespace is never taken from a request |
| `person_query`, `query_endpoints` | Per-person SQL: the switch, and each stage's Trino `{"statement_url", "catalog"}` |
| `query_page_rows`, `query_export_max_rows`, `query_timeout_seconds`, `query_wait_seconds` | Optional bounds on SQL: rows per response (1000), rows per export (100000), seconds per Trino call (30) and per request's wait for a first result (5) |
| `s3_endpoint_url`, `s3_region` | The object store the archives are read from; the endpoint is null for AWS S3, and the region defaults to `us-east-1` |

Query ids and cursors are signed with the key in the environment variable
`OPENLAKEFORGE_QUERY_SIGNING_KEY`, mounted from a Secret; unset, or under 32
characters, the query routes answer 503.

The API refuses to start on a settings file or matrix that would weaken a
check: no audience, a role outside `precedence`, a matrix row with an unknown
role.

## Run the tests

```bash
uv run --project packages/platform-api --locked pytest packages/platform-api/tests
uv run --project packages/platform-api --locked mypy --config-file packages/platform-api/pyproject.toml \
  packages/platform-api/openlakeforge_platform_api packages/platform-api/tests
```

After changing a route or a response model, regenerate the committed OpenAPI
document; `tests/test_openapi.py` fails until you do:

```bash
cd packages/platform-api && uv run --locked python -m openlakeforge_platform_api.openapi
```
