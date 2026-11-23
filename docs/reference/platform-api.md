# Platform API

The HTTP API behind the portal, `olf`'s platform commands and agents. The
decisions it implements are [ADR 0021](../adr/0021-platform-api.md); this page
lists its routes, the permission row each one has, and what each route does
beyond its schema.

Two files are the source, and win over this page when they differ:

- `docs/schema/platform-api.openapi.json`: every path, parameter, request body
  and response model. `packages/platform-api/tests/test_openapi.py` fails when
  it differs from the app; regenerate it with
  `cd packages/platform-api && uv run --locked python -m openlakeforge_platform_api.openapi`.
  Its `version` is the path version, `v1`, not the release.
- `packages/platform-api/openlakeforge_platform_api/permissions.toml`: one row
  per route. `tests/test_permissions.py` fails when a route has no row or a row
  no route.

The API serves no `/openapi.json`, `/docs` or `/redoc`: each would be a route
outside the matrix.

## Calling it

The API is served at `https://<base_domain>/api/`, on the portal's host, behind
the perimeter's lowest role floor (`http` under `access.tls: none`). Send the
person's token as `Authorization: Bearer <token>`; `olf` sends the stored
session's token, and a browser session reaches it through the perimeter, which
passes the session's `olf-cli` ID token on to `/api` only.

Every request goes through the same checks, in this order:

| Check | Refused with |
| --- | --- |
| The route has a row in `permissions.toml` | 403, before any token is read |
| A POST, PUT, PATCH or DELETE is not cross-site (below) | 403 |
| A `{stage}` in the path is an enabled stage | 404 |
| The bearer token validates: signature against the issuer's keys, issuer, expiry, and an audience in the settings' `audiences` | 401 with `WWW-Authenticate: Bearer error="invalid_token"`; 503 when the issuer's keys cannot be fetched |
| The token carries a recognised role | 403 "no OpenLakeForge role assigned" |
| The row admits the role on that stage | 403 naming the role it needs, or the stages the role is limited to |

`GET /api/healthz` is the one row with role `anonymous`: it takes no token and
reports the process only, without calling the issuer.

### Cross-site writes

On POST, PUT, PATCH and DELETE the API refuses an `Origin` other than the
settings' `public_origin` (unset: any `Origin`), a `Sec-Fetch-Site` other than
`same-origin` or `none`, and a body whose type is not `application/json`. Two
routes take another type: `import_report` takes `application/zip` and
`upload_object` `application/octet-stream`. A request with no `Origin` is a
non-browser client and passes. The API serves no CORS headers.
(`openlakeforge_platform_api/csrf.py`)

### The service principal

One caller is not a person: the alert evaluator's CronJob, `alert-evaluator`
([ADR 0023](../adr/0023-alert-evaluation-and-webhook-delivery.md), decision 11).
It sends a projected ServiceAccount token for the audience
`openlakeforge-platform-api`, valid ten minutes. A bearer token whose unverified
`iss` is not the issuer's goes to a Kubernetes TokenReview, made with the API's
own ServiceAccount; it is accepted only if the API server authenticates it for
that audience and names exactly a ServiceAccount listed in the settings'
`service_callers` (`system:serviceaccount:<namespace>:<name>`).

| Outcome | Status |
| --- | --- |
| Invalid token, wrong audience, or another ServiceAccount | 401 |
| A ServiceAccount the route's row does not list in `services` | 403 |
| The API server cannot answer the review | 503; never a fallback to the issuer's path or to the anonymous principal |

The token is never forwarded to a service, and the issuer's tokens are never
sent to the API server. The only row that lists `alert-evaluator` is
`get_stage_service_health`. (`openlakeforge_platform_api/service_tokens.py`)

### Authentication off

On a local profile with `spec.access` and no `spec.identity`, the API is
deployed with no issuer and `anonymous_role` set to the highest role. A request
with no token is the principal `anonymous` with that role, and the matrix
applies unchanged; a request with an issuer token is refused with 401. The API
refuses to start with `anonymous_role` beside `issuer_url` or `audiences`, or
with a `public_origin` that is not on `*.localhost`. Writes are audited as
`anonymous`. The OpenMetadata routes are not configured there.

## Permission rows

Each row in `permissions.toml` has:

