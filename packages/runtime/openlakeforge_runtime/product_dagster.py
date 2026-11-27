from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import floe
import lakehouse_code
from dagster import (
    AssetKey,
    AssetOut,
    AssetSelection,
    DagsterEventType,
    DagsterRunStatus,
    DefaultScheduleStatus,
    Definitions,
    Failure,
    Field,
    MetadataValue,
    Output,
    RunsFilter,
    ScheduleDefinition,
    define_asset_job,
    failure_hook,
    in_process_executor,
    job,
    multi_asset,
    op,
    success_hook,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, dbt_assets
from floe_dagster.assets import load_floe_assets
from floe_dagster.definitions import build_runner_from_env
from floe_dagster.manifest import ManifestEntity
from openlakeforge_core import full_refresh_scope, inventory_for
from openlakeforge_core.quality import MANIFEST_FILENAME, QualityManifest, QualityOutcome

from openlakeforge_runtime import gold_maintenance
from openlakeforge_runtime.bronze_csv import BronzeLoadResult
from openlakeforge_runtime.bronze_retry import DEFAULT_BRONZE_RETRY_ATTEMPTS, call_with_transient_retry
from openlakeforge_runtime.dbt.render_profiles import dbt_environment, ensure_baked_manifest, resolve_profiles_dir
from openlakeforge_runtime.dbt_build import (
    DbtTest,
    build_by_generation,
    dbt_tests,
    failed_nodes_in,
    generation_args,
    merge_run_results,
    model_dependencies,
    model_sources,
    upstream_models,
    without_deselected_checks,
)
from openlakeforge_runtime.dbt_quality import dbt_quality_manifest, read_dbt_evidence
from openlakeforge_runtime.floe_profile import write_profile
from openlakeforge_runtime.floe_quality import (
    FLOE_CHECK_IDS,
    FloeCheckEvidence,
    FloeEntityEvidence,
    floe_quality_manifest,
    read_entity_report,
)
from openlakeforge_runtime.openlakeforge_logging import log_event
from openlakeforge_runtime.polaris_token import GENERATION_PAUSE_TAG, TOKEN_MINTED_MESSAGE, polaris_token_env
from openlakeforge_runtime.s3_artifacts import read_json_uri, split_s3_uri, upload_file, upload_file_to_base_uri
from openlakeforge_runtime.source_bindings import source_binding_tags

__all__ = [
    "ProductDefinitionSpec",
    "ProductCadence",
    "ResolvedProductInputs",
    "SourceDefinitionSpec",
    "build_gold_maintenance_definitions",
    "build_maintenance_definitions",
    "build_product_definitions",
    "build_silver_definitions",
    "build_source_definitions",
    "build_stage_schedules",
]

# The project imported as `lakehouse_code`: beside openlakeforge_runtime/ in the image, but a
# separate root when olf checks a customer project against the distribution.
_LAKEHOUSE_CODE_ROOT = Path(lakehouse_code.__file__).resolve().parent
FLOE_PROJECT = _LAKEHOUSE_CODE_ROOT / "silver" / "floe.yml"
# Names the Floe manifest, and with it each domain's op: without a name the op
# name derives from the project's path and checksum, and every contract edit
# would rename it, cutting the op off from its run history.
_FLOE_MANIFEST_NAME = "openlakeforge"

# Every dbt command renders packages.yml, which names the shared package
# through this variable; a run's `dbt build` inherits it from this process.
for _name, _value in dbt_environment().items():
    os.environ.setdefault(_name, _value)

# Every product run carries its domain under this tag, and the Dagster run queue admits one run per value
# (infra/helm/values/local/dagster.yaml, `tagConcurrencyLimits`). Products of a domain share its Silver tables, and
# each run's Floe Job overwrites the ones it reads (`write_mode: overwrite`): two at once are two concurrent Iceberg
# commits to one table, where the loser fails or replaces the winner's snapshot. The limit is per domain rather than
# per table because Floe's Job is one per domain, and it holds however many products a domain has.
SILVER_DOMAIN_TAG = "openlakeforge/silver-domain"
# The product the job loads, beside its domain: the platform API maps a job to its domain and product from these two
# definition tags (#1272), so nothing parses the job name. A job without them belongs to no product.
PRODUCT_TAG = "openlakeforge/product"

_STAGE_ENV = "OPENLAKEFORGE_STAGE"
# Only PROD carries the recurring-schedule scaffold. DEV and UAT stay manual:
# the same immutable revision is deployed to every stage, so a schedule that
# merely defaulted to stopped would still be present in the DEV instance for
# someone to switch on, and the run it then launched would be a
# production-shaped daily workload against the DEV catalog.
_SCHEDULED_STAGES = frozenset({"prod"})
_DAILY_SCHEDULE_CRON = "0 3 * * *"
_MAINTENANCE_SCHEDULE_CRON = "0 4 * * 0"
# An hour after Silver so the two never run together.
_GOLD_MAINTENANCE_SCHEDULE_CRON = "0 5 * * 0"


@dataclass(frozen=True)
class ProductCadence:
    """When a product's PROD schedule fires; `cron=None` declares the product manual-only.

    A cadence does not switch the schedule on by itself: it ships `STOPPED` unless `enabled_in_prod` is set. The
    flag sets the schedule's default status, so the repository records whether PROD is meant to run. It is not a
    lock: a status set in the Dagster UI overrides it until it is reset with Dagster's "reset to default schedule
    status" action, so the health cross-check (#1148) should flag a mismatch.
    Dagster rejects an invalid cron or timezone when the definitions load, which fails `olf check project-code`.
    """

    cron: str | None = _DAILY_SCHEDULE_CRON
    timezone: str = "UTC"
    enabled_in_prod: bool = False

    def __post_init__(self) -> None:
        if self.enabled_in_prod and self.cron is None:
            raise ValueError("ProductCadence(cron=None) is manual-only, so it cannot set enabled_in_prod=True")


@dataclass(frozen=True)
class ResolvedProductInputs:
    """The assets a product's job selects, once the spec's omitted lists are filled from the project."""

    silver_inputs: tuple[str, ...]
    bronze_inputs: tuple[tuple[str, str], ...]
    gold_assets: tuple[str, ...]


@dataclass(frozen=True)
class ProductDefinitionSpec:
    """A product pipeline: `domain` and `product` are enough.

    Each list defaults to what the project already declares, and an explicit
    value wins over the default:

    - `gold_assets`: the models in the product's folder of the Gold dbt
      project (ADR 0004);
    - `silver_inputs`: the domain's Silver tables those models read;
    - `bronze_inputs`: the Bronze resource each of `silver_inputs` reads, from
      the domain's Floe entities;
    - `cadence`: the PROD schedule's cron and timezone, and whether it runs; absent means daily at 03:00 UTC, stopped.
    """

    domain: str
    product: str
    silver_inputs: tuple[str, ...] | None = None
    bronze_inputs: tuple[tuple[str, str], ...] | None = None
    gold_assets: tuple[str, ...] | None = None
    cadence: ProductCadence | None = None

    @property
    def job_name(self) -> str:
        return f"{self.product}_pipeline"

    @property
    def dbt_project_dir(self) -> Path:
        return _LAKEHOUSE_CODE_ROOT / "gold"

    @property
    def dbt_select(self) -> str:
        # A product folder is a dbt group (ADR 0004). dagster-dbt resolves a `path:` selector
        # without the project directory, so it would match nothing.
        return f"group:{self.product}"

    def resolve(self, manifest_path: Path | None = None) -> ResolvedProductInputs:
        """The effective inputs; `manifest_path` defaults to the Gold project's baked dbt manifest."""
        manifest: dict = {}
        if self.gold_assets is None or self.silver_inputs is None:
            path = manifest_path or ensure_baked_manifest(self.dbt_project_dir)
            if not path.is_file():
                raise ValueError(
                    f"product {self.product!r}: no dbt manifest at {path}; "
                    "pass gold_assets and silver_inputs explicitly or build the dbt project's manifest"
                )
            manifest = json.loads(path.read_text())
        models = _product_models(manifest, self.product)
        gold = self.gold_assets
        if gold is None:
            gold = tuple(sorted({model["name"] for model in models}))
            if not gold:
                raise ValueError(
                    f"product {self.product!r}: the Gold dbt project has no models under models/{self.product}/"
                )
        silver = self.silver_inputs
        if silver is None:
            # Empty for a product that reads only other products' public marts through ref().
            silver = self._read_silver_tables(manifest, models)
        bronze = self.bronze_inputs
        if bronze is None:
            bronze = self._floe_bronze_inputs(silver)
        return ResolvedProductInputs(silver_inputs=tuple(silver), bronze_inputs=tuple(bronze), gold_assets=tuple(gold))

    def _read_silver_tables(self, manifest: dict, models: list[dict]) -> tuple[str, ...]:
        """The Silver the product's build reads: its models' sources, and those of the tests that run with them.

        A test runs with the product when one of its parents is a product model (eager selection, as
        `dbt_build.dbt_tests` schedules it); a relationships test can read a source no model does.
        """
        sources = manifest.get("sources", {})
        model_ids = {model.get("unique_id") for model in models} - {None}
        tests = [
            node
            for node in manifest.get("nodes", {}).values()
            if node.get("resource_type") == "test" and model_ids & set(node.get("depends_on", {}).get("nodes", ()))
        ]
        read = [
            sources[parent]
            for node in (*models, *tests)
            for parent in node.get("depends_on", {}).get("nodes", ())
            if parent in sources
        ]
        foreign = sorted(
            {f"{source['source_name']}.{source['name']}" for source in read if source["source_name"] != self.domain}
        )
        if foreign:
            raise ValueError(
                f"product {self.product!r} of domain {self.domain!r} reads {foreign}: "
                "a product reads its own domain's Silver, or another product's public mart through ref()"
            )
        return tuple(sorted({source["name"] for source in read}))

    def _floe_bronze_inputs(self, silver: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        inventory = inventory_for(_LAKEHOUSE_CODE_ROOT)
        domain = next((candidate for candidate in inventory.domains if candidate.name == self.domain), None)
        if domain is None:
            raise ValueError(f"product {self.product!r}: the project declares no domain {self.domain!r}")
        entities = {entity.name: (entity.source, entity.resource) for entity in inventory.floe_entities(domain)}
        missing = [name for name in silver if name not in entities]
        if missing:
            raise ValueError(
                f"product {self.product!r}: dbt sources {missing} are not Floe entities of domain {self.domain!r}. "
                "A dbt source takes its Silver table's name; if the source is renamed (an `identifier:` override) "
                "or reads another table, pass silver_inputs explicitly"
            )
        return tuple(entities[name] for name in silver)


def _product_models(manifest: dict, product: str) -> list[dict]:
    """The project's own models in `models/<product>/`; a package's models are no product's.

    A model's fqn is the project name, then its folders under `models/`, then its name.
    """
    project = manifest.get("metadata", {}).get("project_name")
    return [
        node
        for node in manifest.get("nodes", {}).values()
        if node["resource_type"] == "model"
        and project in (None, node.get("package_name"))
        and len(node.get("fqn", ())) > 2
        and node["fqn"][1] == product
    ]


@dataclass(frozen=True)
class SourceDefinitionSpec:
    source: str
    resources: tuple[str, ...]
    bronze_loader: Callable[[tuple[str, ...]], dict[str, BronzeLoadResult]]
    # Retries of the loader after a transient network error (see `bronze_retry`); 0 turns them off.
    bronze_retry_attempts: int = DEFAULT_BRONZE_RETRY_ATTEMPTS


def build_source_definitions(spec: SourceDefinitionSpec) -> Definitions:
    @multi_asset(
        name=f"{spec.source}_bronze_resources",
        outs={
            resource: AssetOut(
                key=AssetKey([spec.source, resource]),
                # Subset jobs intentionally omit resources owned by the same
                # source. Dagster therefore must not require every output from
                # the shared source multi-asset on each product run.
                is_required=False,
            )
            for resource in spec.resources
        },
        group_name=spec.source,
        can_subset=True,
    )
    def bronze_resources(context):
        # A full refresh re-ingests the Bronze already landed and never
        # re-extracts, so the loader always runs incrementally. The tags are
        # still read here, before anything loads, so malformed ones fail the
        # run at its first step.
        full_refresh_scope(context.run.tags)
        previous_run_id = os.environ.get("DAGSTER_RUN_ID")
        os.environ["DAGSTER_RUN_ID"] = context.run_id
        try:
            selected = tuple(
                resource
                for resource in spec.resources
                if context.selected_output_names is None or resource in context.selected_output_names
            )
            results = call_with_transient_retry(
                lambda: spec.bronze_loader(selected),
                retries=spec.bronze_retry_attempts,
                on_retry=lambda retry, exc, delay: context.log.warning(
                    f"Bronze load of {spec.source} failed transiently ({type(exc).__name__}); "
                    f"retry {retry} of {spec.bronze_retry_attempts} in {delay:.0f} s"
                ),
            )
        finally:
            if previous_run_id is None:
                os.environ.pop("DAGSTER_RUN_ID", None)
            else:
                os.environ["DAGSTER_RUN_ID"] = previous_run_id
        for entity, result in results.items():
            log_event(
                context.log,
                message="Loaded Bronze source",
                service="dagster",
                tool="dlt",
                source=spec.source,
                dagster_run_id=context.run_id,
                entity=entity,
                asset_key=f"{spec.source}/{entity}",
                rows=result.rows,
                uri=result.uri,
            )
            yield Output(
                value=None,
                output_name=entity,
                metadata={
                    "rows": MetadataValue.int(result.rows),
                    "bronze_uri": MetadataValue.url(result.uri),
                    "source_file": MetadataValue.path(result.source_file),
                },
            )

    return Definitions(assets=[bronze_resources])


@cache
def _floe_profile() -> str:
    return str(write_profile())


def _bronze_resource(entity: ManifestEntity) -> AssetKey | None:
    # The per-resource asset Bronze lands, not dagster-floe's shared
    # `sources/<project>/<source>` key, so Silver waits for exactly its input.
    if entity.source_ref and entity.source_resource:
        return AssetKey([entity.source_ref, entity.source_resource])
    return None


def build_silver_definitions() -> Definitions:
    """Every Floe entity of the project, one subsettable asset per domain.

    Each domain's asset runs as one Kubernetes Job; a run that selects only
    some of a domain's tables passes just those entities to it. Each Silver
    table depends on the Bronze resource its entity reads (`source.ref`,
    `source.resource`).
    """
    return load_floe_assets(
        config_path=str(FLOE_PROJECT),
        profile_path=_floe_profile(),
        manifest_name=_FLOE_MANIFEST_NAME,
        runner=build_runner_from_env(),
        register_source_assets=False,
        source_key_fn=_bronze_resource,
    )


# Floe's own retention floor and orphan delay; `orphan_delay_days` must outlast the
# longest Floe Job, since younger unreferenced files may be an in-flight write's.
_MAINTENANCE_CONFIG = {
    "dry_run": Field(bool, default_value=False),
    "older_than_days": Field(int, default_value=7),
    "retain_last": Field(int, default_value=10),
    "orphan_delay_days": Field(int, default_value=3),
}


@op(config_schema=_MAINTENANCE_CONFIG)
def maintain_silver(context) -> None:
    config = context.op_config
    results = floe.maintain(
        str(FLOE_PROJECT),
        profile_path=_floe_profile(),
        older_than_days=config["older_than_days"],
        retain_last=config["retain_last"],
        orphan_delay_days=config["orphan_delay_days"],
        dry_run=config["dry_run"],
    )
    summary = {
        "dry_run": config["dry_run"],
        "expired_snapshots": sum(result["expired_snapshots"] for result in results),
        "orphan_files": sum(len(result["orphan_files"]) for result in results),
        "orphan_bytes": sum(result["orphan_bytes"] for result in results),
    }
    log_event(context.log, message="Maintained Silver tables", service="dagster", tool="floe", **summary)
    context.add_output_metadata({**summary, "tables": MetadataValue.json(results)})


# The same snapshot floor as Silver (Trino's expire_snapshots takes `retain_last`); the orphan
# delay must clear Trino's own 7-day minimum, not Floe's 3.
_GOLD_MAINTENANCE_CONFIG = {
    "dry_run": Field(bool, default_value=False),
    "older_than_days": Field(int, default_value=7),
    "retain_last": Field(int, default_value=10),
    "orphan_delay_days": Field(int, default_value=gold_maintenance.MIN_RETENTION_DAYS),
}


@op(config_schema=_GOLD_MAINTENANCE_CONFIG)
def maintain_gold(context) -> None:
    config = context.op_config
    gold_maintenance.check_retention(config["older_than_days"], config["orphan_delay_days"])
    schemas = gold_maintenance.gold_schemas(_LAKEHOUSE_CODE_ROOT / "gold")
    results = gold_maintenance.maintain_gold(
        gold_maintenance.connect(),
        schemas,
        older_than_days=config["older_than_days"],
        retain_last=config["retain_last"],
        orphan_delay_days=config["orphan_delay_days"],
        dry_run=config["dry_run"],
        log=lambda message, fields: log_event(context.log, message=message, service="dagster", tool="trino", **fields),
    )
    failed = [result for result in results if "error" in result]
    summary = {
        "dry_run": config["dry_run"],
        "tables": len(results),
        "failed_tables": len(failed),
        "snapshots_past_window": sum(result.get("snapshots_past_window", 0) for result in results),
    }
    log_event(context.log, message="Maintained Gold tables", service="dagster", tool="trino", **summary)
    context.add_output_metadata({**summary, "results": MetadataValue.json(results)})
    if failed:
        raise Failure(f"Gold maintenance failed for {len(failed)} of {len(results)} tables; rerun is safe")


@job(name="gold_maintenance")
def gold_maintenance_job():
    """Expire old snapshots and remove orphan files of the Gold tables dbt-trino writes, through Trino."""
    maintain_gold()


@job
def silver_maintenance():
    """Expire old snapshots and remove orphan files of the Silver tables Floe writes (not Gold: dbt-trino owns it)."""
    maintain_silver()


def build_maintenance_definitions(*, stage: str | None = None) -> Definitions:
    """The maintenance job on every stage, so a dry run can be launched anywhere; its schedule where `stage` allows.

    The schedule follows the daily scaffold's rule (`build_stage_schedules`):
    prod only, and `STOPPED` so deploying a revision never starts deletion.
    """
    resolved = (os.environ.get(_STAGE_ENV, "") if stage is None else stage).strip().lower()
    schedules = (
        [
            ScheduleDefinition(
                name="silver_maintenance_weekly",
                job=silver_maintenance,
                cron_schedule=_MAINTENANCE_SCHEDULE_CRON,
                execution_timezone="UTC",
                default_status=DefaultScheduleStatus.STOPPED,
            )
        ]
        if resolved in _SCHEDULED_STAGES
        else []
    )
    return Definitions(jobs=[silver_maintenance], schedules=schedules)


def build_gold_maintenance_definitions(*, stage: str | None = None) -> Definitions:
    """Silver's counterpart for Gold, with the same stage rule: the job everywhere, the STOPPED schedule on prod."""
    resolved = (os.environ.get(_STAGE_ENV, "") if stage is None else stage).strip().lower()
    schedules = (
        [
            ScheduleDefinition(
                name="gold_maintenance_weekly",
                job=gold_maintenance_job,
                cron_schedule=_GOLD_MAINTENANCE_SCHEDULE_CRON,
                execution_timezone="UTC",
                default_status=DefaultScheduleStatus.STOPPED,
            )
        ]
        if resolved in _SCHEDULED_STAGES
        else []
    )
    return Definitions(jobs=[gold_maintenance_job], schedules=schedules)


def build_product_definitions(spec: ProductDefinitionSpec) -> Definitions:
    class ProductDbtTranslator(DagsterDbtTranslator):
        def get_asset_key(self, dbt_resource_props) -> AssetKey:
            if dbt_resource_props["resource_type"] == "source":
                # A Gold source is named after its domain (ADR 0004), as is the Silver asset.
                return AssetKey([dbt_resource_props["source_name"], dbt_resource_props["name"]])
            # A model of the project is keyed by the product folder it sits in, so another
            # product's public mart this product refs keeps its owner's key.
            owner = dbt_resource_props["fqn"][1] if len(dbt_resource_props.get("fqn", ())) > 2 else spec.product
            return AssetKey([owner, dbt_resource_props["name"]])

        def get_group_name(self, dbt_resource_props) -> str:
            return spec.product

        def get_metadata(self, dbt_resource_props) -> dict:
            # The manifest is parsed once for every stage, with placeholder
            # catalog and schema names; the relation it names is no stage's.
            metadata = dict(super().get_metadata(dbt_resource_props))
            metadata.pop("dagster/table_name", None)
            return metadata

    def _dbt_gold_assets(context):
        dbt = DbtCliResource(
            project_dir=str(spec.dbt_project_dir),
            profiles_dir=str(resolve_profiles_dir()),
            dbt_executable=os.environ.get("OPENLAKEFORGE_DBT_EXECUTABLE", "dbt-ol"),
        )
        previous_job_name = os.environ.get("OPENLINEAGE_DBT_JOB_NAME")
        os.environ["OPENLINEAGE_DBT_JOB_NAME"] = spec.job_name
        # Both scopes rebuild Gold: after a Bronze reload, an incremental model
        # would otherwise keep rows its rebuilt inputs no longer hold.
        full_refresh = full_refresh_scope(context.run.tags) is not None
        target_paths: list[Path] = []
        selected = {key.path[-1] for key in context.selected_asset_keys}
        manifest = json.loads(manifest_path.read_text())
        dependencies = model_dependencies(manifest, selected)
        all_tests = without_deselected_checks(
            dbt_tests(manifest, selected),
            known={key.name for key in context.assets_def.check_keys},
            selected={key.name for key in context.selected_asset_check_keys},
        )
        explicit_tests = [test.unique_id for test in all_tests.values() if test.explicit]
        _require_upstream_products_ready(
            context.instance, spec.product, upstream_models(manifest, selected, spec.product, explicit_tests)
        )
        # Before the build, so a run that dies early still says which tests it meant to run (dbt_quality).
        _upload_selected_tests(context, spec, sorted(t.unique_id for t in all_tests.values() if t.explicit))

        def build_generation(models: list[str], tests: list[DbtTest]):
            args = [
                "build",
                *(["--full-refresh"] if full_refresh else []),
                *generation_args(models, tests, dependencies, all_tests.values()),
            ]
            invocation = dbt.cli(args, context=context)
            target_paths.append(invocation.target_path)
            yield from invocation.stream()

        @contextmanager
        def generation_token():
            with polaris_token_env():
                # The e2e reads these events' times: a token's life starts here (#1061).
                context.log.info(TOKEN_MINTED_MESSAGE)
                yield

        pause = context.run.tags.get(GENERATION_PAUSE_TAG)

        def between_generations() -> None:
            context.log.info(f"Pausing {pause}s before the next generation (e2e, #1061).")
            time.sleep(float(pause or 0))

        try:
            # A dbt process holds its token for good and Polaris lets it expire, so each generation gets its own.
            yield from _retaining_artifacts_on_failure(
                context,
                spec,
                target_paths,
                build_by_generation(
                    dependencies,
                    all_tests,
                    build_generation,
                    generation_token,
                    lambda models, tests: failed_nodes_in(target_paths[-1], models, tests),
                    sources=model_sources(manifest, selected),
                    between=None if pause is None else between_generations,
                ),
                baked_manifest=manifest_path,
            )
        finally:
            if previous_job_name is None:
                os.environ.pop("OPENLINEAGE_DBT_JOB_NAME", None)
            else:
                os.environ["OPENLINEAGE_DBT_JOB_NAME"] = previous_job_name
        if target_paths:
            _upload_dbt_artifacts(context, spec, merge_run_results(target_paths))

    _dbt_gold_assets.__name__ = f"{spec.product}_dbt_gold_assets"
    manifest_path = ensure_baked_manifest(spec.dbt_project_dir)
    inputs = spec.resolve(manifest_path)
    dbt_gold_assets = dbt_assets(
        manifest=manifest_path,
        select=spec.dbt_select,
        dagster_dbt_translator=ProductDbtTranslator(),
    )(_dbt_gold_assets)

    # The steps run one after another, and the heavy work is elsewhere: Silver's in a Floe Kubernetes Job, Gold's
    # in the dbt CLI. Dagster's default multiprocess executor would start a Python process per step, each
    # re-importing the whole code location (about 6 s) before its first line. Floe's own entity concurrency is
    # unaffected: the Job reads it from the profile. dagster-floe's job run config would only carry it as a
    # multiprocess setting, which the in-process executor rejects, so the job takes none.
    product_pipeline = define_asset_job(
        name=spec.job_name,
        selection=(
            AssetSelection.assets(
                *[AssetKey([source, resource]) for source, resource in inputs.bronze_inputs],
                *[AssetKey([spec.domain, entity]) for entity in inputs.silver_inputs],
                *[AssetKey([spec.product, asset_name]) for asset_name in inputs.gold_assets],
            ).required_multi_asset_neighbors()
        ),
        executor_def=in_process_executor,
        tags={**_bronze_source_binding_tags(inputs), SILVER_DOMAIN_TAG: spec.domain, PRODUCT_TAG: spec.product},
        hooks=_quality_manifest_hooks(spec, inputs.silver_inputs),
    )

    return Definitions(
        assets=[dbt_gold_assets],
        jobs=[product_pipeline],
        schedules=build_stage_schedules(spec),
    )


# A run of the producing product in any of these states may be replacing the tables this run would read.
_IN_FLIGHT = [
    DagsterRunStatus.QUEUED,
    DagsterRunStatus.NOT_STARTED,
    DagsterRunStatus.MANAGED,
    DagsterRunStatus.STARTING,
    DagsterRunStatus.STARTED,
    DagsterRunStatus.CANCELING,
]


class UpstreamProductNotReady(Failure):
    """A product's run was refused because a product it reads from has not built, or is building."""


def _require_upstream_products_ready(instance, product: str, upstream: dict[str, tuple[str, ...]]) -> None:
    """Fail before dbt starts when a product this one reads from has not built, or is building now.

    A producing product replaces its tables one dbt generation at a time (`create or replace`, so no single table
    is ever half-written), but a consumer started in the middle would read a fact from the new run beside a
    dimension from the last one. The Dagster dependency alone orders nothing across separate jobs, so the
    consumer checks the instance it runs on (each stage has its own) and fails with the cause instead of waiting:
    a run that holds a slot for another job's hour is worse than a red one the next schedule tick repairs.

    It checks that each model was materialised at some point, not that it is fresh (#921).
    """
    stage = os.environ.get(_STAGE_ENV, "unknown")
    problems: list[str] = []
    for producer, models in upstream.items():
        never_built = [
            name for name in models if instance.get_latest_materialization_event(AssetKey([producer, name])) is None
        ]
        if never_built:
            problems.append(
                f"{producer} has never materialised {', '.join(never_built)} on stage {stage}; "
                f"run {producer}_pipeline first"
            )
        building = instance.get_runs(filters=RunsFilter(tags={PRODUCT_TAG: producer}, statuses=_IN_FLIGHT), limit=1)
        if building:
            problems.append(
                f"{producer}_pipeline run {building[0].run_id} is queued or in progress on stage {stage} and would "
                f"replace {', '.join(models)} while this run reads them; retry {product}_pipeline after it ends"
            )
    if problems:
        raise UpstreamProductNotReady(f"{product}_pipeline cannot start: " + "; ".join(problems))


def _bronze_source_binding_tags(inputs: ResolvedProductInputs) -> dict[str, str]:
    """The stage bindings of the sources this job loads, for its run pod (openlakeforge_runtime/source_bindings.py)."""
    loaded = {source for source, _ in inputs.bronze_inputs}
    sources = [source for source in inventory_for(_LAKEHOUSE_CODE_ROOT).sources if source.name in loaded]
    return source_binding_tags(sources)


def build_stage_schedules(spec: ProductDefinitionSpec, *, stage: str | None = None) -> list[ScheduleDefinition]:
    """Build the schedule a stage is entitled to: the product's cadence, or daily at 03:00 UTC without one.

    `stage` defaults to the deployed stage the activation release puts on the
    pod. Anything else -- an unset variable, a workstation run, a stage this
    scaffold does not cover -- gets no schedule at all, which is the safe
    direction: a stage cannot acquire recurring runs by being unrecognised.

    The schedule is `STOPPED` unless the cadence sets `enabled_in_prod`, so deploying a revision starts nothing
    the product's code has not declared. Without the flag, enabling it is a deliberate act in the Dagster UI.
    """
    resolved = (os.environ.get(_STAGE_ENV, "") if stage is None else stage).strip().lower()
    if resolved not in _SCHEDULED_STAGES:
        return []
    cadence = spec.cadence
    if cadence is None:
        # The default keeps its original name: Dagster keys a schedule's on/off state by name.
        return [_product_schedule(f"{spec.product}_daily", spec, ProductCadence())]
    if cadence.cron is None:
        return []
    return [_product_schedule(f"{spec.product}_schedule", spec, cadence)]


def _product_schedule(name: str, spec: ProductDefinitionSpec, cadence: ProductCadence) -> ScheduleDefinition:
    return ScheduleDefinition(
        name=name,
        job_name=spec.job_name,
        cron_schedule=cadence.cron,
        execution_timezone=cadence.timezone,
        default_status=DefaultScheduleStatus.RUNNING if cadence.enabled_in_prod else DefaultScheduleStatus.STOPPED,
    )


def _quality_manifest_hooks(spec: ProductDefinitionSpec, silver_inputs: tuple[str, ...]) -> set:
    """Hooks that publish the run's quality manifest after each step, failed or not (#1162).

    A Floe abort fails its step before the dbt step starts, so no single step sees both engines: each hook
    rebuilds the whole manifest from what the run holds so far and the last one wins, except that a record the
    run already published with a known outcome is never replaced by an unknown one (a report read that failed
    this time). The key carries the Dagster run id, so a rewrite only touches the same run's manifest.
    """

    @success_hook
    def publish_quality_manifest_on_success(context) -> None:
        _publish_quality_manifest(context, spec, silver_inputs)

    @failure_hook
    def publish_quality_manifest_on_failure(context) -> None:
        _publish_quality_manifest(context, spec, silver_inputs)

    return {publish_quality_manifest_on_success, publish_quality_manifest_on_failure}


def _publish_quality_manifest(context, spec: ProductDefinitionSpec, silver_inputs: tuple[str, ...]) -> None:
    """Upload what the run's Floe checks and dbt artifacts say. Never raises: losing this evidence must not
    turn a passing step red or replace the error of a failing one."""
    try:
        artifact_base = os.environ.get("OPENLAKEFORGE_ARTIFACT_BASE_URI")
        run_artifact_base = os.environ.get("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI")
        stage = os.environ.get(_STAGE_ENV)
        if not (artifact_base and stage):
            return
        common = {
            "stage": stage,
            "domain": spec.domain,
            "product": spec.product,
            "dagster_run_id": context.run_id,
            "observed_at": datetime.now(UTC),
        }
        results = list(
            floe_quality_manifest(**common, entities=_floe_entities(context, spec.domain, silver_inputs)).results
        )
        dbt = run_artifact_base and read_dbt_evidence(
            f"{run_artifact_base.rstrip('/')}/dbt/{spec.domain}/{spec.product}/{context.run_id}"
        )
        if dbt:
            results.extend(dbt_quality_manifest(**common, evidence=dbt).results)
        if not results:
            return
        manifest = QualityManifest(
            stage=stage,
            domain=spec.domain,
            product=spec.product,
            dagster_run_id=context.run_id,
            results=tuple(results),
            # Every product has a dbt step and it runs last, so its evidence being readable is what completes the run.
            complete=bool(dbt),
        )
        bucket = split_s3_uri(artifact_base)[0]
        manifest = _without_regressions(manifest, _published_manifest(bucket, manifest))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / MANIFEST_FILENAME
            path.write_text(manifest.to_json())
            # `ops_key` already carries the stage's `activations/<stage>/` prefix; only the bucket comes from the URI.
            upload_file(path, bucket=bucket, key=manifest.ops_key)
    except Exception as error:
        context.log.error(
            f"Could not publish the quality manifest of run {getattr(context, 'run_id', None)}: {error!r}"
        )


def _published_manifest(bucket: str, manifest: QualityManifest) -> QualityManifest | None:
    """What an earlier step of this run published, or None when there is none or it cannot be read."""
    try:
        return QualityManifest.from_json(json.dumps(read_json_uri(f"s3://{bucket}/{manifest.ops_key}")))
    except Exception:
        return None


def _without_regressions(fresh: QualityManifest, published: QualityManifest | None) -> QualityManifest:
    """`fresh`, keeping the published record wherever `fresh` knows less, and any record `fresh` lacks."""
    if published is None:
        return fresh
    merged = {r.identity: r for r in published.results}
    for result in fresh.results:
        earlier = merged.get(result.identity)
        if not (earlier and result.outcome is QualityOutcome.UNKNOWN and earlier.outcome is not QualityOutcome.UNKNOWN):
            # The first observation's time stays: a rewrite restamps every record with the clock of the later step,
            # and the same execution would otherwise reach a consumer under a second timestamp.
            merged[result.identity] = replace(result, observed_at=earlier.observed_at) if earlier else result
    return QualityManifest(
        stage=fresh.stage,
        domain=fresh.domain,
        product=fresh.product,
        dagster_run_id=fresh.dagster_run_id,
        results=tuple(merged.values()),
        complete=fresh.complete or published.complete,
    )


def _floe_entities(context, domain: str, silver_inputs: tuple[str, ...]) -> list[FloeEntityEvidence]:
    """The Floe check results this run's event log holds, per Silver table. A table whose step has not run
    has none and is left out, rather than reported unknown."""
    checks: dict[str, list[FloeCheckEvidence]] = {}
    for entry in context.instance.all_logs(context.run_id, of_type=DagsterEventType.ASSET_CHECK_EVALUATION):
        evaluation = entry.dagster_event.event_specific_data
        path = evaluation.asset_key.path
        if (
            len(path) == 2
            and path[0] == domain
            and path[1] in silver_inputs
            and evaluation.check_name in FLOE_CHECK_IDS
        ):
            checks.setdefault(path[1], []).append(FloeCheckEvidence.from_asset_check(evaluation))
    entities = []
    for name, evidence in checks.items():
        raw = (c.metadata.get("entity_report_uri") for c in evidence)
        uri = next((u for r in raw if isinstance(u := getattr(r, "value", r), str)), None)
        entities.append(FloeEntityEvidence(entity=name, checks=tuple(evidence), report=read_entity_report(uri)))
    return entities


def _retaining_artifacts_on_failure(
    context, spec: ProductDefinitionSpec, target_paths: list[Path], stream, *, baked_manifest: Path | None = None
):
    """Yield `stream`, uploading what dbt left behind if it raises.

    A failed build is the run whose manifest and run_results.json are needed most, and the pod is cleaned up
    afterwards. The dbt exception is what the caller must see, so a failing upload is logged and never replaces it.
    """
    try:
        yield from stream
    except Exception:
        try:
            with_results = [path for path in target_paths if (path / "run_results.json").exists()]
            if with_results:
                _upload_dbt_artifacts(context, spec, merge_run_results(with_results))
            # The failing invocation's manifest and log are the evidence; it lands after, so it wins those keys.
            if target_paths and target_paths[-1] not in with_results:
                _upload_dbt_artifacts(context, spec, target_paths[-1])
            without_results = [i for i, path in enumerate(target_paths, 1) if path not in with_results]
            uploaded_baked = False
            if baked_manifest is not None and not any((path / "manifest.json").exists() for path in target_paths):
                uploaded_baked = True
                # No invocation left a manifest (dbt never started, or died while parsing before writing one); the
                # quality reader needs one to name tests.
                _upload_run_file(context, spec, baked_manifest, "target/manifest.json")
            if not target_paths or without_results:
                _upload_incomplete_marker(
                    context, spec, len(target_paths), without_results, baked_manifest=uploaded_baked
                )
        except Exception as upload_error:
            context.log.error(f"Could not retain dbt artifacts of the failed build: {upload_error!r}")
        raise


def _upload_incomplete_marker(
    context, spec: ProductDefinitionSpec, invocations: int, without_results: list[int], *, baked_manifest: bool = False
) -> None:
    """Tell a reader of the prefix that the absence of run_results.json is a failure, not a missing upload.

    `baked_manifest` says the uploaded manifest is the image's stage-neutral one: it is baked with an empty schema
    prefix while Glue adds one at run time, so the tables it names are not the run's."""
    run_id = getattr(context, "run_id", None) or "unknown"
    marker = {
        "status": "artifacts_incomplete",
        "reason": "the dbt build failed and some invocations wrote no run_results.json",
        "dagster_run_id": run_id,
        "dbt_invocations": invocations,
        "generations_without_results": without_results,
    }
    if baked_manifest:
        marker["baked_manifest"] = True
    _upload_run_json(context, spec, "artifacts_incomplete.json", marker)


def _upload_selected_tests(context, spec: ProductDefinitionSpec, unique_ids: list[str]) -> None:
    """Record the tests this build was asked to run. Losing it only makes the quality manifest less complete,
    so a failing upload is logged and never stops the build."""
    try:
        _upload_run_json(context, spec, "selected_tests.json", {"selected_tests": unique_ids})
    except Exception as error:
        context.log.error(f"Could not record the selected dbt tests: {error!r}")


def _upload_run_json(context, spec: ProductDefinitionSpec, name: str, document: dict) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / name
        path.write_text(json.dumps(document))
        _upload_run_file(context, spec, path, name)


def _upload_run_file(context, spec: ProductDefinitionSpec, path: Path, name: str) -> None:
    base_uri = os.environ.get("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI")
    if not base_uri:
        return
    run_id = getattr(context, "run_id", None) or "unknown"
    upload_file_to_base_uri(path, base_uri=base_uri, relative_key=f"dbt/{spec.domain}/{spec.product}/{run_id}/{name}")


def _upload_dbt_artifacts(context, spec: ProductDefinitionSpec, target_path: Path) -> None:
    base_uri = os.environ.get("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI")
    if not base_uri:
        context.log.warning("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI is not set; skipping dbt artifact upload")
        return

    run_id = getattr(context, "run_id", None) or "unknown"
    # The run's own artifacts: dagster-dbt points DBT_TARGET_PATH and
    # DBT_LOG_PATH at a per-invocation directory, while target/manifest.json
    # is the stage-neutral one baked into the image.
    artifact_candidates = {
        "target/manifest.json": target_path / "manifest.json",
        "target/run_results.json": target_path / "run_results.json",
        "target/sources.json": target_path / "sources.json",
        "logs/dbt.log": target_path / "dbt.log",
    }

    for artifact, artifact_path in artifact_candidates.items():
        if not artifact_path.exists():
            continue
        relative_key = f"dbt/{spec.domain}/{spec.product}/{run_id}/{artifact}"
        uri = upload_file_to_base_uri(artifact_path, base_uri=base_uri, relative_key=relative_key)
        log_event(
            context.log,
            message="Uploaded dbt artifact",
            service="dagster",
            tool="dbt",
            domain=spec.domain,
            product=spec.product,
            dagster_run_id=run_id,
            asset_key=f"{spec.product}/dbt_gold_assets",
            artifact=artifact,
            uri=uri,
        )
