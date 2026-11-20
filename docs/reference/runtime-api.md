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
| `openlakeforge_runtime.code_location` | `ProductOverrides`, `build_code_location`, `product_specs` |
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

## `build_code_location` and `ProductOverrides`

`lakehouse_code/pipelines/definitions.py` is the code location. It calls
`build_code_location(PRODUCTS)`, which builds the Bronze, Silver and
maintenance definitions and one job per product in `governance.yml`.
`PRODUCTS` in `pipelines/products.py` maps a product id to a
`ProductOverrides(silver_inputs, bronze_inputs, gold_assets, cadence)` and
needs an entry only for a product that departs from the defaults below. An
entry for a product `governance.yml` does not declare raises `ValueError`.

```python
PRODUCTS = {
    "order_revenue": ProductOverrides(cadence=ProductCadence("0 2 * * *", enabled_in_prod=True)),
}
```

## `ProductDefinitionSpec`

`build_code_location` builds one spec per product from its descriptor entry
and its overrides; you write one yourself only to call
`build_product_definitions` directly. `domain` and `product` are required. The
three lists default to what the
project declares, and a list you pass wins over its default:

| Field | Default |
| --- | --- |
| `gold_assets` | The models under `gold/models/<product>/` in the Gold dbt project |
| `silver_inputs` | The domain's Silver tables those models read; a source of another domain is refused |
| `bronze_inputs` | The Bronze resource each of `silver_inputs` reads, from the domain's Floe entities |
| `cadence` | Daily at 03:00 UTC, stopped, as `<product>_daily` |

`cadence` is a `ProductCadence(cron, timezone="UTC", enabled_in_prod=False)`
and sets the PROD schedule, named `<product>_schedule`.
`ProductCadence(cron=None)` declares a product with no schedule. DEV and UAT
never get one.

`enabled_in_prod` sets the PROD schedule's default status. `False` ships it
`STOPPED`; `True` ships it `RUNNING`, so deploying the revision to PROD starts
it. `cron=None` with `enabled_in_prod=True` raises `ValueError`. It is a
default, not a lock: a status someone sets in the Dagster UI is stored in the
stage's metadata database and wins over the code until it is reset with
Dagster's "reset to default schedule status" action. A schedule switched on in
the UI keeps running when Git says off, and one switched on and then off in the
UI stays off when `enabled_in_prod=True` is deployed later. The health
`expected_run` signal flags the second case as a `warning` (a `schedule_mismatch`);
see [health signals](health-signals.md). The first case is not flagged.

`olf check project-code` builds the specs from `PRODUCTS`, loads the
code location's schedules for each stage and fails on any in DEV, UAT or an
unset stage, and on any in PROD other than the product's cadence schedule (job,
cron and default status as the cadence declares) and the maintenance
schedules. A `RUNNING` default is accepted only when `enabled_in_prod` is
`True`, and a schedule the cadence declares `RUNNING` must be among those PROD
loads. It also fails on any sensor the code location defines, in any stage,
whatever its default status: the cadence is the only declared way to start
runs. Dagster's implicit `default_automation_condition_sensor` is added when
the repository is built, not defined in the code, so assets with automation
conditions do not trip it; an `AutomationConditionSensorDefinition` you define
yourself does.

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
