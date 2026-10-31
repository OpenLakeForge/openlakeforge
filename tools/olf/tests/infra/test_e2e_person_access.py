from __future__ import annotations

from typing import Any

import pytest

from olf.infra.e2e import _person_access as person
from olf.infra.e2e._shell import E2EError
from olf.platform import session

# release/identity-roles.yaml with DEV and PROD enabled.
DATA_ACCESS = {
    "platform-admin": {stage: dict.fromkeys(person.LAYERS, "write") for stage in ("dev", "prod")},
    "data-engineer": {"dev": dict.fromkeys(person.LAYERS, "write"), "prod": dict.fromkeys(person.LAYERS, "read")},
    "analyst": {"dev": {"gold": "read"}, "prod": {"gold": "read"}},
    "viewer": {"prod": {"gold": "read"}},
}
TABLES = {"bronze": "sales_bronze.raw", "silver": "sales_silver.orders", "gold": "sales_gold.revenue"}


class Polaris:
    """Decides as the catalog roles the bootstrap derives from a row do (ADR 0019, section 5), not from
    `expected`: olf-write, olf-read, or olf-gold-read with grants on Gold namespaces only."""

    def __init__(
        self, data_access: dict[str, Any] = DATA_ACCESS, overrides: dict[Any, person.Outcome] | None = None
    ) -> None:
        self.data_access = data_access
        self.overrides = overrides or {}
        self.calls: list[tuple[str, str, str]] = []

    def catalog_role(self, role: str, stage: str) -> str | None:
        row = self.data_access[role].get(stage, {})
        if not row:
            return None
        return "write" if row.get("gold") == "write" else "read" if len(row) == 3 else "gold-read"

    def run(self, role: str, stage: str, sql: str) -> person.Outcome:
        self.calls.append((role, stage, sql))
        if (role, stage, sql.split()[0]) in self.overrides:
            return self.overrides[(role, stage, sql.split()[0])]
        held = self.catalog_role(role, stage)
        if held is None:
            return "refused"
        if sql == "SHOW SCHEMAS":
            return "ok"
        if sql.startswith("CREATE"):
            return "ok" if held == "write" else "refused"
        gold = "_gold." in sql
        if held == "gold-read" and not gold:
            return "refused"
        writing = sql.startswith("INSERT")
        return "read-only" if writing and held != "write" else "ok"


def _check(polaris: Polaris, data_access: dict[str, Any] = DATA_ACCESS) -> None:
    person.check_person_decisions(["dev", "prod"], data_access, TABLES, polaris.run)


def test_every_role_reaches_exactly_its_data_access_row() -> None:
    polaris = Polaris()
    _check(polaris)
    calls = set(polaris.calls)
    assert ("analyst", "dev", "SELECT count(*) FROM sales_silver.orders") in calls
    assert ("viewer", "dev", "SELECT count(*) FROM sales_gold.revenue") in calls
    assert ("data-engineer", "prod", f"INSERT INTO {person.PROBE_TABLE} VALUES (1)") in calls
    assert ("platform-admin", "prod", f"INSERT INTO {person.PROBE_TABLE} VALUES (1)") in calls


def test_the_probe_tables_exist_before_any_role_uses_them() -> None:
    polaris = Polaris()
    _check(polaris)
    creates = [i for i, call in enumerate(polaris.calls) if call[2].startswith("CREATE")]
    assert [polaris.calls[i][:2] for i in creates] == [("platform-admin", "dev")] * 2 + [("platform-admin", "prod")] * 2
    assert max(creates) < min(i for i, call in enumerate(polaris.calls) if not call[2].startswith("CREATE"))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({("analyst", "dev", "SELECT"): "ok"}, "analyst on dev"),
        ({("viewer", "dev", "SELECT"): "ok"}, "viewer on dev"),
        ({("data-engineer", "prod", "INSERT"): "ok"}, "data-engineer on prod"),
        # Refused by Polaris instead of by storage: the vended credential was not the read-only one.
        ({("data-engineer", "prod", "INSERT"): "refused"}, "expected read-only, got refused"),
        ({("data-engineer", "dev", "INSERT"): "read-only"}, "data-engineer on dev"),
        ({("platform-admin", "prod", "INSERT"): "refused"}, "platform-admin on prod"),
        ({("viewer", "prod", "SHOW"): "refused"}, "viewer on prod"),
        ({("platform-admin", "prod", "CREATE"): "refused"}, "platform-admin on prod"),
    ],
)
def test_a_decision_against_the_row_fails_the_check(override: dict[Any, person.Outcome], message: str) -> None:
    with pytest.raises(E2EError, match=message):
        _check(Polaris(overrides=override))


def test_a_stage_no_role_writes_fails_rather_than_passing_on_refusals() -> None:
    data_access = {**DATA_ACCESS, "platform-admin": {"dev": DATA_ACCESS["platform-admin"]["dev"]}}
    with pytest.raises(E2EError, match="no role writes on prod"):
        _check(Polaris(data_access), data_access)


def test_a_role_with_no_row_on_the_deployed_stages_is_checked_for_refusals_only() -> None:
    # A DEV-only deployment: viewer reads PROD alone, which is not deployed.
    polaris = Polaris()
    person.check_person_decisions(["dev"], DATA_ACCESS, TABLES, polaris.run)
    viewer = {(stage, sql) for role, stage, sql in polaris.calls if role == "viewer"}
    assert ("dev", "SHOW SCHEMAS") in viewer
    assert ("dev", "SELECT count(*) FROM sales_gold.revenue") in viewer


def test_a_role_admitted_where_its_row_refuses_still_fails_on_a_single_stage() -> None:
    with pytest.raises(E2EError, match="viewer on dev"):
        person.check_person_decisions(
            ["dev"], DATA_ACCESS, TABLES, Polaris(overrides={("viewer", "dev", "SHOW"): "ok"}).run
        )


@pytest.mark.parametrize(
    ("row", "layer", "write", "want"),
    [
        ({"gold": "read"}, "gold", False, "ok"),
        ({"gold": "read"}, "gold", True, "read-only"),
        ({"gold": "read"}, "silver", False, "refused"),
        ({"gold": "read"}, None, False, "refused"),
        (dict.fromkeys(person.LAYERS, "read"), None, True, "read-only"),
        (dict.fromkeys(person.LAYERS, "write"), None, True, "ok"),
        ({}, "gold", False, "refused"),
    ],
)
def test_expected_follows_the_row(row: dict[str, str], layer: str | None, write: bool, want: str) -> None:
    assert person.expected(row, layer, write=write) == want


def _fails(message: str) -> Any:
    def call() -> None:
        raise session.PlatformError(message)

    return call


def test_outcome_tells_polaris_from_storage_by_the_upstream_message() -> None:
    assert person.outcome(lambda: None) == "ok"
    polaris = "Polaris refused this on stage dev ...\nForbidden: Principal 'github' is not authorized for op LOAD_TABLE"
    assert person.outcome(_fails(polaris)) == "refused"
    storage = "your role may only read ...\nError committing write\nAccess Denied (Service: S3, Status Code: 403)"
    assert person.outcome(_fails(storage)) == "read-only"


def test_a_trino_rule_denial_is_not_read_as_a_read_only_credential() -> None:
    with pytest.raises(E2EError, match="other than an access decision"):
        person.outcome(_fails("only the people catalogs ...\nAccess Denied: Cannot access catalog lakehouse_dev"))


def test_any_other_failure_is_not_read_as_a_decision() -> None:
    with pytest.raises(E2EError, match="other than an access decision"):
        person.outcome(_fails("cannot reach Trino at trino.olf.localhost: ConnectionError"))
