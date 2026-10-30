"""AWS provider-contract and storage/catalog preflight checks."""

from __future__ import annotations

import os

from olf import log
from olf.auth import aws_session
from olf.e2e._shell import E2EConfig, E2EError, aws_stack_region, load_provider_contracts_or_raise
from olf.e2e._trino import stage_catalog_name


def check_aws_provider_contracts(cfg: E2EConfig) -> None:
    log.step("Checking AWS provider contracts...")
    provider_contracts = load_provider_contracts_or_raise(cfg)
    # storage/catalog are per-stage in the native v3 contract (#114); pick
    # any enabled stage - every stage shares the same provider/implementation
    # strings regardless of which one this check happens to land on.
    stage_name = next(iter(provider_contracts["stages"]))
    expected = {
        ("stages", stage_name, "storage", "implementation"): "storage.aws_s3",
        ("shared", "metadata_database", "implementation"): "metadata_database.aws_rds_postgresql",
        ("stages", stage_name, "catalog", "implementation"): "catalog.aws_glue",
        ("stages", stage_name, "catalog", "catalog_type"): "glue",
        ("shared", "ops_storage", "implementation"): "artifacts.aws_s3_bucket",
    }
    for path, expected_value in expected.items():
        value = provider_contracts
        for key in path:
            value = value[key]
        if value != expected_value:
            raise E2EError(f"provider_contracts.{'.'.join(path)} expected {expected_value!r}, got {value!r}")


def check_aws_storage_and_glue(cfg: E2EConfig) -> None:
    """Assert the S3 artifact bucket and every expected Glue database exist.

    Glue database lifecycle moved to Phase 2 (ADR 0002), so the provider
    contract no longer publishes a namespace/database list to check against --
    `olf catalog sync-namespaces` is what is under test here, and the only
    source of truth for "did it work" is AWS itself.
    """
    log.step("Checking S3 artifact bucket and Glue catalog databases...")
    provider_contracts = load_provider_contracts_or_raise(cfg)
    bucket = provider_contracts["shared"]["ops_storage"]["bucket_name"]
    region = aws_stack_region(cfg)
    # This account's Glue service refuses to create a custom catalog per
    # stage, so every stage shares the account's one default catalog and
    # olf.contracts.build_contract_env prefixes each physical database name
    # with the stage's own catalog_name to stay collision-free - the bare
    # inventory names below never exist in Glue on their own.
    stage_name = stage_catalog_name(cfg).removeprefix("lakehouse_")
    stage = provider_contracts["stages"].get(stage_name)
    if stage is None:
        raise E2EError(f"provider_contracts.stages has no entry for stage {stage_name!r}.")
    namespace_prefix = f"{stage['catalog']['catalog_name']}_"
    expected_schemas = {
        f"{namespace_prefix}{name}"
        for name in (
            cfg.inventory.bronze_namespace_names
            | cfg.inventory.silver_namespace_names
            | cfg.inventory.gold_namespace_names
        )
    }
    try:
        session = aws_session(os.environ, region=region)
        session.client("s3").head_bucket(Bucket=bucket)
        glue = session.client("glue", region_name=region)
        for database in sorted(expected_schemas):
            glue.get_database(Name=database)
    except Exception as exc:
        raise E2EError(f"AWS S3/Glue preflight failed: {exc}") from exc
