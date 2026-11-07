from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import floe
from dagster import (
    AssetKey,
    AssetOut,
    AssetSelection,
    DefaultScheduleStatus,
    Definitions,
    Failure,
    Field,
    MetadataValue,
    Output,
    ScheduleDefinition,
    define_asset_job,
    job,
    multi_asset,
    op,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, dbt_assets
from floe_dagster.assets import load_floe_assets
from floe_dagster.definitions import build_job_run_config_from_manifest, build_runner_from_env
from floe_dagster.manifest import ManifestEntity

import lakehouse_code
from openlakeforge_runtime import gold_maintenance
from openlakeforge_runtime.bronze_csv import BronzeLoadResult
from openlakeforge_runtime.dbt.render_profiles import dbt_environment, ensure_baked_manifest, resolve_profiles_dir
from openlakeforge_runtime.floe_profile import write_profile
from openlakeforge_runtime.openlakeforge_logging import log_event
from openlakeforge_runtime.s3_artifacts import upload_file_to_base_uri
from openlakeforge_runtime.source_bindings import source_binding_tags
from openlakeforge_core import full_refresh_scope, inventory_for

__all__ = [
    "ProductDefinitionSpec",
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

    - `gold_assets`: the models of the product's dbt project;
    - `silver_inputs`: the sources of the product's dbt project, which are the
      domain's Silver tables;
    - `bronze_inputs`: the Bronze resource each of `silver_inputs` reads, from
      the domain's Floe entities.
    """

    domain: str
    product: str
    silver_inputs: tuple[str, ...] | None = None
    bronze_inputs: tuple[tuple[str, str], ...] | None = None
    gold_assets: tuple[str, ...] | None = None

    @property
    def job_name(self) -> str:
        return f"{self.product}_pipeline"

    @property
    def dbt_project_dir(self) -> Path:
        return _LAKEHOUSE_CODE_ROOT / "gold" / self.product / "dbt"

    def resolve(self, manifest_path: Path | None = None) -> ResolvedProductInputs:
        """The effective inputs; `manifest_path` defaults to the product's baked dbt manifest."""
        manifest = None
        if self.gold_assets is None or self.silver_inputs is None:
            manifest = json.loads((manifest_path or ensure_baked_manifest(self.dbt_project_dir)).read_text())
        gold = self.gold_assets
        if gold is None:
            gold = _manifest_names(manifest, "nodes", "model")
            if not gold:
                raise ValueError(f"product {self.product!r}: its dbt project declares no models to build")
        silver = self.silver_inputs
        if silver is None:
            silver = _manifest_names(manifest, "sources", "source")
            if not silver:
                raise ValueError(f"product {self.product!r}: its dbt project declares no sources to read")
        bronze = self.bronze_inputs
        if bronze is None:
            bronze = self._floe_bronze_inputs(silver)
        return ResolvedProductInputs(silver_inputs=tuple(silver), bronze_inputs=tuple(bronze), gold_assets=tuple(gold))

    def _floe_bronze_inputs(self, silver: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        inventory = inventory_for(_LAKEHOUSE_CODE_ROOT)
        domain = next((candidate for candidate in inventory.domains if candidate.name == self.domain), None)
        if domain is None:
            raise ValueError(f"product {self.product!r}: the project declares no domain {self.domain!r}")
        entities = {entity.name: (entity.source, entity.resource) for entity in inventory.floe_entities(domain)}
        missing = [name for name in silver if name not in entities]
        if missing:
            raise ValueError(
                f"product {self.product!r}: {missing} are not Floe entities of domain {self.domain!r}; "
                "a dbt source of the product must be one of the domain's Silver tables"
            )
        return tuple(entities[name] for name in silver)


def _manifest_names(manifest: dict | None, section: str, resource_type: str) -> tuple[str, ...]:
    """Sorted names of the project's own `resource_type` entries; packages' models are not the product's."""
    assert manifest is not None
    project = manifest.get("metadata", {}).get("project_name")
    return tuple(
        sorted(
            {
                entry["name"]
                for entry in manifest.get(section, {}).values()
                if entry["resource_type"] == resource_type and project in (None, entry.get("package_name"))
            }
        )
    )


@dataclass(frozen=True)
class SourceDefinitionSpec:
    source: str
    resources: tuple[str, ...]
    bronze_loader: Callable[[tuple[str, ...]], dict[str, BronzeLoadResult]]


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
            results = spec.bronze_loader(selected)
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
                return AssetKey([spec.domain, dbt_resource_props["name"]])
            return AssetKey([spec.product, dbt_resource_props["name"]])

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
        try:
            invocation = dbt.cli(["build", *(["--full-refresh"] if full_refresh else [])], context=context)
            yield from invocation.stream()
        finally:
            if previous_job_name is None:
                os.environ.pop("OPENLINEAGE_DBT_JOB_NAME", None)
            else:
                os.environ["OPENLINEAGE_DBT_JOB_NAME"] = previous_job_name
        _upload_dbt_artifacts(context, spec, invocation.target_path)

    _dbt_gold_assets.__name__ = f"{spec.product}_dbt_gold_assets"
    manifest_path = ensure_baked_manifest(spec.dbt_project_dir)
    inputs = spec.resolve(manifest_path)
    dbt_gold_assets = dbt_assets(
        manifest=manifest_path,
        dagster_dbt_translator=ProductDbtTranslator(),
    )(_dbt_gold_assets)

    product_pipeline = define_asset_job(
        name=spec.job_name,
        selection=(
            AssetSelection.assets(
                *[AssetKey([source, resource]) for source, resource in inputs.bronze_inputs],
                *[AssetKey([spec.domain, entity]) for entity in inputs.silver_inputs],
                *[AssetKey([spec.product, asset_name]) for asset_name in inputs.gold_assets],
            ).required_multi_asset_neighbors()
        ),
        config=build_job_run_config_from_manifest(
            config_path=str(FLOE_PROJECT), profile_path=_floe_profile(), manifest_name=_FLOE_MANIFEST_NAME
        ),
        tags=_bronze_source_binding_tags(inputs) or None,
    )

    return Definitions(
        assets=[dbt_gold_assets],
        jobs=[product_pipeline],
        schedules=build_stage_schedules(spec),
    )


def _bronze_source_binding_tags(inputs: ResolvedProductInputs) -> dict[str, str]:
    """The stage bindings of the sources this job loads, for its run pod (openlakeforge_runtime/source_bindings.py)."""
    loaded = {source for source, _ in inputs.bronze_inputs}
    sources = [source for source in inventory_for(_LAKEHOUSE_CODE_ROOT).sources if source.name in loaded]
    return source_binding_tags(sources)


def build_stage_schedules(
    spec: ProductDefinitionSpec, *, stage: str | None = None
) -> list[ScheduleDefinition]:
    """Build the daily schedule scaffold a stage is entitled to.

    `stage` defaults to the deployed stage the activation release puts on the
    pod. Anything else -- an unset variable, a workstation run, a stage this
    scaffold does not cover -- gets no schedule at all, which is the safe
    direction: a stage cannot acquire recurring runs by being unrecognised.

    The schedule is `STOPPED` even where it exists, so deploying a revision
    never starts it. Enabling it is a deliberate act in the Dagster UI.
    """
    resolved = (os.environ.get(_STAGE_ENV, "") if stage is None else stage).strip().lower()
    if resolved not in _SCHEDULED_STAGES:
        return []
    return [
        ScheduleDefinition(
            name=f"{spec.product}_daily",
            job_name=spec.job_name,
            cron_schedule=_DAILY_SCHEDULE_CRON,
            execution_timezone="UTC",
            default_status=DefaultScheduleStatus.STOPPED,
        )
    ]


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
