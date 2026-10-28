from __future__ import annotations

import os
import subprocess
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
    MetadataValue,
    Output,
    ScheduleDefinition,
    define_asset_job,
    multi_asset,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, dbt_assets
from floe_dagster.assets import load_floe_assets
from floe_dagster.definitions import build_job_run_config_from_manifest, build_runner_from_env

from libs.bronze_csv import BronzeLoadResult
from libs.dbt.render_profiles import ensure_runtime_profile_dir
from libs.floe_profile import write_profile
from libs.openlakeforge_logging import log_event
from libs.s3_artifacts import upload_file_to_base_uri

_LAKEHOUSE_CODE_ROOT = Path(__file__).resolve().parents[1] / "lakehouse_code"
FLOE_PROJECT = _LAKEHOUSE_CODE_ROOT / "silver" / "floe.yml"

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


def build_silver_definitions() -> Definitions:
    """Every Floe entity of the project, built in memory from `FLOE_PROJECT`.

    dagster-floe gives entities that share a named source one
    `sources/<project>/<name>` dependency; Bronze lands one asset per source
    resource, so each entity is pointed at its own `<source>/<resource>`
    instead, taken from the entity's `source.ref`/`resource`.
    """
    profile = _floe_profile()
    floe_defs = load_floe_assets(
        config_path=str(FLOE_PROJECT),
        profile_path=profile,
        runner=build_runner_from_env(),
        register_source_assets=False,
    )
    bronze = {
        AssetKey(entity["asset_key"]): AssetKey([entity["source"]["ref"], entity["source"]["resource"]])
        for entity in floe.build_manifest(str(FLOE_PROJECT), profile)["entities"]
        if entity["source"].get("ref")
    }
    assets = []
    for asset in floe_defs.assets:
        bronze_key = next((bronze[key] for key in asset.keys if key in bronze), None)
        if bronze_key is not None:
            named = {key: bronze_key for key in asset.dependency_keys if key.path[0] == "sources"}
            asset = asset.with_attributes(asset_key_replacements=named)
        assets.append(asset)
    return Definitions(assets=assets)


def build_product_definitions(spec: ProductDefinitionSpec) -> Definitions:
    class ProductDbtTranslator(DagsterDbtTranslator):
        def get_asset_key(self, dbt_resource_props) -> AssetKey:
            if dbt_resource_props["resource_type"] == "source":
                return AssetKey([spec.domain, dbt_resource_props["name"]])
            return AssetKey([spec.product, dbt_resource_props["name"]])

        def get_group_name(self, dbt_resource_props) -> str:
            return spec.product

    def _dbt_gold_assets(context):
        dbt_target = os.environ.get("OPENLAKEFORGE_DBT_TARGET", "local_runtime")
        dbt_profiles_dir = _ensure_dbt_profiles_dir(spec)
        dbt = DbtCliResource(
            project_dir=str(spec.dbt_project_dir),
            profiles_dir=str(dbt_profiles_dir),
            target=dbt_target,
            dbt_executable=os.environ.get("OPENLAKEFORGE_DBT_EXECUTABLE", "dbt-ol"),
        )
        previous_job_name = os.environ.get("OPENLINEAGE_DBT_JOB_NAME")
        os.environ["OPENLINEAGE_DBT_JOB_NAME"] = spec.job_name
        try:
            yield from dbt.cli(["build"], context=context).stream()
        finally:
            if previous_job_name is None:
                os.environ.pop("OPENLINEAGE_DBT_JOB_NAME", None)
            else:
                os.environ["OPENLINEAGE_DBT_JOB_NAME"] = previous_job_name
        _upload_dbt_artifacts(context, spec)

    _dbt_gold_assets.__name__ = f"{spec.product}_dbt_gold_assets"
    dbt_gold_assets = dbt_assets(
        manifest=_ensure_dbt_manifest(spec),
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
        config=build_job_run_config_from_manifest(config_path=str(FLOE_PROJECT), profile_path=_floe_profile()),
    )

    return Definitions(
        assets=[dbt_gold_assets],
        jobs=[product_pipeline],
        schedules=build_stage_schedules(spec),
    )


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


