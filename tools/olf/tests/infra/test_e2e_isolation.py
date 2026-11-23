from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from conftest import e2e_cfg

from olf.infra.e2e import _isolation
from olf.infra.e2e._shell import E2EError

OPS = "openlakeforge-ops"


def _denied(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, operation)


class _S3Client:
    """A SeaweedFS stand-in that grants an identity only its own buckets and ops-bucket prefixes, as the gateway's
    auth config does; `leaks` names buckets or ops prefixes it wrongly grants as well."""

    def __init__(self, buckets: set[str], prefixes: set[str], objects: dict[tuple[str, str], bytes]) -> None:
        self.buckets = buckets
        self.prefixes = prefixes
        self.objects = objects

    def _allowed(self, bucket: str, key: str = "") -> bool:
        if bucket == OPS:
            return any(key.startswith(prefix) for prefix in self.prefixes)
        return bucket in self.buckets

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        if not self._allowed(Bucket):
            raise ClientError({"Error": {"Code": "403", "Message": ""}}, "HeadBucket")

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int) -> dict:  # noqa: N803, ARG002
        if not self._allowed(Bucket, Prefix):
            raise _denied("ListObjectsV2")
        return {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        if not self._allowed(Bucket, Key):
            raise _denied("PutObject")
        self.objects[(Bucket, Key)] = Body

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop((Bucket, Key), None)


def _contracts(stages: tuple[str, ...]) -> dict:
    return {
        "stages": {
            stage: {"storage": {layer: {"bucket_name": f"{stage}-{layer}"} for layer in ("bronze", "silver", "gold")}}
            for stage in stages
        }
    }


def _cluster(
    monkeypatch: pytest.MonkeyPatch,
    stages: tuple[str, ...],
    *,
    namespaces: tuple[str, ...] | None = None,
    leaks: dict[str, set[str]] | None = None,
    quality_keys: bool = True,
) -> dict[tuple[str, str], bytes]:
    """Stub a local cluster with `stages` enabled; every identity is granted exactly its own state plus `leaks`
    (identity -> extra buckets or ops prefixes). Returns the objects written through the stand-in gateway."""
    objects: dict[tuple[str, str], bytes] = {}
    leaks = leaks or {}
    grants: dict[str, tuple[set[str], set[str]]] = {}
    for stage in stages:
        grants[f"seaweedfs-{stage}-s3-creds"] = (
            {f"{stage}-{layer}" for layer in ("bronze", "silver", "gold")},
            {f"activations/{stage}/"},
        )
        if quality_keys:
            grants[f"quality-reconciler-{stage}-s3-creds"] = (
                set(),
                {f"activations/{stage}/quality-sync/", f"activations/{stage}/run-artifacts/quality/"},
            )
            grants[f"platform-api-quality-reader-{stage}-s3-creds"] = (
                set(),
                {f"activations/{stage}/run-artifacts/quality/"},
            )
    for secret, extra in leaks.items():
        grants[secret][0].update(extra)
        grants[secret][1].update(extra)

    def _identity(_namespace: str, secret: str, **_kw) -> tuple[str, str] | None:  # noqa: ANN003
        return (secret, "secret") if secret in grants else None

    def _client(access_key: str, _secret: str, **_kw) -> _S3Client:  # noqa: ANN003
        return _S3Client(*grants[access_key], objects)

    def _trino_query(cfg, sql: str, **_kw) -> str:  # noqa: ANN001, ANN003
        own = cfg.namespace.removeprefix("olf-")
        if any(f"lakehouse_{stage}." in sql for stage in stages if stage != own):
            raise E2EError("Query failed: Access Denied: Cannot access catalog")
        return "1"

    @contextmanager
    def _port_forward(*_args, **_kwargs):  # noqa: ANN202
        yield 8333

    present = {f"olf-{stage}" for stage in (stages if namespaces is None else namespaces)}
    monkeypatch.setattr(_isolation, "load_provider_contracts_or_raise", lambda _cfg: _contracts(stages))
    monkeypatch.setattr(_isolation.k8s, "resource_exists", lambda _kind, name, *_a, **_kw: name in present)
    monkeypatch.setattr(_isolation.k8s, "port_forward", _port_forward)
    monkeypatch.setattr(_isolation, "_s3_identity", _identity)
    monkeypatch.setattr(_isolation, "_s3_client", _client)
    monkeypatch.setattr(_isolation, "trino_query", _trino_query)
    monkeypatch.setattr(_isolation, "check_keyless_trino", lambda *_args: None)
    monkeypatch.setattr(
        _isolation,
        "check_stage_data_is_its_own",
        lambda _cfg, _contracts, _stage, other: _isolation.NotCompared("deferred", f"{other} has written nothing"),
    )
    monkeypatch.setattr(_isolation, "check_dagster_state_isolation", lambda *_args: None)
    monkeypatch.setenv("OPENLAKEFORGE_OPS_BUCKET_NAME", OPS)
    return objects


def _stage_cfg(tmp_path: Path, stage: str):  # noqa: ANN202
    return replace(e2e_cfg(tmp_path), namespace=f"olf-{stage}", shared_namespace="olf-system")


def _report(tmp_path: Path, stage: str) -> dict:
    return json.loads((tmp_path / f".tmp/e2e-evidence/stage-isolation-olf-{stage}.json").read_text())


@pytest.mark.parametrize(
    ("stages", "pairs"),
    [
        (["dev"], []),
        (["prod", "dev", "dev"], [("dev", "prod"), ("prod", "dev")]),
        (
            ["dev", "uat", "prod"],
            [("dev", "prod"), ("dev", "uat"), ("prod", "dev"), ("prod", "uat"), ("uat", "dev"), ("uat", "prod")],
        ),
    ],
)
def test_every_ordered_pair_of_distinct_stages_is_generated(stages: list[str], pairs: list[tuple[str, str]]) -> None:
    assert _isolation.ordered_stage_pairs(stages) == pairs


def test_the_suites_of_three_stages_together_probe_every_ordered_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stages = ("dev", "uat", "prod")
    _cluster(monkeypatch, stages)

    for stage in stages:
        _isolation.check_stage_isolation(_stage_cfg(tmp_path, stage))

    reports = [_report(tmp_path, stage) for stage in stages]
    covered = {(result["stage"], result["other_stage"]) for report in reports for result in report["results"]}
    assert covered == set(_isolation.ordered_stage_pairs(stages))
    assert sorted(tuple(pair) for report in reports for pair in report["pairs"]) == _isolation.ordered_stage_pairs(
        stages
    )


def test_the_report_records_each_probe_of_each_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cluster(monkeypatch, ("dev", "uat", "prod"))

    _isolation.check_stage_isolation(_stage_cfg(tmp_path, "dev"))

    report = _report(tmp_path, "dev")
    assert {key: report[key] for key in ("schema_version", "check", "stage", "enabled_stages", "pairs")} == {
        "schema_version": 1,
        "check": "stage-isolation",
        "stage": "dev",
        "enabled_stages": ["dev", "prod", "uat"],
        "pairs": [["dev", "prod"], ["dev", "uat"]],
    }
    statuses = {(r["other_stage"], r["probe"]): r["status"] for r in report["results"]}
    for other in ("prod", "uat"):
        assert statuses == statuses | {
            (other, "trino-catalog"): "passed",
            (other, "polaris-catalog"): "passed",
            (other, "object-storage"): "passed",
            (other, "quality-state"): "passed",
            (other, "stage-data"): "deferred",
            (other, "dagster-runs"): "passed",
        }
    assert len(statuses) == 12
    assert all(set(r) == {"stage", "other_stage", "probe", "status", "detail"} for r in report["results"])


def test_a_breach_in_one_pair_fails_the_check_after_probing_every_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cluster(monkeypatch, ("dev", "uat", "prod"), leaks={"seaweedfs-dev-s3-creds": {"prod-silver"}})

    with pytest.raises(E2EError, match=r"object-storage \(dev -> prod\): isolation breach: .*prod-silver"):
        _isolation.check_stage_isolation(_stage_cfg(tmp_path, "dev"))

    statuses = {(r["other_stage"], r["probe"]): r["status"] for r in _report(tmp_path, "dev")["results"]}
    assert statuses[("prod", "object-storage")] == "failed"
    assert statuses[("uat", "object-storage")] == "passed"


def test_a_kubectl_failure_in_one_probe_is_recorded_and_the_other_pairs_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sibling whose Dagster has no Ready endpoint fails its port-forward with KubectlError, not E2EError."""
    _cluster(monkeypatch, ("dev", "uat", "prod"))

    def _dagster(_cfg, _stage: str, other: str) -> None:  # noqa: ANN001
        if other == "prod":
            raise _isolation.k8s.KubectlError("kubectl port-forward exited before localhost:3000 became ready.")

    monkeypatch.setattr(_isolation, "check_dagster_state_isolation", _dagster)

    with pytest.raises(E2EError, match=r"dagster-runs \(dev -> prod\): kubectl port-forward exited"):
        _isolation.check_stage_isolation(_stage_cfg(tmp_path, "dev"))

    results = {(r["other_stage"], r["probe"]): r for r in _report(tmp_path, "dev")["results"]}
    assert results[("prod", "dagster-runs")]["status"] == "failed"
    assert "port-forward exited" in results[("prod", "dagster-runs")]["detail"]
    assert results[("uat", "dagster-runs")]["status"] == "passed"


def test_a_cross_stage_write_that_succeeds_is_removed_and_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    objects = _cluster(
        monkeypatch,
        ("dev", "prod"),
        leaks={"quality-reconciler-prod-s3-creds": {"activations/dev/quality-sync/zz-olf-e2e-isolation-probe"}},
    )

    with pytest.raises(
        E2EError, match=r"quality-state \(prod -> dev\): .* not denied PutObject under activations/dev/"
    ):
        _isolation.check_stage_isolation(_stage_cfg(tmp_path, "prod"))

    assert objects == {}


def test_a_stage_without_quality_keys_skips_the_quality_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cluster(monkeypatch, ("dev", "prod"), quality_keys=False)

    _isolation.check_stage_isolation(_stage_cfg(tmp_path, "dev"))

    (quality,) = [r for r in _report(tmp_path, "dev")["results"] if r["probe"] == "quality-state"]
    assert quality["status"] == "skipped"


def test_a_positive_control_failure_is_not_read_as_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An identity refused everything, its own bucket included, proves nothing about isolation."""
    _cluster(monkeypatch, ("dev", "prod"))
    monkeypatch.setattr(_isolation, "_s3_client", lambda *_a, **_kw: _S3Client(set(), set(), {}))

    with pytest.raises(E2EError, match=r"positive control failed: dev could not HeadBucket on its own bronze"):
        _isolation.check_stage_isolation(_stage_cfg(tmp_path, "dev"))


def test_a_single_stage_deployment_skips_the_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cluster(monkeypatch, ("dev",))

    _isolation.check_stage_isolation(_stage_cfg(tmp_path, "dev"))

    assert not (tmp_path / ".tmp/e2e-evidence").exists()


def test_a_contract_stage_without_a_namespace_is_not_probed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cluster(monkeypatch, ("dev", "uat", "prod"), namespaces=("dev", "prod"))

    _isolation.check_stage_isolation(_stage_cfg(tmp_path, "dev"))

    report = _report(tmp_path, "dev")
    assert report["enabled_stages"] == ["dev", "prod"]
    assert {r["other_stage"] for r in report["results"]} == {"prod"}


OWN = "dev-id\ndev-secret\n"
PROD = "prod-id\nprod-secret\n"
_KEYLESS_CONTRACTS = {
    "stages": {
        stage: {
            "storage": {"silver": {"bucket_name": f"lakehouse-{stage}-silver"}},
            "catalog": {
                "floe_credentials_secret_name": f"polaris-{stage}-processing-creds",
                "floe_client_id_key": "POLARIS_PROCESSING_CLIENT_ID",
                "floe_client_secret_key": "POLARIS_PROCESSING_CLIENT_SECRET",
            },
        }
        for stage in ("dev", "prod")
    }
}


def _keyless(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, tokenless_reads: bool = False) -> list[str]:
    """A stand-in for DEV's keyless catalog: Polaris answers only DEV's own token, and no location outside DEV."""
    statements: list[str] = []

    def _trino_query(_cfg, sql: str, credential: str | None = None, **_kw) -> str:  # noqa: ANN001, ANN003
        statements.append(sql)
        if "lakehouse-prod-silver" in sql:
            raise E2EError("Query failed: Location not allowed")
        if credential == PROD or (credential is not None and credential != OWN and not tokenless_reads):
            raise E2EError("Query failed: Forbidden: Principal 'olf-prod-processing' is not authorized")
        return "1"

    monkeypatch.setattr(_isolation, "trino_query", _trino_query)
    monkeypatch.setattr(_isolation, "_materialized_tables", lambda *_a: [("gold", "customer_health_gold.alpha")])
    monkeypatch.setattr(
        _isolation,
        "processing_credential",
        lambda _cfg, lookup: PROD if "polaris-prod-processing-creds" in lookup else OWN,
    )
    return statements


def test_the_keyless_catalog_refuses_no_token_another_stage_and_a_foreign_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    statements = _keyless(monkeypatch, tmp_path)

    _isolation.check_keyless_trino(e2e_cfg(tmp_path), _KEYLESS_CONTRACTS, "dev", "prod")

    assert any("register_table" in sql and "lakehouse-prod-silver" in sql for sql in statements)
    assert any("location = 's3://lakehouse-prod-silver/" in sql for sql in statements)


def test_a_tokenless_read_is_an_isolation_breach(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _keyless(monkeypatch, tmp_path, tokenless_reads=True)

    with pytest.raises(E2EError, match="isolation breach: a query with no Polaris token"):
        _isolation.check_keyless_trino(e2e_cfg(tmp_path), _KEYLESS_CONTRACTS, "dev", "prod")


def test_trino_isolation_probe_rejects_a_non_denial_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing/broken sibling catalog fails the query too, but for a reason
    other than access control - the probe must not read that as isolation."""
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-dev", shared_namespace="olf-system")

    def _trino_query(_cfg, sql: str, **_identity) -> str:  # noqa: ANN001, ANN003
        if "lakehouse_prod" in sql:
            raise E2EError("Query failed: Catalog 'lakehouse_prod' does not exist")
        return ""

    monkeypatch.setattr(_isolation, "trino_query", _trino_query)

    with pytest.raises(E2EError, match="reason other than an access-control rejection"):
        _isolation._expect_trino_denied(
            cfg, user="olf-dev-runtime", sql="SELECT 1 FROM lakehouse_prod.x", what="read access", sibling="prod"
        )


def test_a_coordinator_that_has_no_sibling_catalog_counts_as_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEV's own Trino holds no PROD catalog (ADR 0019 D8), so Trino answers 'not found'."""
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-dev", shared_namespace="olf-system")

    def _trino_query(_cfg, sql: str, **_identity) -> str:  # noqa: ANN001, ANN003
        raise E2EError("Query failed: line 1:15: Catalog 'lakehouse_prod' not found")

    monkeypatch.setattr(_isolation, "trino_query", _trino_query)

    _isolation._expect_trino_denied(
        cfg, user="olf-dev-runtime", sql="SELECT 1 FROM lakehouse_prod.x", what="read access", sibling="prod"
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


@pytest.fixture(autouse=True)
def _listed_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_isolation, "list_tables", lambda _cfg, _schema, catalog=None: ["alpha", "beta"])


def _materialized(cfg) -> list[tuple[str, str]]:  # noqa: ANN001
    return _isolation._materialized_tables(cfg, "lakehouse_prod")


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
        for layer, table in _materialized(cfg)
    }
    queried: list[str] = []

    def _trino_query(_cfg, sql: str, **_identity) -> str:  # noqa: ANN001, ANN003
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

    # Every table Trino lists in a domain's Silver schema and a product's Gold schema.
    declared = {
        f"{schema}.{table}"
        for schema in (
            *(domain.silver_namespace for domain in cfg.inventory.domains),
            *(product.gold_namespace for product in cfg.inventory.products),
        )
        for table in ("alpha", "beta")
    }
    assert set(queried) == declared
    assert len(queried) == len(declared) > 0


def test_stage_data_check_rejects_a_table_reading_the_sibling_stages_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    layer, table = _materialized(cfg)[0]
    _stub_files(monkeypatch, cfg, {table: f"s3://dev-{layer}/{table}/data/x.parquet"})

    with pytest.raises(E2EError, match=f"{table} reads data files from .*dev-{layer}"):
        _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")


def test_stage_data_check_rejects_sibling_files_copied_into_the_stages_own_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Location passes -- the copy sits in PROD's bucket -- but the name is DEV's."""
    cfg = replace(e2e_cfg(tmp_path), namespace="olf-prod")
    layer, table = _materialized(cfg)[0]
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
    _, empty = _materialized(cfg)[0]
    _stub_files(monkeypatch, cfg, {empty: ""})

    with pytest.raises(E2EError, match="has no data files"):
        _isolation.check_stage_data_is_its_own(cfg, _STAGE_BUCKETS, "prod", "dev")


def _stub_runs(monkeypatch: pytest.MonkeyPatch, runs: dict[str, set[str]]) -> None:
    monkeypatch.setattr(
        _isolation,
        "_dagster_run_ids",
        lambda _service, namespace, _log, **_kwargs: runs[namespace.removeprefix("olf-")],
    )


def test_bucket_listing_failure_surfaces_as_an_e2e_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DeniedPaginator:
        def paginate(self, **_kwargs):  # noqa: ANN202
            raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "ListObjectsV2")

    class _Client:
        def get_paginator(self, _name: str) -> _DeniedPaginator:
            return _DeniedPaginator()

    @contextmanager
    def _port_forward(*_args, **_kwargs):  # noqa: ANN202
        yield 8333

    monkeypatch.setattr(_isolation, "_s3_identity", lambda *_args, **_kwargs: ("key", "secret"))
    monkeypatch.setattr(_isolation, "_s3_client", lambda *_args, **_kwargs: _Client())
    monkeypatch.setattr(_isolation.k8s, "port_forward", _port_forward)

    with pytest.raises(E2EError, match="could not list prod's bronze bucket 'prod-bronze'"):
        _isolation.stage_bucket_objects(
            {"stages": {"prod": {"storage": {"bronze": {"bucket_name": "prod-bronze"}}}}},
            "prod",
            ("bronze",),
            namespace="olf-system",
        )


def test_dagster_run_listing_failure_surfaces_as_an_e2e_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The e2e CLI reports E2EError; a raw client error would be a traceback."""
    from openlakeforge_services.base import ServiceClientError
    from openlakeforge_services.dagster import DagsterClient

    @contextmanager
    def _port_forward(*_args, **_kwargs):  # noqa: ANN202
        yield 18080

    def _graphql(*_args, **_kwargs):  # noqa: ANN202
        raise ServiceClientError("HTTP 502")

    monkeypatch.setattr(_isolation.k8s, "port_forward", _port_forward)
    monkeypatch.setattr(DagsterClient, "graphql", _graphql)

    with pytest.raises(E2EError, match="could not list Dagster runs in olf-prod: HTTP 502"):
        _isolation._dagster_run_ids("dagster-dagster-webserver", "olf-prod", "/tmp/log")


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
