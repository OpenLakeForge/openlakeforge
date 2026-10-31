"""`olf infra catalog sync-namespaces` grants olf-gold-read on Gold namespaces only (ADR 0019, section 5)."""

from __future__ import annotations

import io
import json
import re
import urllib.error
from pathlib import Path

import pytest
from openlakeforge_domain import CatalogNamespace

from olf.infra import catalog, polaris
from olf.infra.commands.catalog import reconcile_polaris

REPO_ROOT = Path(__file__).resolve().parents[4]
CATALOG = "lakehouse_dev"
ROLE = "olf-gold-read"
PRIVILEGES = {"TABLE_READ_DATA", "NAMESPACE_READ_PROPERTIES", "VIEW_READ_PROPERTIES"}
MANAGED = {catalog.MANAGED_BY_KEY: catalog.MANAGED_BY_VALUE}


class FakePolaris:
    """The calls the client makes, answered as Polaris 1.4.0 does: a repeated grant is HTTP 500, a missing role 404,
    and dropping a namespace removes the grants on it."""

    def __init__(self, *, role_exists: bool = True) -> None:
        self.namespaces: dict[str, dict[str, str]] = {}
        self.grants: set[tuple[str, str]] = set()
        self.role_exists = role_exists
        self.grant_writes: list[tuple[str, str]] = []

    def respond(self, request) -> tuple[int, dict]:  # noqa: ANN001 - urllib request
        method, path = request.get_method(), request.full_url.split("127.0.0.1:8181", 1)[1]
        body = json.loads(request.data) if request.data else {}
        if path.endswith(f"/catalog-roles/{ROLE}/grants"):
            return self._grants(method, body)
        if path == f"/api/catalog/v1/{CATALOG}/namespaces":
            if method == "POST":
                self.namespaces[body["namespace"][0]] = body["properties"]
                return 200, {}
            return 200, {"namespaces": [[name] for name in self.namespaces]}
        if match := re.fullmatch(rf"/api/catalog/v1/{CATALOG}/namespaces/([^/]+)(/tables|/properties)?(\?.*)?", path):
            name, suffix = match.group(1), match.group(2)
            if suffix == "/tables":
                return 200, {"identifiers": []}
            if suffix == "/properties":
                self.namespaces[name].update(body["updates"])
                return 200, {}
            if method == "DELETE":
                del self.namespaces[name]
                self.grants = {grant for grant in self.grants if grant[0] != name}
                return 204, {}
            return 200, {"properties": self.namespaces[name]}
        return 400, {"error": f"unexpected {method} {path}"}

    def _grants(self, method: str, body: dict) -> tuple[int, dict]:
        if not self.role_exists:
            return 404, {}
        if method == "GET":
            return 200, {
                "grants": [
                    {"type": "namespace", "namespace": [namespace], "privilege": privilege}
                    for namespace, privilege in sorted(self.grants)
                ]
            }
        grant = body["grant"]
        assert grant["type"] == "namespace"
        key = (grant["namespace"][0], grant["privilege"])
        if key in self.grants:
            return 500, {"error": "grant_records_pkey"}
        self.grants.add(key)
        self.grant_writes.append(key)
        return 201, {}


class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status, self._body = status, json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def connect(monkeypatch: pytest.MonkeyPatch, fake: FakePolaris) -> polaris.PolarisClient:
    def urlopen(request, timeout=0):  # noqa: ANN001, ARG001 - urllib signature
        status, body = fake.respond(request)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "", {}, io.BytesIO(json.dumps(body).encode()))
        return FakeResponse(status, body)

    monkeypatch.setattr(polaris.urllib.request, "urlopen", urlopen)
    config = polaris.PolarisConfig("http://127.0.0.1:8181", CATALOG, "id", "secret")
    return polaris.PolarisClient(config, token="t")


def sync(client: polaris.PolarisClient, *, gold: tuple[str, ...], prune: bool = False, extra: tuple[str, ...] = ()):
    desired = tuple(CatalogNamespace(name, f"s3://bucket/{name}/") for name in (*gold, *extra))
    reconcile_polaris(client, desired=desired, gold_namespaces=gold, dry_run=False, prune=prune)


def test_a_new_gold_namespace_gets_the_three_read_privileges(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePolaris()

    sync(connect(monkeypatch, fake), gold=("sales_gold",))

    assert fake.grants == {("sales_gold", privilege) for privilege in PRIVILEGES}


def test_a_rerun_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePolaris()
    client = connect(monkeypatch, fake)
    sync(client, gold=("sales_gold",))
    fake.grant_writes.clear()

    sync(client, gold=("sales_gold",))

    assert fake.grant_writes == []


def test_only_the_missing_privilege_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePolaris()
    fake.grants = {("sales_gold", "TABLE_READ_DATA")}

    sync(connect(monkeypatch, fake), gold=("sales_gold",))

    assert set(fake.grant_writes) == {
        ("sales_gold", "NAMESPACE_READ_PROPERTIES"),
        ("sales_gold", "VIEW_READ_PROPERTIES"),
    }


def test_bronze_and_silver_namespaces_are_never_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePolaris()

    sync(connect(monkeypatch, fake), gold=("sales_gold",), extra=("crm_bronze", "sales_silver"))

    assert {namespace for namespace, _ in fake.grants} == {"sales_gold"}
    assert set(fake.namespaces) == {"sales_gold", "crm_bronze", "sales_silver"}


def test_a_stack_without_the_role_syncs_namespaces_and_skips_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePolaris(role_exists=False)

    sync(connect(monkeypatch, fake), gold=("sales_gold",))

    assert set(fake.namespaces) == {"sales_gold"}
    assert fake.grant_writes == []


def test_a_dry_run_reports_the_grants_without_writing_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePolaris()

    reconcile_polaris(
        connect(monkeypatch, fake),
        desired=(CatalogNamespace("sales_gold", "s3://gold/sales_gold/"),),
        gold_namespaces=("sales_gold",),
        dry_run=True,
        prune=False,
    )

    assert fake.grant_writes == [] and fake.namespaces == {}
    assert "grant TABLE_READ_DATA on sales_gold" in capsys.readouterr().out


def test_a_pruned_gold_namespace_leaves_no_grants_and_a_rerun_regrants_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePolaris()
    client = connect(monkeypatch, fake)
    sync(client, gold=("sales_gold", "retired_gold"))
    fake.grant_writes.clear()

    sync(client, gold=("sales_gold",), prune=True)

    assert set(fake.namespaces) == {"sales_gold"}
    assert {namespace for namespace, _ in fake.grants} == {"sales_gold"}
    assert fake.grant_writes == []


def test_a_server_error_other_than_a_missing_role_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = connect(monkeypatch, FakePolaris())
    monkeypatch.setattr(FakePolaris, "_grants", lambda self, method, body: (500, {"error": "boom"}))

    with pytest.raises(polaris.PolarisError):
        client.namespace_grants(ROLE)


def test_gold_namespaces_come_from_the_descriptors_and_follow_the_prefix() -> None:
    names = catalog.desired_gold_namespaces(REPO_ROOT)
    prefixed = catalog.desired_gold_namespaces(REPO_ROOT, namespace_prefix="p_")

    assert "order_revenue_gold" in names
    assert all(name.endswith("_gold") for name in names)
    assert prefixed == tuple(sorted(f"p_{name}" for name in names))
