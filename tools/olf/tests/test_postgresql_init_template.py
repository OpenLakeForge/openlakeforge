"""Regression guard for the PostgreSQL bootstrap templates' cross-stage
`CONNECT` isolation (#225).

Postgres grants `CONNECT` on every new database to `PUBLIC` by default. Any
role that can authenticate to the shared server - e.g. `dagster_dev`'s
credential - could reach `dagster_prod` unless the bootstrap script
explicitly revokes it and grants `CONNECT` back to the owning role only.

This can't be verified end-to-end without a cluster (see the issue), so this
asserts the rendered *shape* of the templates instead: the revoke/grant pair
is emitted inside the one function every provisioned database routes
through, in both the in-cluster module (`storage/postgresql`, used by the
local and azure-poc roots - every stage's Dagster and Superset database,
plus Polaris) and the RDS module (`storage/rds-postgresql`, used by the
aws-poc root - every stage's Dagster and Superset database; RDS has no
Polaris database, aws-poc uses Glue for the catalog instead).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
IN_CLUSTER_TEMPLATE = REPO_ROOT / "infra/terraform/modules/storage/postgresql/templates/init.sh.tftpl"
RDS_TEMPLATE = REPO_ROOT / "infra/terraform/modules/storage/rds-postgresql/templates/init.sh.tftpl"


def _function_body(template: Path, function_name: str) -> str:
    text = template.read_text()
    match = re.search(rf"{re.escape(function_name)}\(\) \{{\n(.*?)\n\}}\n", text, re.DOTALL)
    assert match, f"{function_name}() not found in {template}"
    return match.group(1)


def test_in_cluster_template_revokes_public_connect_and_grants_the_owner() -> None:
    body = _function_body(IN_CLUSTER_TEMPLATE, "create_database_if_missing")

    revoke = 'REVOKE CONNECT ON DATABASE \\"$db_name\\" FROM PUBLIC'
    grant = 'GRANT CONNECT ON DATABASE \\"$db_name\\" TO \\"$owner_name\\"'
    assert revoke in body
    assert grant in body

    # A regression that deletes the revoke but keeps the grant (or vice
    # versa) would still leave every other stage able to connect, so both
    # halves of the pair are required, not just one.
    assert body.index(revoke) < body.index(grant), "REVOKE must run before the explicit GRANT back to the owner"


def test_in_cluster_template_routes_every_database_through_the_revoke() -> None:
    text = IN_CLUSTER_TEMPLATE.read_text()

    # Every stage's dagster_<stage>/superset_<stage> database is provisioned
    # through this loop over database_env_prefixes, not a one-off call.
    loop = re.search(r"%\{ for prefix in database_env_prefixes ~\}(.*?)%\{ endfor ~\}", text, re.DOTALL)
    assert loop, "database_env_prefixes loop not found in template"
    assert "create_database_if_missing" in loop.group(1)

    # Polaris shares the same server and the same exposure; it must go
    # through the same function rather than a separate, unrevoked path.
    assert 'create_database_if_missing "$POLARIS_DB_NAME" "$POLARIS_DB_USER"' in text


def test_rds_template_revokes_public_connect_and_grants_the_owner() -> None:
    body = _function_body(RDS_TEMPLATE, "create_role_and_db")

    # This template uses psql `--set` + `\gexec` with `format(... %I ...)`
    # rather than shell interpolation - `%I` is what makes the generated
    # identifiers injection-safe, so assert the format() calls, not raw SQL.
    revoke = "format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'db_name')\\gexec"
    grant = "format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'db_user')\\gexec"
    assert revoke in body
    assert grant in body
    assert body.index(revoke) < body.index(grant), "REVOKE must run before the explicit GRANT back to the owner"


def test_rds_template_routes_every_database_through_the_revoke() -> None:
    text = RDS_TEMPLATE.read_text()

    loop = re.search(r"%\{ for prefix in database_env_prefixes ~\}(.*?)%\{ endfor ~\}", text, re.DOTALL)
    assert loop, "database_env_prefixes loop not found in template"
    assert "create_role_and_db" in loop.group(1)
