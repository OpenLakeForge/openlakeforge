"""Read-only durability check (#908): what a stage held before an upgrade or a restore, and what is still there.

`e2e run` launches the product pipelines first, and that rebuilds whatever was lost. `capture` only reads the
stage into a baseline file and `verify` reads it again and reports every difference, so a lost run, table,
dashboard, entity or object stays visible. Neither launches a pipeline or writes to the cluster.

The comparison works on `Sources`, one callable per kind of state, so it needs no cluster (as `_issuer_outage.run_drill`
needs none); `cluster_sources` binds them to a deployed stack. The baseline is plain JSON and `write_baseline` refuses
to write one that holds the value of a Secret.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olf.common import config, log
from olf.infra import project_activation
from olf.infra.artifact_store import ArtifactStoreError, S3RevisionStore, artifact_bucket, artifact_storage_client
from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e._assertions import _openmetadata_e2e_login, openmetadata_client, superset_client
from olf.infra.e2e._dagster import dagster_client
from olf.infra.e2e._isolation import _materialized_tables, stage_bucket_items
from olf.infra.e2e._layers import configured_layers
from olf.infra.e2e._secret_exposure import _credentials, migrated_credentials, scan, scan_namespaces
from olf.infra.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise
from olf.infra.e2e._trino import check_trino_tables_and_marts, stage_catalog_name, trino_query, trino_scalar
from olf.infra.openmetadata._auth import admin_password

BASELINE_VERSION = 1
LAYERS = ("bronze", "silver", "gold")
# Dagster's runsOrError pages by limit; a listing that fills it may be cut short, so it is refused, not trusted.
DAGSTER_RUN_LIMIT = 10_000
OPENMETADATA_PAGE_SIZE = 1000
SUPERSET_PAGE_SIZE = 100
SEARCH_INDEX = "table_search_index"

State = dict[str, Any]


@dataclass(frozen=True)
class Sources:
    """One reader per kind of state, each returning the JSON-able value stored under the same key of a baseline."""

    tables: Callable[[], State]  # "schema.table" -> {"rows": int, "snapshot_id": str}
    dagster_runs: Callable[[], list[str]]  # run ids
    dashboards: Callable[[], list[str]]  # Superset dashboard uuids
    entities: Callable[[], list[str]]  # OpenMetadata fully qualified names
    search_hits: Callable[[], int]  # hits for a table search
    activation: Callable[[], State]  # pointer key -> activation revision
    objects: Callable[[], State]  # "bucket/key" -> size in bytes

    def read(self) -> State:
        runs = sorted(self.dagster_runs())
        return {
            "tables": self.tables(),
            "dagster": {"run_count": len(runs), "run_ids": runs},
            "superset": {"dashboards": sorted(self.dashboards())},
            "openmetadata": {"entities": sorted(self.entities()), "search_hits": self.search_hits()},
            "activation": self.activation(),
            "objects": self.objects(),
        }


def capture(sources: Sources, *, stage: str) -> State:
    return {"version": BASELINE_VERSION, "stage": stage, **sources.read()}


def write_baseline(baseline: State, path: Path, credentials: Mapping[str, str]) -> None:
    """Write `baseline` to `path` unless it holds the value of any of `credentials` (a Secret label to its value)."""
    text = json.dumps(baseline, indent=1, sort_keys=True) + "\n"
    scan(credentials, {"the durability baseline": text})
    path.write_text(text)


def load_baseline(path: Path) -> State:
    try:
        baseline = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise E2EError(f"cannot read the durability baseline {path}: {exc}") from exc
    if not isinstance(baseline, dict) or baseline.get("version") != BASELINE_VERSION:
        raise E2EError(f"{path} is not a version {BASELINE_VERSION} durability baseline.")
    return baseline


def differences(baseline: State, current: State) -> list[str]:
    """Every way `current` has lost something `baseline` held. New state (a later run, a new object) is not one."""
    found: list[str] = []
    for name, before in baseline["tables"].items():
        after = current["tables"].get(name)
        if after is None:
            found.append(f"table {name} is missing")
            continue
        if after["rows"] < before["rows"]:
            found.append(f"table {name} has fewer rows: {before['rows']} before, {after['rows']} now")
        if after["snapshot_id"] != before["snapshot_id"]:
            found.append(
                f"table {name} changed its current snapshot: {before['snapshot_id']} before, {after['snapshot_id']} now"
            )
    found += [
        f"Dagster run {run} is missing"
        for run in _missing(baseline["dagster"]["run_ids"], current["dagster"]["run_ids"])
    ]
    found += [
        f"Superset dashboard {uuid} is missing"
        for uuid in _missing(baseline["superset"]["dashboards"], current["superset"]["dashboards"])
    ]
    found += [
        f"OpenMetadata entity {fqn} is missing"
        for fqn in _missing(baseline["openmetadata"]["entities"], current["openmetadata"]["entities"])
    ]
    if baseline["openmetadata"]["search_hits"] > 0 and current["openmetadata"]["search_hits"] == 0:
        found.append(
            f"OpenMetadata search finds nothing; it found {baseline['openmetadata']['search_hits']} hits before"
        )
    for key, revision in baseline["activation"].items():
        if current["activation"].get(key) != revision:
            found.append(f"activation pointer {key} was {revision}, now {current['activation'].get(key)}")
    for key, size in baseline["objects"].items():
        if key not in current["objects"]:
            found.append(f"object {key} is missing")
        elif current["objects"][key] != size:
            found.append(f"object {key} changed size: {size} bytes before, {current['objects'][key]} now")
    return found


def _missing(before: list[str], after: list[str]) -> list[str]:
    return sorted(set(before) - set(after))


def verify(baseline: State, sources: Sources) -> list[str]:
    """The differences between `baseline` and what `sources` read now, including a source that cannot be read."""
    try:
        current = capture(sources, stage=baseline["stage"])
    except E2EError as exc:
        return [f"the stage could not be read: {exc}"]
    return differences(baseline, current)


def cluster_sources(cfg: E2EConfig) -> Sources:
    """`Sources` over a deployed local stack: Trino, Dagster, Superset and OpenMetadata APIs, and the S3 buckets."""
    stage = config.env(CONTRACT_STAGE_ENV, "dev", environ=cfg.environ)
    # A layer the stage does not run has no service to ask (Slim runs neither): it records and verifies empty.
    layers = configured_layers(cfg)
    return Sources(
        tables=lambda: _tables(cfg),
        dagster_runs=lambda: _dagster_runs(cfg),
        dashboards=(lambda: _dashboards(cfg)) if layers["analytics"] else list,
        entities=(lambda: _entities(cfg)) if layers["governance"] else list,
        search_hits=(lambda: _search_hits(cfg)) if layers["governance"] else int,
        activation=lambda: _activation(cfg, stage),
        objects=lambda: _objects(cfg, stage),
    )


def _tables(cfg: E2EConfig) -> State:
    catalog = stage_catalog_name(cfg)
    tables: State = {}
    for _, table in _materialized_tables(cfg, catalog):
        schema, name = table.split(".", 1)
        tables[table] = {
            "rows": int(trino_scalar(cfg, f"SELECT count(*) FROM {catalog}.{table}")),
            "snapshot_id": _current_snapshot(cfg, f'{catalog}.{schema}."{name}$refs"'),
        }
    return tables


def _current_snapshot(cfg: E2EConfig, refs_table: str) -> str:
    # The `main` ref is the snapshot the table serves; `$snapshots` also lists the ones a rollback left behind.
    # A table that never committed has no ref.
    return next(iter(trino_query(cfg, f"SELECT snapshot_id FROM {refs_table} WHERE name = 'main'").split()), "")


def _dagster_runs(cfg: E2EConfig) -> list[str]:
    with dagster_client(cfg) as client:
        runs = client.list_runs(limit=DAGSTER_RUN_LIMIT + 1)
    if len(runs) > DAGSTER_RUN_LIMIT:
        raise E2EError(f"Dagster holds more than {DAGSTER_RUN_LIMIT} runs; the baseline would be cut short.")
    return [run.run_id for run in runs]


def _dashboards(cfg: E2EConfig) -> list[str]:
    with superset_client(cfg) as client:
        client.login()
        return dashboard_uuids(lambda q: client.request("GET", "/api/v1/dashboard/", params={"q": json.dumps(q)}))


def dashboard_uuids(get_page: Callable[[dict[str, Any]], Mapping[str, Any]]) -> list[str]:
    """Every dashboard uuid, paged the way SupersetClient.databases pages; not yet run against a live Superset."""
    found: list[str] = []
    page = 0
    while True:
        body = get_page({"columns": ["uuid"], "page": page, "page_size": SUPERSET_PAGE_SIZE})
        rows = body.get("result", [])
        found += [str(row["uuid"]).lower() for row in rows]
        page += 1
        if not rows or page * SUPERSET_PAGE_SIZE >= body.get("count", 0):
            return found


def _entities(cfg: E2EConfig) -> list[str]:
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=cfg.environ)
    with openmetadata_client(cfg) as (client, token):
        _login(cfg, client, token)
        # Typed, so a domain and a data product of one name cannot hide each other's loss.
        names = [f"domain:{name}" for name, _ in client.list_domains()]
        names += [f"dataProduct:{product.name}" for product in client.list_data_products()]
        after = ""
        while database_fqn:
            params: dict[str, Any] = {"database": database_fqn, "limit": OPENMETADATA_PAGE_SIZE}
            if after:
                params["after"] = after
            page = client.request("GET", "/api/v1/tables", params=params)
            names += [f"table:{table['fullyQualifiedName']}" for table in page.get("data", [])]
            after = page.get("paging", {}).get("after") or ""
            if not after:
                break
    return names


def _search_hits(cfg: E2EConfig) -> int:
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=cfg.environ)
    if not database_fqn:
        raise E2EError("OPENLAKEFORGE_CATALOG_DATABASE_FQN is unset; cannot scope the search to this stage.")
    # OpenMetadata is shared by every stage, so an unscoped count would stay positive on another stage's tables.
    # Not yet run against a live OpenMetadata.
    query = f'database.fullyQualifiedName:"{database_fqn}"'
    with openmetadata_client(cfg) as (client, token):
        _login(cfg, client, token)
        result = client.request("GET", "/api/v1/search/query", params={"q": query, "index": SEARCH_INDEX, "size": 1})
    return int(result["hits"]["total"]["value"])


def _login(cfg: E2EConfig, client: Any, token: str | None) -> None:
    password = (
        "" if token else admin_password(cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ)
    )
    _openmetadata_e2e_login(client, password)


def _activation(cfg: E2EConfig, stage: str) -> State:
    bucket = artifact_bucket(environ=cfg.environ)
    key = project_activation.active_key(stage)
    try:
        with artifact_storage_client("port-forward", bucket, environ=cfg.environ) as client:
            pointer = S3RevisionStore(client, bucket).read(key)
    except ArtifactStoreError as exc:
        raise E2EError(str(exc)) from exc
    # No pointer is a stage with no project activated: nothing to preserve.
    return {} if pointer is None else {key: json.loads(pointer)["activation_revision"]}


def _objects(cfg: E2EConfig, stage: str) -> State:
    contracts = load_provider_contracts_or_raise(cfg)
    items = stage_bucket_items(contracts, stage, LAYERS, namespace=cfg.platform_namespace, environ=cfg.environ)
    objects: State = {path: item["Size"] for path, item in items.items()}
    bucket = contracts["shared"]["ops_storage"]["bucket_name"]
    try:
        with artifact_storage_client("port-forward", bucket, environ=cfg.environ) as client:
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                objects.update({f"{bucket}/{item['Key']}": item["Size"] for item in page.get("Contents", [])})
    except ArtifactStoreError as exc:
        raise E2EError(str(exc)) from exc
    return objects


def secret_values(cfg: E2EConfig) -> dict[str, str]:
    """The Secret values a baseline must not hold: the identity set and the credentials moved out of Terraform."""
    contracts = load_provider_contracts_or_raise(cfg)
    return {
        **_credentials(cfg, contracts["shared"]["identity"]),
        **migrated_credentials(cfg, scan_namespaces(cfg, contracts)),
    }


def capture_to_file(cfg: E2EConfig, output: Path) -> None:
    # A baseline of an empty lake would verify anything.
    check_trino_tables_and_marts(cfg)
    log.step("Capturing the durability baseline...")
    baseline = capture(cluster_sources(cfg), stage=config.env(CONTRACT_STAGE_ENV, "dev", environ=cfg.environ))
    write_baseline(baseline, output, secret_values(cfg))
    log.info(f"Wrote {output}: {len(baseline['tables'])} tables, {len(baseline['objects'])} objects.")


def verify_file(cfg: E2EConfig, baseline_path: Path) -> None:
    baseline = load_baseline(baseline_path)
    stage = config.env(CONTRACT_STAGE_ENV, "dev", environ=cfg.environ)
    if baseline["stage"] != stage:
        raise E2EError(f"{baseline_path} is of stage {baseline['stage']}, not {stage}; pass --stage.")
    log.step(f"Verifying the stage against {baseline_path}...")
    problems = verify(baseline, cluster_sources(cfg))
    if problems:
        raise E2EError(f"{len(problems)} difference(s) from the baseline:\n" + "\n".join(problems))
    log.info("Everything the baseline held is still there.")
