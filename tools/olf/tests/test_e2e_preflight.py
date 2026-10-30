from pathlib import Path

import pytest
from conftest import E2E_INVENTORY, e2e_cfg

from olf.e2e import _preflight, _shell

EXPECTED_GLUE_SCHEMAS = (
    E2E_INVENTORY.bronze_namespace_names | E2E_INVENTORY.silver_namespace_names | E2E_INVENTORY.gold_namespace_names
)


def test_aws_provider_contract_smoke_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider_contracts = {
        "shared": {
            "metadata_database": {"implementation": "metadata_database.aws_rds_postgresql"},
            "ops_storage": {"implementation": "artifacts.aws_s3_bucket"},
        },
        "stages": {
            "dev": {
                "storage": {"implementation": "storage.aws_s3"},
                "catalog": {"implementation": "catalog.aws_glue", "catalog_type": "glue"},
            }
        },
    }
    monkeypatch.setattr(_preflight, "load_provider_contracts_or_raise", lambda _cfg: provider_contracts)

    _preflight.check_aws_provider_contracts(e2e_cfg(tmp_path, env="aws", suite="smoke"))


def test_aws_storage_and_glue_smoke_check_uses_bucket_and_databases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The contract no longer publishes a namespace/database list (ADR 0002) --
    every expected database name comes straight from the inventory, and the
    check asserts against AWS directly rather than against the contract.
    Every expected name also carries the stage's own catalog_name prefix:
    this account's Glue service refuses to create a catalog per stage, so
    every stage shares the account's one default catalog, and the database
    name is what stays collision-free across stages instead."""
    provider_contracts = {
        "shared": {"ops_storage": {"bucket_name": "openlakeforge-ops"}},
        "stages": {"dev": {"catalog": {"catalog_name": "lakehouse_dev"}}},
    }
    calls: list[tuple[str, str]] = []
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CATALOG", "lakehouse_dev")
    monkeypatch.setattr(_preflight, "load_provider_contracts_or_raise", lambda _cfg: provider_contracts)
    # aws_stack_region (called by check_aws_storage_and_glue) calls terraform_output
    # internally within _shell's own module namespace, not _preflight's.
    monkeypatch.setattr(_shell, "terraform_output", lambda _dir, name: "eu-central-1" if name == "aws_region" else "")

    class Session:
        def client(self, name, **_kwargs):  # noqa: ANN001, ANN003, ANN202
            if name == "s3":
                return type("S3", (), {"head_bucket": lambda _self, *, Bucket: calls.append(("bucket", Bucket))})()
            return type("Glue", (), {"get_database": lambda _self, *, Name: calls.append(("database", Name))})()

    monkeypatch.setattr(_preflight, "aws_session", lambda *_args, **_kwargs: Session())

    _preflight.check_aws_storage_and_glue(e2e_cfg(tmp_path, env="aws", suite="smoke"))

    assert ("bucket", "openlakeforge-ops") in calls
    assert {name for kind, name in calls if kind == "database"} == {
        f"lakehouse_dev_{name}" for name in EXPECTED_GLUE_SCHEMAS
    }


def test_aws_storage_and_glue_smoke_check_uses_the_requested_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider_contracts = {
        "shared": {"ops_storage": {"bucket_name": "openlakeforge-ops"}},
        "stages": {
            "dev": {"catalog": {"catalog_name": "lakehouse_dev"}},
            "prod": {"catalog": {"catalog_name": "lakehouse_prod"}},
        },
    }
    calls: list[str] = []
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CATALOG", "lakehouse_prod")
    monkeypatch.setattr(_preflight, "load_provider_contracts_or_raise", lambda _cfg: provider_contracts)
    monkeypatch.setattr(_shell, "terraform_output", lambda _dir, name: "eu-central-1" if name == "aws_region" else "")

    class Session:
        def client(self, name, **_kwargs):  # noqa: ANN001, ANN003, ANN202
            if name == "s3":
                return type("S3", (), {"head_bucket": lambda _self, **_kwargs: None})()
            return type("Glue", (), {"get_database": lambda _self, *, Name: calls.append(Name)})()

    monkeypatch.setattr(_preflight, "aws_session", lambda *_args, **_kwargs: Session())

    _preflight.check_aws_storage_and_glue(e2e_cfg(tmp_path, env="aws", suite="smoke"))

    assert set(calls) == {f"lakehouse_prod_{name}" for name in EXPECTED_GLUE_SCHEMAS}


def test_aws_stack_region_prefers_foundation_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(_shell, "terraform_output", lambda _dir, name: "eu-central-1" if name == "aws_region" else "")

    assert _shell.aws_stack_region(e2e_cfg(tmp_path, env="aws", suite="smoke")) == "eu-central-1"