| Key | Meaning |
| --- | --- |
| `role` | The lowest canonical role allowed; every role above it in `release/identity-roles.yaml`'s `precedence` is allowed too. `anonymous` takes no token |
| `stages` | The stages the route applies to; `*` is every enabled stage. Empty: the route names no stage, and a stage in its path is refused |
| `role_stages` | Optional. Limits a named role to some of `stages`, for example `{ data-engineer = ["dev"] }`; a role it does not name keeps `stages` |
| `services` | Optional. Service principals that may call the route as well. Only a read may name one |
| `changes_state` | Optional. The route is audited (below). Every route that is not a GET has it, except `submit_query` and `cancel_query` |

The matrix is checked when it loads: a role outside `precedence`, an unknown
key or a malformed `role_stages` stops the API from starting.

## Routes

36 paths and 39 operations. Paths are under `/api/v1` except `healthz`. The
stage column reads "every" for `*`, and a role in parentheses is limited to
the stages given.

| Method | Path | Row | Lowest role | Stages | |
| --- | --- | --- | --- | --- | --- |
| `GET` | `/api/healthz` | `healthz` | `anonymous` | — | |
| `GET` | `/whoami` | `whoami` | `viewer` | — | |
| `GET` | `/stages/{stage}/jobs` | `list_jobs` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/runs` | `list_runs` | `data-engineer` | every | |
| `POST` | `/stages/{stage}/runs` | `launch_run` | `data-engineer` | dev | audited |
| `GET` | `/stages/{stage}/runs/{run_id}` | `get_run` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/runs/{run_id}/failure` | `get_run_failure` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/runs/{run_id}/quality` | `get_run_quality` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/runs/{run_id}/quality/reports/{report_id}` | `get_run_quality_report` | `data-engineer` | every (`data-engineer`: dev) | |
| `GET` | `/stages/{stage}/runs/{run_id}/logs` | `list_run_logs` | `data-engineer` | every (`data-engineer`: dev) | |
| `GET` | `/stages/{stage}/logs` | `list_logs` | `data-engineer` | every (`data-engineer`: dev) | |
| `GET` | `/stages/{stage}/health` | `get_stage_health` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/health/services` | `get_stage_service_health` | `data-engineer` | every | also `alert-evaluator` |
| `GET` | `/stages/{stage}/health/pipelines` | `list_pipeline_health` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/health/quality` | `get_stage_quality_health` | `data-engineer` | every | |
| `GET` | `/monitoring-capabilities` | `monitoring_capabilities` | `viewer` | — | |
| `GET` | `/stages/{stage}/alerts/incidents` | `list_alert_incidents` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/metrics` | `list_stage_metrics` | `data-engineer` | every | |
| `GET` | `/stages/{stage}/metrics/{series}` | `get_stage_metric` | `data-engineer` | every | |
| `GET` | `/platform/metrics/{series}` | `get_platform_metric` | `data-engineer` | — | |
| `GET` | `/domains` | `list_domains` | `viewer` | — | |
| `GET` | `/data-products` | `list_data_products` | `viewer` | — | |
| `POST` | `/stages/{stage}/reports` | `import_report` | `data-engineer` | dev | audited |
| `GET` | `/stages/{stage}/reports/{dashboard}/export` | `export_report` | `data-engineer` | every | |
| `GET` | `/query-capabilities` | `query_capabilities` | `viewer` | — | |
| `GET` | `/stages/{stage}/schemas` | `list_schemas` | `viewer` | every | |
| `GET` | `/stages/{stage}/schemas/{schema}/tables` | `list_tables` | `viewer` | every | |
| `POST` | `/stages/{stage}/queries` | `submit_query` | `viewer` | every | |
| `GET` | `/stages/{stage}/queries/{query_id}` | `get_query` | `viewer` | every | |
| `DELETE` | `/stages/{stage}/queries/{query_id}` | `cancel_query` | `viewer` | every | |
| `GET` | `/stages/{stage}/queries/{query_id}/export` | `export_query` | `viewer` | every | |
| `GET` | `/file-capabilities` | `file_capabilities` | `viewer` | — | |
| `GET` | `/stages/{stage}/buckets` | `list_buckets` | `data-engineer` | every (`data-engineer`: dev) | |
| `GET` | `/stages/{stage}/buckets/{bucket}/objects` | `list_objects` | `data-engineer` | every (`data-engineer`: dev) | |
| `GET` | `/stages/{stage}/buckets/{bucket}/objects/content` | `download_object` | `data-engineer` | every (`data-engineer`: dev) | |
| `PUT` | `/stages/{stage}/buckets/{bucket}/objects/content` | `upload_object` | `data-engineer` | every (`data-engineer`: dev) | audited |
| `POST` | `/stages/{stage}/buckets/{bucket}/folders` | `create_folder` | `data-engineer` | every (`data-engineer`: dev) | audited |
| `POST` | `/stages/{stage}/buckets/{bucket}/objects/rename` | `rename_object` | `data-engineer` | every (`data-engineer`: dev) | audited |
| `POST` | `/stages/{stage}/buckets/{bucket}/objects/delete` | `delete_objects` | `data-engineer` | every (`data-engineer`: dev) | audited |

Why each floor is where it is is commented on its row in `permissions.toml`.
In short: Dagster reads sit at the Dagster UI's floor, so the API shows no one
a run the perimeter would not let them open; pod logs and quality reports can
quote data a run read, so a `data-engineer` reads them on DEV only; the
OpenMetadata, discovery and query rows sit at `viewer` because the service
behind them decides per person; and the files explorer is closed to `analyst`
and `viewer`, reads included.

## What each service call carries

| Routes | Service | Credential on the call |
| --- | --- | --- |
| `list_domains`, `list_data_products` | OpenMetadata | The caller's token, forwarded unchanged |
| Discovery and queries | Each stage's Trino, through Polaris | The caller's token, as the `token` extra credential |
| Jobs, runs, launch, run failure, pipeline health | Each stage's Dagster GraphQL API | None: Dagster checks nothing |
| `import_report`, `export_report` | Each stage's Superset | The stage's Superset service user |
| `list_logs`, `list_run_logs` | Object store | The log reader key |
| `get_run_quality`, `get_stage_quality_health`, the health routes' quality signal | Object store | The stage's quality reader key |
| `list_alert_incidents` | Object store | The alert state key |
| The files explorer, `get_run_quality_report`, the object-store probe | Object store | The files key |
| Audit records | Object store | The audit key |
| Metrics routes, the health routes' `metrics` integration | Prometheus | None |
| Health probes | Kubernetes API, in each stage namespace | The API's ServiceAccount token, `list` only |

### Object-store keys

Each key is its own SeaweedFS identity, minted in-cluster by the credentials
Job and mounted from its own Secret (`infra/terraform/modules/storage/seaweedfs/credentials.tf`).
Each has its own S3 client in the API; none is used for another's purpose.

| Key | Identity | Grant |
| --- | --- | --- |
| Log reader | `openlakeforge-platform-api-logs` | `Read`, `List` on each stage's log archive prefix |
| Quality reader, one per stage | `openlakeforge-platform-api-quality-<stage>` | `Read`, `List` on `activations/<stage>/run-artifacts/quality/` of the ops bucket |
| Alert state | `openlakeforge-platform-api-alerts` | `Read`, `List` on the alert evaluator's `alerts/state/` |
| Audit | `openlakeforge-platform-api-audit` | `Write` on `audit/platform-api/` only |
| Files | `openlakeforge-platform-api-files` | `Read`, `Write`, `List` on the enabled stages' layer buckets and the ops bucket |

SeaweedFS gates overwrite and delete on `Write`, so the audit key and the files
key can both delete under their grant. A missing key leaves the routes that
need it unavailable (503, or a source reported as not provided) and never stops
the API from starting.

## Audit

Every route with `changes_state` runs inside the audit
(`openlakeforge_platform_api/audit.py`):

- Before it acts, the API stores an `attempted` record. If that write fails, it
  answers 503 and does nothing. The outcome (`succeeded` or `failed`) follows;
  a lost outcome is logged, never undone.
- A record names the token's subject, email and canonical role, the operation,
  the stage, its target, and the outcome. The request id in it is the API's
  own, never the client's.
- Each record is one JSON line on stdout and one object under
  `audit/platform-api/date=<day>/` in the ops bucket.
- A refusal on such a route is stored too once the caller has a principal. A
  refusal of a caller with no valid token goes to stdout only, so no anonymous
  caller can fill the bucket.
- The records are tamper-evident against everyone but the API, not
  append-only: the audit key's `Write` also overwrites and deletes.

Read-only routes, the queries included, are not audited, and the API logs
nothing about a query's SQL or a log search's text.

## Pipelines and runs

`list_jobs`, `list_runs` and `get_run` read the stage's Dagster. Each job
carries the `domain` and `product` it loads, from its job definition's tags
(`openlakeforge/silver-domain`, `openlakeforge/product`), or `null` for a job
of no product.

`launch_run` launches one of the stage's jobs as the signed-in person. The job
is looked up among the jobs `list_jobs` returns: an unknown name is 404, and a
name two code locations both define is 409, naming them. The run is tagged
`openlakeforge/launched-by` (the person's email, or subject) and
`openlakeforge/launch-key` (the API's request id), by which a launch whose
response was lost can be found rather than launched twice. `olf runs launch`
does not retry it.

`get_run_failure` answers at most five failed steps in event order
(`steps_truncated` past them), each with its step key, the innermost cause's
class and the first line of its message, redacted and cut to 300 characters;
the run's own failure message when no step failed; and the rejected and
accepted rows from the run's quality manifest. It never selects a stack trace.
The redactor (`openlakeforge_services.redact`) removes URL userinfo and
queries, Bearer and Basic credentials, `key=value` secrets and access-key
lookalikes. An unreadable event log gives `steps: null` and an absent manifest
`rows: null`, never an empty list or zeros.

`get_run_quality` lists every check in the run's quality manifest, its
`complete` flag and outcome counts, the reports it names and a `runbook` link
(`#quality` when a check ended in `error`, else `#run_failed` for a failed run,
`null` otherwise or without `runbook_base_url`). Without a usable manifest,
`checks` and `counts` are `null`. `get_run_quality_report` streams one of those
reports with the files key, as a JSON attachment. Its id is derived from the
manifest's `artifact_uri`, never an object key from the client, and a URI
outside the stage's activation prefix is 404. A report's `href` is `null` where
the caller may not download it or the API holds no files key; no presigned URL
is handed out.

