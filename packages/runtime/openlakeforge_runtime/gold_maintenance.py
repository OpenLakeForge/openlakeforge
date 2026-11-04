from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Any

from openlakeforge_runtime.dbt.render_profiles import discover_project_dirs

# Trino refuses `expire_snapshots` / `remove_orphan_files` below
# `iceberg.expire-snapshots.min-retention` / `iceberg.remove-orphan-files.min-retention`,
# both 7d by default. infra/terraform/modules/query/trino sets neither, so 7d is what a
# stage enforces; checking it here fails the run before any table is touched, with the
# reason, instead of once per table from the server.
MIN_RETENTION_DAYS = 7


class RetentionBelowMinimum(ValueError):
    pass


def check_retention(older_than_days: int, orphan_delay_days: int) -> None:
    for name, days in (("older_than_days", older_than_days), ("orphan_delay_days", orphan_delay_days)):
        if days < MIN_RETENTION_DAYS:
            raise RetentionBelowMinimum(
                f"{name}={days} is below Trino's Iceberg minimum retention of {MIN_RETENTION_DAYS} days"
            )


def connect() -> Any:
    """A Trino connection as the stage's dbt identity: the same variables the dbt profile reads."""
    import trino  # deferred: the module loads (and is tested) without a Trino client

    env = os.environ
    https = env.get("OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME", "http") == "https"
    user = env["OPENLAKEFORGE_DBT_TRINO_USER"]
    password = env.get("OPENLAKEFORGE_DBT_TRINO_PASSWORD", "")
    ldap = env.get("OPENLAKEFORGE_DBT_TRINO_METHOD", "none") == "ldap"
    return trino.dbapi.connect(
        host=env["OPENLAKEFORGE_QUERY_TRINO_HOST"],
        port=int(env["OPENLAKEFORGE_QUERY_TRINO_TLS_PORT" if https else "OPENLAKEFORGE_QUERY_TRINO_PORT"]),
        user=user,
        catalog=env["OPENLAKEFORGE_CATALOG_NAME"],
        http_scheme="https" if https else "http",
        auth=trino.auth.BasicAuthentication(user, password) if ldap else None,
        verify=env.get("OPENLAKEFORGE_QUERY_TRINO_CA") or True,
    )


def gold_schemas(gold_root: Any) -> list[str]:
    """Each product's Gold schema, named as the dbt profile names it (render_profiles.render_profile)."""
    prefix = os.environ.get("OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX", "")
    return [f"{prefix}{project_dir.parent.name}_gold" for project_dir in discover_project_dirs(gold_root)]


def _rows(connection: Any, sql: str) -> list[tuple]:
    cursor = connection.cursor()
    cursor.execute(sql)
    return list(cursor.fetchall())


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def maintain_gold(
    connection: Any,
    schemas: Iterable[str],
    *,
    older_than_days: int,
    orphan_delay_days: int,
    dry_run: bool,
    log: Callable[[str, dict], None],
) -> list[dict]:
    """Expire snapshots and remove orphan files of every Gold table, one result per table.

    A failing table does not stop the others; the caller fails the run on any `error`. Both
    procedures are idempotent, so rerunning after an interruption only repeats the remaining work.
    The dry run counts what `expire_snapshots` would drop (snapshots past the window that no
    branch or tag holds); Trino can list no orphan files, so those are not reported.
    """
    check_retention(older_than_days, orphan_delay_days)
    results: list[dict] = []
    for schema in schemas:
        tables = _rows(
            connection,
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE' ORDER BY table_name",
        )
        for (table,) in tables:
            relation = f"{_quote(schema)}.{_quote(table)}"
            result: dict = {"schema": schema, "table": table, "dry_run": dry_run}
            try:
                if dry_run:
                    metadata = f"{_quote(schema)}.{_quote(table + '$snapshots')}"
                    refs = f"{_quote(schema)}.{_quote(table + '$refs')}"
                    ((count,),) = _rows(
                        connection,
                        f"SELECT count(*) FROM {metadata} WHERE committed_at < current_timestamp - "
                        f"INTERVAL '{older_than_days}' DAY AND snapshot_id NOT IN (SELECT snapshot_id FROM {refs})",
                    )
                    result["expirable_snapshots"] = count
                else:
                    _rows(
                        connection,
                        f"ALTER TABLE {relation} EXECUTE expire_snapshots(retention_threshold => '{older_than_days}d')",
                    )
                    _rows(
                        connection,
                        f"ALTER TABLE {relation} EXECUTE remove_orphan_files(retention_threshold => '{orphan_delay_days}d')",
                    )
            except Exception as error:  # recorded per table, then the caller fails the run
                result["error"] = str(error)
            log("Maintained Gold table", result)
            results.append(result)
    return results
