# Runtime API

`openlakeforge_runtime` is the package your `lakehouse_code/` imports. Only the
modules and names below are public. Everything else in the package, such as
`floe_profile`, `s3_artifacts`, `gold_maintenance` and `dbt`, and any name that
starts with an underscore, is private: it can change in any release, including
a patch release.

`olf check project-code` prints a warning for every import in `lakehouse_code/`
of a module or name outside this page.

## Public modules

| Module | Public names |
| --- | --- |
| `openlakeforge_runtime.product_dagster` | `ProductDefinitionSpec`, `ProductCadence`, `ResolvedProductInputs`, `build_product_definitions`, `SourceDefinitionSpec`, `build_source_definitions`, `build_silver_definitions`, `build_maintenance_definitions`, `build_gold_maintenance_definitions`, `build_stage_schedules` |
| `openlakeforge_runtime.bronze_csv` | `BronzeLoadResult`, `load_entities_to_bronze`, `load_entity_to_bronze` |
| `openlakeforge_runtime.bronze_dlt` | `load_dlt_source` |
| `openlakeforge_runtime.bronze_incremental` | `CursorNotReached`, `keyset_batches`, `newest_first_pages`, `sql_engine` |

Each module lists its public names in `__all__`, and
`openlakeforge_runtime.PUBLIC_MODULES` lists the modules.

## Compatibility promise

While the project is in beta (`v1beta1`):

- A public name is not removed or given an incompatible signature in a patch or
  minor release.
- A name that has to go is first deprecated: it stays importable and keeps
  working for at least one minor release, and the release notes say what
  replaces it.
- A test (`tools/olf/tests/runtime/test_public_api.py`) holds the published
  names, so removing one fails the build.
- New public names and new optional arguments can appear in any release.

## `ProductDefinitionSpec`

`domain` and `product` are required. The three lists default to what the
project declares, and a list you pass wins over its default:

| Field | Default |
| --- | --- |
| `gold_assets` | The models under `gold/models/<product>/` in the Gold dbt project |
| `silver_inputs` | The domain's Silver tables those models read; a source of another domain is refused |
| `bronze_inputs` | The Bronze resource each of `silver_inputs` reads, from the domain's Floe entities |
| `cadence` | Daily at 03:00 UTC, as `<product>_daily` |

```python
spec = ProductDefinitionSpec(domain="sales", product="order_revenue")
defs = build_product_definitions(spec)
```

`cadence` is a `ProductCadence(cron, timezone="UTC")` and sets the PROD
schedule, named `<product>_schedule`. `ProductCadence(cron=None)` declares a
product with no schedule. DEV and UAT never get one, and the schedule is
`STOPPED` either way: switching it on stays a decision in the Dagster UI.
Keep the spec in a module-level `spec` name so `olf check project-code` checks
the cadence the product declares.

```python
spec = ProductDefinitionSpec(
    domain="sales", product="order_revenue", cadence=ProductCadence("0 * * * *")
)
defs = build_product_definitions(spec)
```

## Retries

Only the Bronze extract and load retries, and only inside the step: when it
raises a transient network error (a `ConnectionError` or `TimeoutError`, a
`requests` connection error, timeout or chunked-encoding error, or a botocore
connection, read-timeout, connection-closed or response-streaming error, also
as the cause dlt wraps in `PipelineStepFailed`), the loader runs again after
15 s, then 30 s, then 60 s (15 s doubling, at most 2 minutes), so 3 retries by
default. A TLS or proxy error (`ssl.SSLError`, or the `requests` and botocore SSL and proxy errors, which are
connection errors too) and any other error fail the step at once. An interrupt
(`KeyboardInterrupt`, `SystemExit` or Dagster's `DagsterExecutionInterruptedError`,
raised when a run is terminated) is never retried, so a terminated run stops.
The wait is a sleep in the run pod, so the run keeps its queue slot meanwhile.

`SourceDefinitionSpec(bronze_retry_attempts=...)` sets the number of retries
for a source; `0` turns them off.

Silver and Gold never retry. A Floe failure can be a deterministic `abort` or
contract failure that must stop the run, and a failed dbt build leaves a
half-built generation behind. The job carries no Dagster `RetryPolicy`,
because Dagster would also use it to restart a step that a terminated run
interrupted.

A dbt source that is not a Silver table of the domain fails when the
definitions load, naming the source. `ProductDefinitionSpec.resolve()` returns
the effective lists.