`get_run_failure`, `get_run_quality` and `get_run_quality_report` are each held
to a 20-second deadline.

## Logs

`list_logs` reads a stage's archived pod log lines; `list_run_logs` reads those
of one run's start-to-end window, across at most three UTC days of event time,
paged by an opaque `next` cursor passed back as `after` (a cursor the route did
not return is 422). Both take `severity`, a floor on each line's `level`, and
`q`, a case-insensitive substring of the message (1 to 200 characters; a
control character is 422 before anything is read). `limit`, `truncated` and
`next` count matching lines only, so a sparse search can answer fewer lines
than `limit`, or none, with `next` set. The archive format is
[ADR 0022](../adr/0022-pod-log-collection-and-archive-format.md)'s.

Both routes read the archive on one shared pool of two workers
(`openlakeforge_platform_api/log_reads.py`):

| Situation | Answer |
| --- | --- |
| No worker frees up within 5 seconds | 503 with `Retry-After: 5`; nothing was read |
| The read passes 20 seconds, counted from when a worker took it | 502 |
| The client disconnects | The read is cancelled: a queued read never starts, a running one stops at its next check |
| The API holds no log reader key | 503 without `Retry-After` |
| The stage has no archive | 404 |

A request therefore takes at most 25 seconds. The archive reader checks the
deadline and the cancellation between listing pages, objects and records, and
starts none of its queued fetches once cancelled.

