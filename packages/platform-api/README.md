# Platform API

The backend the portal, `olf`'s platform commands and agents share
([ADR 0021](../../docs/adr/0021-platform-api.md)). It serves `/api/healthz`,
`/api/v1/whoami` and the read endpoints below.

| Route | Reads | Identity on the service call |
| --- | --- | --- |
| `GET /api/v1/stages/{stage}/jobs` | The stage's Dagster jobs, without Dagster's implicit asset jobs | None: Dagster checks nothing, so the matrix row is the only check |
| `GET /api/v1/stages/{stage}/runs?job=&limit=` | The stage's most recent runs, newest first; `limit` 1 to 100, default 20 | As above |
| `GET /api/v1/stages/{stage}/runs/{run_id}` | One run's status and times | As above |
| `GET /api/v1/stages/{stage}/runs/{run_id}/quality` | Every check of the run's quality manifest (ADR 0023), never re-run: engine (`floe`, `dbt`), check id, table, column, the engine's severity, `outcome` (`passed`, `warn`, `error`, `not_run`, `unknown`), `pipeline_outcome` (`continued`, `quarantined`, `blocked`, `unknown`), and the counts the engine recorded (`rows`, `accepted_rows`, `rejected_rows`, `violations` from Floe, `failures` from dbt; `null` where it recorded none); the manifest's `complete` flag and outcome `counts`; `reports`, one per report the run retained, each with an `href` to the route below or `null` where the caller may not download it or the API holds no files key; `runbook`, the alert runbook's `#quality` section when a check ended in `error`, else its `#run_failed` section for a failed run, and `null` otherwise or without a configured `runbook_base_url`. A run that wrote no manifest, a job that is not a product pipeline or an unreadable manifest has `checks: null` and `counts: null`, with `quality` `not_provided` or `unavailable` saying which; never an empty list or zeros. The read is held to 20 seconds | As the runs routes; the manifest with the stage's quality reader key |
| `GET /api/v1/stages/{stage}/runs/{run_id}/quality/reports/{report_id}` | One report the run's quality manifest names (a Floe entity's run report, dbt's `run_results.json`), streamed as a JSON attachment. The id comes from the route above, never an object key; a report outside the stage's activation prefix, an id the manifest does not name or a deleted report is a 404. `data-engineer` on DEV only, as for logs | The files key, from `OPENLAKEFORGE_FILES_ACCESS_KEY_ID` and `OPENLAKEFORGE_FILES_SECRET_ACCESS_KEY`; 503 without it |
| `GET /api/v1/stages/{stage}/health` | A rollup (`healthy`, `warning`, `critical`, `unknown`, `disabled`) of the stage's pipeline signals, each pipeline's state, `services` (one signal per probed component, folded into the state) and `integrations`: Dagster `available`, `disabled` (none configured) or `unavailable` (unreadable), and `services` `disabled` where the stage has no `stage_namespaces` entry; `quality` is `not_provided` until provided, and `metrics` is `disabled` (the profile turns metrics off), `not_provided` (enabled, but the provider has no backend), `unavailable` (the backend did not answer a query within 5 seconds) or `available`; neither is ever healthy, and `metrics` never changes the state. The probes run on worker threads; a probe that fails is `unknown` | As the runs routes; evaluated by `openlakeforge_core.health` |
| `GET /api/v1/stages/{stage}/health/services` | The stage and the `services` list of the route above, from the probes alone: no Dagster read, so it answers within the probes' timeout (5 seconds) however Dagster behaves. The alert evaluator's route, and the only one its service principal may call (ADR 0021, decision 3) | As above, plus the `alert-evaluator` service principal |
| `GET /api/v1/stages/{stage}/health/pipelines` | Per Dagster job: latest run, last success, failed runs among the last 25, schedule, freshness (age of the last success) and the evaluated signals; no run tags. 404 where the stage has no Dagster. The thresholds used are in `thresholds` and are provisional: runs older than 7 days read `unknown`, an expected run is critical 15 minutes after the schedule's cron last came due (from its cron and timezone, never from a tick), and unknown when nothing shows the schedule was on then | As above |
| `GET /api/v1/stages/{stage}/health/quality` | Per product pipeline: the run whose quality manifest decided its `quality` signal, that signal's `state` and `reason`, the manifest's `complete` flag and its results counted by outcome (`passed`, `warn`, `error`, `not_run`, `unknown`, and `blocking`, the errors that blocked the pipeline), with `totals` summed over the pipelines whose counts are known. Read in the same pass as the route above, so `state` is its `quality` signal. A missing, unreadable or foreign manifest has `counts: null`, never zeros; no reader is `quality` `not_provided` and an unreadable Dagster `unavailable`, each with `totals: null`. The whole read is held to 20 seconds and past that is Dagster `unavailable`. No check ids, tables or evidence pointers; 404 where the stage has no Dagster | As above; manifests with the stage's quality reader key |
| `GET /api/v1/stages/{stage}/alerts/incidents` | The stage's open alert incidents from the evaluator's `alerts/state/v1/state.json` (ADR 0023, decision 3): id, condition, severity, domain, product, table, component, times, summary, run ids, `runbook` (the alert runbook's section for the condition, as the notification links it; `null` without `runbook_base_url`), and `delivery` of the latest notification, `pending` (queued), `delivered`, `undeliverable` (expired or rejected), `unrouted` or `unknown` (settled before outcomes were kept), with attempt, receiver and acceptance counts and `targets`, each receiver by its profile name with its status (`delivered`, `rejected`, `retrying`, `expired`), attempts and `url` (the GitHub discussion; `null` for a webhook). `state` is `not_provided` without `alerts_state_base_uri` or the key, and `unavailable` with `incidents: null` when the state is missing, unreadable, unparsable or not read within 10 seconds; `stale` when the last evaluation is over 10 minutes old. Never a webhook URL or header, and never a write | The alert state key, from `OPENLAKEFORGE_ALERTS_ACCESS_KEY_ID` and `OPENLAKEFORGE_ALERTS_SECRET_ACCESS_KEY`: `Read` and `List` on `alerts/state/*` |
| `GET /api/v1/stages/{stage}/logs?date=&hour=&pod=&container=&run_id=&product=&component=&severity=&since=&until=&limit=` | The stage's archived pod log lines, the collector's v2 records and the polling archiver's v1 ones (`openlakeforge_runtime/k8s_log_archive.py`) with duplicates dropped (ADR 0022), for a UTC day or one hour of it, oldest first; `run_id`, `product` and `component` match v2 records only; `severity` is a floor on the line's `level`; `limit` 1 to 5000, default 500, and `truncated` when it was reached. Each line carries `timestamp`, when it was written as the source recorded it (v2's CRI time, the kubelet timestamp a v1 line starts with), and `level` (`debug`, `info`, `warning`, `error`, `critical`) when the message states one in a shape `level_of` in `openlakeforge_services/logs.py` recognises: a JSON object's `level`, `severity`, `levelname`, `log.level` or `lvl`; an upper-case level word opening the message, or a bracketed one in any case, after an optional timestamp; Python logging's `<time> - <logger> - <LEVEL> - `; a top-level `level=` or `lvl=` of a line that parses as logfmt whole (one inside a quoted value does not count). Either is null when the source recorded none, never guessed. Both log routes read on the same two workers: a request that waits more than 5 s for one is answered 503 with `Retry-After: 5`, the read is held to 20 seconds from when a worker takes it (502 past it), and a read whose client disconnects stops at its next listing page, object or record (`openlakeforge_platform_api/log_reads.py`) | The API's read-only object-store key for the log prefix, from `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; the matrix row decides who reads |
| `GET /api/v1/stages/{stage}/runs/{run_id}/logs?pod=&container=&component=&severity=&match_run=&since=&until=&after=&limit=` | The same lines over one run's window: from its start (its creation, before it has started) to its end (now, while it runs), read from Dagster as `runs/{run_id}` does, across every UTC day the window touches, so a run that crosses midnight returns both days in one ordering. The window is the run's, so other workloads' lines of the stage in it are included; `match_run=true` keeps only v2 records tagged with the run. `since` and `until` narrow the window; at most 3 UTC days of event time are read per call. v1 objects are partitioned by when the archiver ran, up to its one-hour lookback (`V1_LOOKBACK_SECONDS` in `openlakeforge_core/log_archive.py`) after their lines, so v1 is also listed on the day that lookback reaches past the window's end; lines are still selected by their own time. `truncated` is set when the limit or the day cut left lines out, and then `next` is an opaque cursor: pass it back as `after`, with the same filters, to continue exactly past the last line, lines sharing its timestamp included. `since` and `until` name the window that was read. Run read and archive read take one log read worker and share its 20-second deadline (502 past it), with `list_logs`'s 503 and disconnect handling; the archive read checks the deadline between pages, objects and records and stops there | Dagster with no credential, then the log key, as above; the matrix row is `list_logs`'s |
| `GET /api/v1/stages/{stage}/metrics` | The stage's series in the metrics catalog: name, unit, source, labels, the ranges the retention allows, and `state` with a `reason`. A Prometheus series is `available` while one of its scrape targets is up (one `up` query), a Dagster series while the stage's Dagster answers | Prometheus and Dagster, as below |
| `GET /api/v1/stages/{stage}/metrics/{series}?range=` | One named series of the catalog (`openlakeforge_core.metrics`) for the stage, `range` one of `1h`, `6h`, `24h`, `7d`, one point per step, a step without a sample `null`. Prometheus series read the `olf:<series>` recording rule; `pipeline_runs` and `pipeline_run_duration_seconds` are derived from Dagster runs and work without Prometheus. `state` is `disabled`, `not_provided` or `unavailable` (backend down, no samples, or every target down for the range) with no lines, never zeros. A range past the metrics retention is a 400; a series outside the catalog or the stage scope a 404 ([ADR 0024](../../docs/adr/0024-metrics-prometheus-reference-and-read-path.md), decision 4) | Prometheus with no credential, over F45; Dagster as the runs routes |
| `GET /api/v1/platform/metrics/{series}?range=` | As above for a platform series: the nodes and the `olf-system` components, `stage: null` | Prometheus, as above |
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
| `issuer_url` | The issuer; its `/.well-known/openid-configuration` must name the same issuer. Unset with `anonymous_role` |
| `jwks_url` | Optional. Where the issuer's keys are fetched, instead of discovery at `issuer_url`; `iss` is still compared with `issuer_url` as a string |
| `audiences` | Client ids whose tokens are accepted; at least one, none with `anonymous_role` |
| `anonymous_role` | Optional. Authentication off (ADR 0021, decision 8): a call with no token is the principal `anonymous` with this canonical role. Needs a `public_origin` on `*.localhost` and no issuer |
| `role_claim`, `role_mapping` | As in `identity.oidc` (ADR 0014): the claim, and canonical role to claim values |
| `precedence` | Canonical roles, highest first (`release/identity-roles.yaml`) |
| `stages` | Enabled stage names |
| `ca_file` | Optional CA bundle added to the system roots for the issuer's TLS |
| `dagster_graphql_urls` | Optional; each enabled stage's Dagster GraphQL URL. A stage without one answers 404 on the Dagster routes |
| `stage_namespaces`, `trino_urls` | Optional; each probed stage's namespace (where its Role and the pod's token exist) and its Trino coordinator base URL. A stage with no namespace is not probed |
| `polaris_health_url` | Optional; Polaris's management base URL. The probe reads `/q/health/ready` with no credential; absent, the Polaris signal is `disabled` |
| `openmetadata_url` | Optional; OpenMetadata's in-cluster base URL. Without it the OpenMetadata routes answer 404 |
| `log_archives` | Optional; each enabled stage's archive, `{"base_uri": "s3://…", "namespace": "…"}`: where its archiver writes and the namespace it archives. A stage without one answers 404 on the logs route; the namespace is never taken from a request |
| `platform_log_archive` | Optional; the shared platform namespace's archive, in the shape of one `log_archives` entry. Without it `GET /api/v1/platform/logs` answers 404 |
| `log_archive_retention_days` | Optional; the contract's `logs.archive.retention_days` when its `retention_supported` is true, else null. The monitoring capabilities state it on each stage with an archive; a value below 1 refuses the start |
| `person_query`, `query_endpoints` | Per-person SQL: the switch, and each stage's Trino `{"statement_url", "catalog"}` |
| `query_page_rows`, `query_export_max_rows`, `query_timeout_seconds`, `query_wait_seconds` | Optional bounds on SQL: rows per response (1000), rows per export (100000), seconds per Trino call (30) and per request's wait for a first result (5) |
| `metrics_enabled`, `metrics_url`, `metrics_retention_days` | Optional; the profile's resolved `metrics.enabled`, the contract's `metrics.query_url` (a `prometheus-http-v1` base URL) and `retention_days` (default 7). A URL without `metrics_enabled` refuses the start ([ADR 0024](../../docs/adr/0024-metrics-prometheus-reference-and-read-path.md)) |
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
