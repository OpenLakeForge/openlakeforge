from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from dagster import (
    AssetKey,
    AssetOut,
    AssetSelection,
    DefaultScheduleStatus,
    Definitions,
    MetadataValue,
    Output,
    ScheduleDefinition,
    define_asset_job,
    multi_asset,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, dbt_assets
from floe_dagster.assets import load_floe_assets
from floe_dagster.definitions import build_job_run_config_from_manifest, build_runner_from_env
from floe_dagster.manifest import ManifestEntity

import lakehouse_code
from libs.bronze_csv import BronzeLoadResult
from libs.dbt.render_profiles import BAKED_PROFILES_DIR, ensure_baked_manifest
from libs.floe_profile import write_profile
from libs.openlakeforge_logging import log_event
from libs.s3_artifacts import upload_file_to_base_uri
from libs.source_bindings import source_binding_tags
from openlakeforge_domain import full_refresh_scope, inventory_for

# The project imported as `lakehouse_code`: beside libs/ in the image, but a
# separate root when olf checks a customer project against the distribution.
_LAKEHOUSE_CODE_ROOT = Path(lakehouse_code.__file__).resolve().parent
FLOE_PROJECT = _LAKEHOUSE_CODE_ROOT / "silver" / "floe.yml"
# Names the Floe manifest, and with it each domain's op: without a name the op
# name derives from the project's path and checksum, and every contract edit
# would rename it, cutting the op off from its run history.
_FLOE_MANIFEST_NAME = "openlakeforge"

_STAGE_ENV = "OPENLAKEFORGE_STAGE"
# Only PROD carries the recurring-schedule scaffold. DEV and UAT stay manual:
# the same immutable revision is deployed to every stage, so a schedule that
# merely defaulted to stopped would still be present in the DEV instance for
# someone to switch on, and the run it then launched would be a
# production-shaped daily workload against the DEV catalog.
_SCHEDULED_STAGES = frozenset({"prod"})
_DAILY_SCHEDULE_CRON = "0 3 * * *"


@dataclass(frozen=True)
class ProductDefinitionSpec:
    domain: str
    product: str
    silver_inputs: tuple[str, ...]
    bronze_inputs: tuple[tuple[str, str], ...]
    gold_assets: tuple[str, ...]

    @property
    def job_name(self) -> str:
        return f"{self.product}_pipeline"

    @property
    def dbt_project_dir(self) -> Path:
        return _LAKEHOUSE_CODE_ROOT / "gold" / self.product / "dbt"


@dataclass(frozen=True)
class SourceDefinitionSpec:
    source: str
    resources: tuple[str, ...]
    bronze_loader: Callable[..., dict[str, BronzeLoadResult]]


def build_source_definitions(spec: SourceDefinitionSpec) -> Definitions:
    # A source with an incremental resource has a loader that takes
    # `full_refresh` (libs.bronze_dlt.load_dlt_source); a full-only source
    # reloads everything on every run, so its loader has nothing to refresh.
    declared = next((s for s in inventory_for(_LAKEHOUSE_CODE_ROOT).sources if s.name == spec.source), None)
    incremental = declared is not None and any(resource.load.incremental for resource in declared.resources)

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
        # Read for every source, before any load, so malformed refresh tags
        # fail the run while Bronze is still untouched.
        full_refresh = full_refresh_scope(context.run.tags) == "bronze"
        previous_run_id = os.environ.get("DAGSTER_RUN_ID")
        os.environ["DAGSTER_RUN_ID"] = context.run_id
        try:
            selected = tuple(
                resource
                for resource in spec.resources
                if context.selected_output_names is None or resource in context.selected_output_names
            )
            if incremental:
                results = spec.bronze_loader(selected, full_refresh=full_refresh)
            else:
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
            profiles_dir=str(BAKED_PROFILES_DIR),
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
    dbt_gold_assets = dbt_assets(
        manifest=ensure_baked_manifest(spec.dbt_project_dir),
        dagster_dbt_translator=ProductDbtTranslator(),
    )(_dbt_gold_assets)

    product_pipeline = define_asset_job(
        name=spec.job_name,
        selection=(
            AssetSelection.assets(
                *[AssetKey([source, resource]) for source, resource in spec.bronze_inputs],
                *[AssetKey([spec.domain, entity]) for entity in spec.silver_inputs],
                *[AssetKey([spec.product, asset_name]) for asset_name in spec.gold_assets],
            ).required_multi_asset_neighbors()
        ),
        config=build_job_run_config_from_manifest(
            config_path=str(FLOE_PROJECT), profile_path=_floe_profile(), manifest_name=_FLOE_MANIFEST_NAME
        ),
        tags=_bronze_source_binding_tags(spec) or None,
    )

    return Definitions(
        assets=[dbt_gold_assets],
        jobs=[product_pipeline],
        schedules=build_stage_schedules(spec),
    )


def _bronze_source_binding_tags(spec: ProductDefinitionSpec) -> dict[str, str]:
    """The stage bindings of the sources this job loads, for its run pod (libs/source_bindings.py)."""
    loaded = {source for source, _ in spec.bronze_inputs}
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
