from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from conftest import e2e_cfg

from olf.e2e import _isolation
from olf.e2e._shell import E2EError


class _S3Client:
    def __init__(self) -> None:
        self.head_buckets: list[str] = []
        self.prefixes: list[str] = []

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        self.head_buckets.append(Bucket)
        if Bucket == "openlakeforge-prod-bronze":
            raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "HeadBucket")

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int) -> None:  # noqa: N803, ARG002
        self.prefixes.append(Prefix)
        if Prefix == "activations/prod/":
            raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "ListObjectsV2")


def test_isolation_uses_the_selected_stage_bucket_from_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-dev", shared_namespace="olf-system")
    provider_contracts = {
        "stages": {
            "dev": {"storage": {"bronze": {"bucket_name": "lakehouse-bronze"}}},
            "prod": {"storage": {"bronze": {"bucket_name": "openlakeforge-prod-bronze"}}},
        }
    }
    client = _S3Client()

    monkeypatch.setattr(_isolation, "load_provider_contracts_or_raise", lambda _cfg: provider_contracts)
    monkeypatch.setattr(_isolation.k8s, "resource_exists", lambda *_args: True)
    def _trino_query(_cfg, sql: str) -> str:  # noqa: ANN001
        if "lakehouse_prod" in sql:
            raise E2EError("Access Denied: Cannot select from columns [...] in catalog lakehouse_prod")
        return ""

    monkeypatch.setattr(_isolation, "trino_query", _trino_query)
    monkeypatch.setattr(_isolation, "_s3_identity", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(_isolation, "_s3_client", lambda *_args, **_kwargs: client)

    @contextmanager
    def _port_forward(*_args, **_kwargs):  # noqa: ANN202
        yield 8333

    monkeypatch.setattr(_isolation.k8s, "port_forward", _port_forward)
    monkeypatch.setattr(_isolation, "check_stage_data_is_its_own", lambda *_args: None)
    monkeypatch.setattr(_isolation, "check_dagster_state_isolation", lambda *_args: None)

    _isolation.check_stage_isolation(cfg)

    assert client.head_buckets == ["openlakeforge-prod-bronze", "lakehouse-bronze"]
    assert client.prefixes == ["activations/prod/", "activations/dev/"]


def test_trino_isolation_probe_rejects_a_non_denial_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing/broken sibling catalog fails the query too, but for a reason
    other than access control - the probe must not read that as isolation."""
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-dev", shared_namespace="olf-system")

    def _trino_query(_cfg, sql: str) -> str:  # noqa: ANN001
        if "lakehouse_prod" in sql:
            raise E2EError("Query failed: Catalog 'lakehouse_prod' does not exist")
        return ""

    monkeypatch.setattr(_isolation, "trino_query", _trino_query)

    with pytest.raises(E2EError, match="reason other than an access-control rejection"):
        _isolation._expect_trino_denied(
            cfg, user="olf-dev-runtime", sql="SELECT 1 FROM lakehouse_prod.x", what="read access"
        )


def test_s3_isolation_probe_rejects_a_not_found_response() -> None:
    """A HeadBucket 404 (sibling bucket never provisioned) must not be read
    as an access denial - that would pass the probe while proving nothing
    about isolation."""

    def _call() -> None:
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket")

    with pytest.raises(E2EError, match="reason other than access control"):
        _isolation._expect_s3_denied(_call, who="dev", what="HeadBucket")


def test_s3_isolation_probe_accepts_a_403_denial() -> None:
    def _call() -> None:
        raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "HeadBucket")

    _isolation._expect_s3_denied(_call, who="dev", what="HeadBucket")


_STAGE_BUCKETS = {
    "stages": {
        stage: {"storage": {layer: {"bucket_name": f"{stage}-{layer}"} for layer in ("silver", "gold")}}
        for stage in ("dev", "prod")
    }
}


def _stub_files(
    monkeypatch: pytest.MonkeyPatch,
    cfg,  # noqa: ANN001
    overrides: dict[str, str],
    sibling_objects: set[str] | None = None,
    unregistered: frozenset[str] = frozenset(),
) -> list[str]:
    """Answer each `<table>$files` query with one file in the stage's own bucket
    unless overridden; list the sibling's buckets as `sibling_objects` and this
    stage's as its table files plus `unregistered` objects no table references."""
    own = {
        table: f"s3://prod-{layer}/{table.replace('.', '/')}/data/{table}-00000.parquet"
        for layer, table in _isolation._materialized_tables(cfg)
    }
    queried: list[str] = []

    def _trino_query(_cfg, sql: str) -> str:  # noqa: ANN001
        schema, name = re.search(r'lakehouse_prod\.(\w+)\."(\w+)\$files"', sql).groups()  # type: ignore[union-attr]
        queried.append(f"{schema}.{name}")
        return overrides.get(f"{schema}.{name}", own[f"{schema}.{name}"])

    monkeypatch.setattr(_isolation, "trino_query", _trino_query)
    sibling_listed = {"dev-only-00000.parquet"} if sibling_objects is None else sibling_objects
    own_listed = {path.rsplit("/", 1)[-1] for path in {**own, **overrides}.values() if path} | unregistered
    monkeypatch.setattr(
        _isolation, "_bucket_data_files", lambda _c, _contracts, stage: sibling_listed if stage == "dev" else own_listed
    )
    return queried


def test_stage_data_check_covers_every_declared_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    queried = _stub_files(monkeypatch, cfg, {})

    _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")

    # The inventory's own counts, which the table-count assertion also relies
    # on -- not the helper under test.
    marts = {table for table in queried if table.split(".", 1)[0] in cfg.inventory.gold_namespace_names}
    assert len(marts) == cfg.inventory.gold_table_count > 0
    assert len(set(queried) - marts) == cfg.inventory.silver_table_count > 0


def test_stage_data_check_rejects_a_table_reading_the_sibling_stages_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    layer, table = _isolation._materialized_tables(cfg)[0]
    _stub_files(monkeypatch, cfg, {table: f"s3://dev-{layer}/{table}/data/x.parquet"})

    with pytest.raises(E2EError, match=f"{table} reads data files from .*dev-{layer}"):
        _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")


def test_stage_data_check_rejects_sibling_files_copied_into_the_stages_own_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Location passes -- the copy sits in PROD's bucket -- but the name is DEV's."""
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    layer, table = _isolation._materialized_tables(cfg)[0]
    _stub_files(
        monkeypatch,
        cfg,
        {table: f"s3://prod-{layer}/{table}/data/written-by-dev.parquet"},
        sibling_objects={"written-by-dev.parquet"},
    )

    with pytest.raises(E2EError, match="promotion copied data: .*written-by-dev.parquet"):
        _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")


def test_stage_data_check_rejects_an_unregistered_copy_in_the_stages_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No table references the copy, but it sits in PROD's bucket under DEV's name."""
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    copy = "orphaned-dev-copy.parquet"
    _stub_files(monkeypatch, cfg, {}, sibling_objects={copy}, unregistered=frozenset({copy}))

    with pytest.raises(E2EError, match=f"promotion copied data: .*{copy}"):
        _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")


def test_stage_data_check_defers_to_a_sibling_that_has_written_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    _stub_files(monkeypatch, cfg, {}, sibling_objects=set())

    _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")


def test_stage_data_check_rejects_a_table_with_no_data_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    _, empty = _isolation._materialized_tables(cfg)[0]
    _stub_files(monkeypatch, cfg, {empty: ""})

    with pytest.raises(E2EError, match="has no data files"):
        _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")


def _stub_runs(monkeypatch: pytest.MonkeyPatch, runs: dict[str, set[str]]) -> None:
    monkeypatch.setattr(
        _isolation, "terraform_output_json", lambda _dir, _name: {stage: f"{stage}-webserver" for stage in runs}
    )
    monkeypatch.setattr(
        _isolation, "_dagster_run_ids", lambda service, _namespace, _log: runs[service.removesuffix("-webserver")]
    )


def test_dagster_isolation_accepts_disjoint_run_histories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_runs(monkeypatch, {"prod": {"p1", "p2"}, "dev": {"d1"}})

    _isolation.check_dagster_state_isolation(e2e_cfg(tmp_path), "prod", "dev")


def test_dagster_isolation_rejects_a_shared_run_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_runs(monkeypatch, {"prod": {"r1", "r2"}, "dev": {"r1", "r2"}})

    with pytest.raises(E2EError, match="share runs"):
        _isolation.check_dagster_state_isolation(e2e_cfg(tmp_path), "prod", "dev")


def test_dagster_isolation_requires_runs_in_the_stage_under_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_runs(monkeypatch, {"prod": set(), "dev": {"d1"}})

    with pytest.raises(E2EError, match="prod's Dagster has no runs"):
        _isolation.check_dagster_state_isolation(e2e_cfg(tmp_path), "prod", "dev")


def test_dagster_isolation_defers_to_a_sibling_that_has_not_run_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEV's suite runs before PROD is promoted, so PROD has no history yet."""
    _stub_runs(monkeypatch, {"dev": {"d1"}, "prod": set()})

    _isolation.check_dagster_state_isolation(e2e_cfg(tmp_path), "dev", "prod")