## Health

The health routes return states in the vocabulary of
[Health signals](health-signals.md), which also lists the probe components.

- `get_stage_health` and `list_pipeline_health` take `range`, one of `1h`,
  `6h`, `24h` and `7d` (default `24h`; anything else is 400). The range scopes
  `failed_runs` only. `state`, the signals and `services` are the current
  evaluation, read from each job's 25 newest runs. Where those runs do not
  reach back to the window's start, one stage-wide read fills the count, capped
  at 500 runs; a read that reaches the cap sets `window.covered_from` to where
  the count starts. A Dagster that cannot be read gives `failed_runs: null`,
  never 0.
- `get_stage_service_health` is the `services` half of `get_stage_health`, from
  the probes alone, with no Dagster read, so it answers within the probes'
  5-second timeout while Dagster hangs. It is the alert evaluator's route.
- `get_stage_quality_health` answers, per pipeline, the outcome counts of the
  manifest behind its `quality` signal and their sum, held to a 20-second
  deadline. A pipeline whose latest run ended before its checks finished reads
  `unknown` with that run's partial counts, never an older run's result.
- `monitoring_capabilities` lists the stages and routes the caller's role may
  use for monitoring, and each source's configuration; see
  [Monitoring capabilities](health-signals.md#monitoring-capabilities).

Each row of `services` (`ServiceHealth`) carries `component`, the stable key,
and `display_name`, `service` and `service_display_name` from one map in
`openlakeforge_platform_api/service_probes.py`. `service` is an enum in the
OpenAPI document; renaming a display name is a reviewed change, no name carries
a host, namespace or container, and a test fails when a component the probes
can emit has no name. `last_success_at` is set only by
`log-archive.newest-object` and `quality-sync.publication`, and is null
whenever the row is `unknown` or `disabled`; it is never the probe time.

The probes find Dagster's and Trino's controllers by label
(`app.kubernetes.io/name=dagster` and `=trino`, overridable per stage); a
selector that matches nothing is `unknown`. Restarts are counted over the last
30 minutes, never from the lifetime `restartCount`.

## Alerts and metrics

`list_alert_incidents` answers the stage's open incidents from the alert
evaluator's state, with each one's latest delivery, from payload-allowlist
fields and counts only, and a `runbook` link built from `runbook_base_url`. A
missing or unreadable state is `unavailable` with no incidents, and a state
older than two evaluator runs (10 minutes) is `stale`. The read is held to 10
seconds; each incident's `job`, `domain` and `product` are `null` when Dagster
cannot be read in time. There is no acknowledgement route
([ADR 0023](../adr/0023-alert-evaluation-and-webhook-delivery.md)).

`list_stage_metrics`, `get_stage_metric` and `get_platform_metric` serve only
the named series of [Metrics](metrics.md) over its fixed ranges. No route takes
a query, start, end or step; an unknown series is 404 and an unknown range 400.
Each read is held to 5 seconds.

## OpenMetadata

`list_domains` and `list_data_products` forward the caller's token unchanged;
OpenMetadata decides what each person sees by their own OpenMetadata roles.
The API validates the token's audience first, since OpenMetadata checks only
signature and expiry.

A person OpenMetadata has never seen is answered 404 "user instance for <name>
not found". The API then registers exactly that user with the caller's own
token (`POST /api/v1/users`, which self-signup allows) and reads again. The
name is the one OpenMetadata derives: `preferred_username`, taken before any
`@` and lowercased, read from the validated token. OpenMetadata refuses a
self-signup under any other name, so a mismatch fails closed. The role sync
assigns the new user's roles on its next run. Registration is not audited: it
is the side effect a first browser sign-in has, not an operation the person
asked for.

## Superset reports

Both routes act as the stage's Superset service user, `olf-platform-api` with
role `olf_platform_api`, which signs in with Superset's database provider. The
role is not Admin: it may import assets, read and write datasets, charts and
dashboards, export dashboards, and read databases; it has no SQL Lab, no role
management and no write on databases. The artifacts phase makes the user a
co-owner of every report asset it imports, so the user may overwrite them, and
grants the role the report schemas, so it may export them
(`infra/terraform/modules/analytics/superset/main.tf`,
`openlakeforge_services.reports`).

- `import_report` takes a report directory zipped from its root, at most 10
  MiB (413 past it, before anything is attempted). The upload's databases are
  dropped and the import runs sparse, so a dataset resolves to a connection the
  stage already has and an upload cannot replace one. A report that reads a
  schema the report roles were not granted is 409: only the artifacts phase
  grants. DEV only, because the user co-owns every report asset.
- `export_report` returns one dashboard, by uuid or slug, as a stage-neutral
  zip bundle.

A stage with no Superset is 404; a stage whose service-user password the API
does not hold is 503.

## Queries

`query_capabilities` lists the stages that offer queries and their export
formats, naming no host; without a signing key it lists none. `list_schemas` and `list_tables` run as the person,
with fully qualified names.

`submit_query`, `get_query`, `cancel_query` and `export_query` run the
person's SQL on the stage's Trino, as the person:

- A statement runs only if Trino accepts it as a table expression in
  `SELECT * FROM (<sql>)`, checked with `EXPLAIN (TYPE VALIDATE)`, which runs
  nothing. SHOW, DESCRIBE, EXPLAIN and every write are 403.
- A submit may name `schema` (printable ASCII, at most 128 characters, else
  400); every Trino call for that query, the export's re-run included, sends it
  as `X-Trino-Schema`.
- The query id and cursor are HMAC-signed records of Trino's query id and
  continuation, the stage, the schema and the token's `sub`, so any replica
  resumes a query and no one resumes another person's; one that does not
  verify is 404. The key is `OPENLAKEFORGE_QUERY_SIGNING_KEY`, mounted from a
  Secret, at least 32 characters; without it query execution answers 503.
- A response carries at most `query_page_rows` rows (default 1000), and an
  export at most `query_export_max_rows` (default 100000), else 413. Trino gives
  a result out once, so an export runs the statement again. CSV cells a
  spreadsheet would read as formulas are prefixed with a quote.
- There is no per-person limit on queries in flight: it would need state shared
  between replicas.

## Files explorer

The files explorer reads and manages the objects in each stage's layer buckets
(bronze, silver, gold) and, for `platform-admin`, the ops bucket, with the
files key. The object store cannot tell one person from another, so the rows
above and the guards below are the only control
(`openlakeforge_platform_api/files.py`, `file_writes.py`).

**Who sees what.** `platform-admin` uses every enabled stage; `data-engineer`
only `dev`; `analyst` and `viewer` nothing, reads included. `file_capabilities`
returns the stages and operations the caller may use; an empty list is how a
client knows to hide its Files page. A bucket not among the stage's layer
buckets is 404, and so is the ops bucket for anyone below `platform-admin`,
who sees it under every stage. The buckets come from `file_buckets` and
`ops_bucket` in the settings, rendered from each stage's bucket bindings;
without them or the files key, every files route answers 503.

**Keys.** A key travels as a query parameter, and a folder is a prefix ending
in `/`. A key is 422 if it has a `.` or `..` segment, an empty segment (`//`),
a backslash, a leading `/`, a control character, invalid UTF-8 or more than
1024 bytes, because SeaweedFS would normalise it into another key (`audit//x`
into `audit/x`) and the audit-prefix guard must speak about the key the store
acts on. Listings are paged with a cursor, 200 entries by default and 1000 at
most.

**The audit prefix is read-only.** A write that reaches the audit records
(`audit_base_uri`, `audit/platform-api/` of the ops bucket), or a folder that
contains them, is 403, and the refusal is stored. Reads are allowed.

**Uploads.** Downloads and uploads are streamed, never held whole. An upload
needs a `Content-Length` (411) and is refused above `files_max_upload_bytes`,
1 GiB unless the settings say otherwise (413). Past one 8 MiB part it is a
multipart upload, aborted on any failure. It does not replace an existing key
unless `overwrite=true` (409): the final `PutObject` or
`CompleteMultipartUpload` carries `If-None-Match: *`, which SeaweedFS evaluates
under the object's write lock, so of two uploads racing for a new key one wins.
A request cancelled during an upload closes its audit record as `succeeded` if
the final write had landed and as `failed` otherwise.

**Renames and deletes.** S3 has no atomic rename, so a rename copies and then
deletes, and a folder moves or deletes at most 1000 keys (422 above that). Each
object write is conditional:

- the copy carries `If-None-Match: *` for the destination and
  `x-amz-copy-source-if-match` with the ETag the rename read from the source;
- the original is deleted with `If-Match` (per-key ETags in a folder's batch
  delete);
- a failed move removes the copies it made with each copy's own ETag, leaving
  any a different writer has since replaced.

A folder marker is a directory in SeaweedFS, which cannot be copied and has no
ETag, so a rename writes the destination marker with `If-None-Match: *`, and a
delete or rollback removes markers after what is under them, without an ETag.
A source that changed during the move stays, with 409. Only a multi-key folder
move is not atomic: a failure midway leaves the keys already deleted moved and
the rest in place, and a delete reports each key's outcome rather than failing
as a whole. The existence check before a rename or a new folder is a courtesy;
a folder is created by a plain `PutObject` of its marker, so two requests for
the same new folder can both succeed.

The conditional writes were checked against the SeaweedFS source of the
version `release/component-catalog.yaml` pins (4.54); a SeaweedFS upgrade
rechecks them.
