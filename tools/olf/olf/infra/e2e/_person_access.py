"""Per-person data access (ADR 0019, verification item 10): a person's Dex token, sent to Trino's query route
the way `olf query` sends it, reaches exactly what the role's `data_access` row grants, and Polaris decides.

Each role reads a table of every layer on the run's stage, lists every stage's schemas, and writes a probe
table on every stage. A role that may read but not write gets a read-only vended credential, so its write is
refused by storage rather than by Polaris; the check tells the two apart.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

from olf.common import log
from olf.infra import trino
from olf.infra.e2e._perimeter_bearer import CLI_CLIENT, issue_id_token
from olf.infra.e2e._shell import E2EConfig, E2EError
from olf.infra.e2e._trino import PROBE_SCHEMA, list_tables, stage_catalog_name
from olf.platform import query, session

Outcome = Literal["ok", "refused", "read-only"]
LAYERS = ("bronze", "silver", "gold")
PROBE_TABLE = f"{PROBE_SCHEMA}.person_probe"
SCOPE = "openid email profile groups federated:id"
# Dex's ID tokens last five minutes; a fresh one leaves the slowest query room to finish.
TOKEN_REUSE_SECONDS = 180


def expected(row: Mapping[str, str], layer: str | None, *, write: bool) -> Outcome:
    """What a role's row on a stage allows on a table of `layer`; None is a namespace outside the layers, which
    only a row naming every layer reaches (Gold read alone is granted per Gold namespace)."""
    access = row.get(layer) if layer else (row.get("gold") if len(row) == len(LAYERS) else None)
    if access is None:
        return "refused"
    return "ok" if not write or access == "write" else "read-only"


def check_person_decisions(
    stages: list[str],
    data_access: Mapping[str, Mapping[str, Mapping[str, str]]],
    tables: Mapping[str, str],
    run: Callable[[str, str, str], Outcome],
) -> None:
    """`tables` maps a layer to a `<schema>.<table>` on `stages[0]`; `run(role, stage, sql)` runs as that role."""
    roles = list(data_access)

    def require(role: str, stage: str, sql: str, want: Outcome) -> None:
        got = run(role, stage, sql)
        if got != want:
            raise E2EError(f"{role} on {stage}: {sql!r} expected {want}, got {got}")

    for stage in stages:
        writers = [role for role in roles if expected(data_access[role].get(stage, {}), None, write=True) == "ok"]
        if not writers:
            raise E2EError(f"no role writes on {stage}; the write cases would pass on refusals alone")
        log.step(f"Creating {stage}.{PROBE_TABLE} as {writers[0]}...")
        require(writers[0], stage, f"CREATE SCHEMA IF NOT EXISTS {PROBE_SCHEMA}", "ok")
        require(writers[0], stage, f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} (probe integer)", "ok")
    for role in roles:
        log.step(f"Checking {role}'s data access as a person...")
        admitted = False
        for stage in stages:
            row = data_access[role].get(stage, {})
            require(role, stage, "SHOW SCHEMAS", "ok" if row else "refused")
            require(role, stage, f"SELECT count(*) FROM {PROBE_TABLE}", expected(row, None, write=False))
            require(role, stage, f"INSERT INTO {PROBE_TABLE} VALUES (1)", expected(row, None, write=True))
            admitted = admitted or bool(row)
        for layer, table in tables.items():
            allowed = expected(data_access[role].get(stages[0], {}), layer, write=False)
            require(role, stages[0], f"SELECT count(*) FROM {table}", allowed)
        if not admitted:
            raise E2EError(f"{role} reaches no stage; its checks would pass on refusals alone")


def outcome(call: Callable[[], object]) -> Outcome:
    """Run `call`, telling a Polaris refusal from storage refusing a read-only credential. The markers are the
    upstream messages `olf query` quotes, not its own wording."""
    try:
        call()
    except session.PlatformError as exc:
        text = str(exc)
        if "Forbidden:" in text and "not authorized" in text:
            return "refused"
        # Trino's own rules also say "Access Denied" ("Cannot access catalog"); only a failed write to storage
        # is the read-only credential.
        if "Access Denied" in text and ("Put failed" in text or "Error committing write" in text):
            return "read-only"
        raise E2EError(f"the query failed for a reason other than an access decision: {text}") from exc
    return "ok"


def layer_tables(cfg: E2EConfig) -> dict[str, str]:
    """A table of each layer the run's pipelines filled, read through the workload path; Silver and Gold are
    required."""
    names = {
        "bronze": cfg.inventory.bronze_namespace_names,
        "silver": cfg.inventory.silver_namespace_names,
        "gold": cfg.inventory.gold_namespace_names,
    }
    tables: dict[str, str] = {}
    for layer in LAYERS:
        for schema in sorted(names[layer]):
            found = list_tables(cfg, schema)
            if found:
                tables[layer] = f"{schema}.{found[0]}"
                break
    missing = [layer for layer in ("silver", "gold") if layer not in tables]
    if missing:
        raise E2EError(f"no {' or '.join(missing)} table to check people's reads against")
    return tables


def check_person_access(
    cfg: E2EConfig,
    identity: Mapping[str, Any],
    stages: list[str],
    routes: Mapping[str, str],
    logins: Mapping[str | None, str],
) -> None:
    """Needs the stub users already running, one per mapped role (`logins`). `stages` are the contract's."""
    data_access = identity["roles"].get("data_access") or {}
    client = identity.get("clients", {}).get(CLI_CLIENT)
    if "query_api" not in identity["roles"]["grants"] or "shared/query" not in routes or not client:
        log.info("Skipping the person-access checks: the contract has no query route for people.")
        return
    roles = {role: login for role, login in logins.items() if role is not None and role in data_access}
    url = f"{routes['shared/query'].rstrip('/')}/v1/statement"
    run_stage = trino.stage_from_catalog(stage_catalog_name(cfg))
    ordered = [run_stage, *(stage for stage in stages if stage != run_stage)]
    # RFC 8252 loopback redirect: Dex accepts any port, none is registered.
    loopback = f"http://127.0.0.1:{20000 + secrets.randbelow(40000)}/callback"
    tokens: dict[str, tuple[float, str]] = {}

    def bearer(role: str) -> str:
        if role not in tokens or time.monotonic() - tokens[role][0] > TOKEN_REUSE_SECONDS:
            token = issue_id_token(cfg, identity, roles[role], client["client_id"], loopback, scope=SCOPE)
            tokens[role] = (time.monotonic(), token)
        return tokens[role][1]

    def run(role: str, stage: str, sql: str) -> Outcome:
        user = str(session.claims(bearer(role))["email"])
        return outcome(lambda: query.run_statement(url, sql, bearer=lambda: bearer(role), user=user, catalog=stage))

    check_person_decisions(ordered, {role: data_access[role] for role in roles}, layer_tables(cfg), run)
    log.info(
        "Not checking a vended credential against another table's location: SQL cannot address a path, and "
        "SeaweedFS enforcing the session policy is verification item 1."
    )