def _upload_dbt_artifacts(context, spec: ProductDefinitionSpec) -> None:
    base_uri = os.environ.get("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI")
    if not base_uri:
        context.log.warning("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI is not set; skipping dbt artifact upload")
        return

    run_id = getattr(context, "run_id", None) or "unknown"
    artifact_candidates = [
        spec.dbt_project_dir / "target" / "manifest.json",
        spec.dbt_project_dir / "target" / "run_results.json",
        spec.dbt_project_dir / "target" / "sources.json",
        spec.dbt_project_dir / "logs" / "dbt.log",
    ]

    for artifact_path in artifact_candidates:
        if not artifact_path.exists():
            continue
        relative_key = (
            f"dbt/{spec.domain}/{spec.product}/{run_id}/"
            f"{artifact_path.relative_to(spec.dbt_project_dir).as_posix()}"
        )
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
            artifact=str(artifact_path.relative_to(spec.dbt_project_dir)),
            uri=uri,
        )


def _ensure_dbt_manifest(spec: ProductDefinitionSpec) -> Path:
    manifest_path = spec.dbt_project_dir / "target" / "manifest.json"
    if manifest_path.exists():
        return manifest_path

    env = os.environ.copy()
    env.setdefault("AWS_ACCESS_KEY_ID", "openlakeforge")
    env.setdefault("AWS_SECRET_ACCESS_KEY", "openlakeforge")
    env.setdefault("OPENLAKEFORGE_STORAGE_REGION", "us-east-1")
    env.setdefault("AWS_REGION", env["OPENLAKEFORGE_STORAGE_REGION"])
    env.setdefault("AWS_DEFAULT_REGION", env["AWS_REGION"])
    env.setdefault("OPENLAKEFORGE_STORAGE_ENDPOINT", "http://seaweedfs-s3.olf-system:8333")
    env.setdefault("AWS_ENDPOINT_URL_S3", env["OPENLAKEFORGE_STORAGE_ENDPOINT"])
    env.setdefault("OPENLAKEFORGE_QUERY_TRINO_HOST", "trino.olf-system")
    env.setdefault("OPENLAKEFORGE_QUERY_TRINO_PORT", "8080")
    env.setdefault("OPENLAKEFORGE_QUERY_TRINO_CATALOG", "iceberg")
    env.setdefault("OPENLAKEFORGE_CATALOG_NAME", "lakehouse_dev")
    env.setdefault("OPENLAKEFORGE_DBT_TRINO_USER", "openlakeforge-dbt")
    env.setdefault("OPENLAKEFORGE_CATALOG_TYPE", "rest")
    env.setdefault("OPENLAKEFORGE_CATALOG_PROVIDER", "polaris")
    env.setdefault("OPENLAKEFORGE_CATALOG_REST_URI", "http://polaris.olf-system:8181/api/catalog")
    env.setdefault(
        "OPENLAKEFORGE_CATALOG_TOKEN_URI", "http://polaris.olf-system:8181/api/catalog/v1/oauth/tokens"
    )
    env.setdefault("OPENLAKEFORGE_CATALOG_WAREHOUSE", "lakehouse_dev")
    env.setdefault("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE", "PRINCIPAL_ROLE:ALL")
    profiles_dir = _ensure_dbt_profiles_dir(spec, env=env)

    subprocess.run(
        [
            "dbt",
            "deps",
            "--project-dir",
            str(spec.dbt_project_dir),
        ],
        check=True,
        env=env,
    )
    subprocess.run(
        [
            "dbt",
            "parse",
            "--project-dir",
            str(spec.dbt_project_dir),
            "--profiles-dir",
            str(profiles_dir),
            "--target",
            "local",
        ],
        check=True,
        env=env,
    )
    return manifest_path


def _ensure_dbt_profiles_dir(spec: ProductDefinitionSpec, env: dict[str, str] | None = None) -> Path:
    return ensure_runtime_profile_dir(spec.dbt_project_dir, env=env)
