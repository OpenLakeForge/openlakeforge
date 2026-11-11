from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from olf.infra.e2e import _durability as durability
from olf.infra.e2e._shell import E2EError

SECRET = "s3cr3t-value-0123456789abcdef"


def _sources(**overrides: Any) -> durability.Sources:
    state: dict[str, Any] = {
        "tables": lambda: {
            "gold.orders": {"rows": 100, "snapshot_id": "111"},
            "silver.orders": {"rows": 120, "snapshot_id": "222"},
        },
        "dagster_runs": lambda: ["run-a", "run-b"],
        "dashboards": lambda: ["uuid-1"],
        "entities": lambda: ["sales", "svc.db.gold.orders"],
        "search_hits": lambda: 7,
        "activation": lambda: {"activations/dev/ACTIVE.json": "sha256:abc"},
        "objects": lambda: {"gold-bucket/a.parquet": 10, "ops/ACTIVE.json": 5},
    }
    return durability.Sources(**{**state, **overrides})


def test_an_unchanged_stage_verifies_clean_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    durability.write_baseline(durability.capture(_sources(), stage="dev"), path, {})

    assert durability.verify(durability.load_baseline(path), _sources()) == []


def test_new_state_is_not_a_difference() -> None:
    baseline = durability.capture(_sources(), stage="dev")

    later = _sources(dagster_runs=lambda: ["run-a", "run-b", "run-c"], objects=lambda: {**_sources().objects(), "x": 1})

    assert durability.verify(baseline, later) == []


def test_a_removed_run_and_a_changed_snapshot_are_both_reported() -> None:
    baseline = durability.capture(_sources(), stage="dev")
    after = _sources(
        dagster_runs=lambda: ["run-a"],
        tables=lambda: {
            "gold.orders": {"rows": 100, "snapshot_id": "999"},
            "silver.orders": {"rows": 120, "snapshot_id": "222"},
        },
    )

    assert durability.verify(baseline, after) == [
        "table gold.orders changed its current snapshot: 111 before, 999 now",
        "Dagster run run-b is missing",
    ]


def test_every_kind_of_loss_is_reported() -> None:
    baseline = durability.capture(_sources(), stage="dev")
    after = _sources(
        tables=lambda: {"silver.orders": {"rows": 5, "snapshot_id": "222"}},
        dashboards=lambda: [],
        entities=lambda: ["sales"],
        search_hits=lambda: 0,
        activation=lambda: {},
        objects=lambda: {"gold-bucket/a.parquet": 11},
    )

    assert durability.verify(baseline, after) == [
        "table gold.orders is missing",
        "table silver.orders has fewer rows: 120 before, 5 now",
        "Superset dashboard uuid-1 is missing",
        "OpenMetadata entity svc.db.gold.orders is missing",
        "OpenMetadata search finds nothing; it found 7 hits before",
        "activation pointer activations/dev/ACTIVE.json was sha256:abc, now None",
        "object gold-bucket/a.parquet changed size: 10 bytes before, 11 now",
        "object ops/ACTIVE.json is missing",
    ]


def test_a_source_that_cannot_be_read_is_a_difference_not_a_crash() -> None:
    def unreachable() -> list[str]:
        raise E2EError("Superset endpoint did not become reachable.")

    baseline = durability.capture(_sources(), stage="dev")

    assert durability.verify(baseline, _sources(dashboards=unreachable)) == [
        "the stage could not be read: Superset endpoint did not become reachable."
    ]


def test_a_capture_that_exposes_a_secret_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    leaking = _sources(entities=lambda: [f"svc.{SECRET}"])

    with pytest.raises(E2EError) as raised:
        durability.write_baseline(durability.capture(leaking, stage="dev"), path, {"ns/secret/key": SECRET})

    assert "the durability baseline contains the value of Secret ns/secret/key" in str(raised.value)
    assert SECRET not in str(raised.value)
    assert not path.exists()


def test_an_unreadable_or_foreign_file_is_not_a_baseline(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text('{"version": 99}')

    with pytest.raises(E2EError, match="cannot read the durability baseline"):
        durability.load_baseline(tmp_path / "missing.json")
    with pytest.raises(E2EError, match="not a version 1 durability baseline"):
        durability.load_baseline(other)


def test_a_slim_stage_never_contacts_superset_or_openmetadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: Any) -> Any:
        raise AssertionError("a service the stage does not run was contacted")

    monkeypatch.setattr(durability, "configured_layers", lambda _cfg: {"analytics": False, "governance": False})
    for reader in ("_dashboards", "_entities", "_search_hits"):
        monkeypatch.setattr(durability, reader, forbidden)
    monkeypatch.setattr(durability, "_tables", lambda _cfg: {})
    monkeypatch.setattr(durability, "_dagster_runs", lambda _cfg: ["run-a"])
    monkeypatch.setattr(durability, "_activation", lambda _cfg, _stage: {})
    monkeypatch.setattr(durability, "_objects", lambda _cfg, _stage: {})
    cfg: Any = SimpleNamespace(environ={})

    baseline = durability.capture(durability.cluster_sources(cfg), stage="dev")

    assert baseline["superset"] == {"dashboards": []}
    assert baseline["openmetadata"] == {"entities": [], "search_hits": 0}
    assert durability.verify(baseline, durability.cluster_sources(cfg)) == []


def test_the_dashboard_list_is_followed_to_its_last_page() -> None:
    pages = {0: [f"{n:032x}" for n in range(100)], 1: [f"{n:032x}" for n in range(100, 150)]}
    asked: list[int] = []

    def get_page(query: dict[str, Any]) -> dict[str, Any]:
        asked.append(query["page"])
        return {"count": 150, "result": [{"uuid": uuid.upper()} for uuid in pages[query["page"]]]}

    assert durability.dashboard_uuids(get_page) == pages[0] + pages[1]
    assert asked == [0, 1]


def test_the_current_snapshot_is_the_main_ref_not_the_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    sql: list[str] = []
    monkeypatch.setattr(durability, "trino_query", lambda _cfg, query: sql.append(query) or "42\n")

    assert durability._current_snapshot(cast(Any, None), 'c.s."t$refs"') == "42"
    assert sql == ["SELECT snapshot_id FROM c.s.\"t$refs\" WHERE name = 'main'"]

    monkeypatch.setattr(durability, "trino_query", lambda _cfg, _query: "")
    assert durability._current_snapshot(cast(Any, None), 'c.s."t$refs"') == ""


class _FakeOpenMetadata:
    def list_domains(self) -> list[tuple[str, str]]:
        return [("sales_orders", "")]

    def list_data_products(self) -> list[Any]:
        return [SimpleNamespace(name="sales_orders")]

    def request(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"data": [{"fullyQualifiedName": "svc.db.gold.orders"}]}


def test_a_lost_data_product_is_not_hidden_by_a_domain_of_the_same_name(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def fake_client(_cfg: Any) -> Iterator[tuple[Any, None]]:
        yield _FakeOpenMetadata(), None

    monkeypatch.setattr(durability, "openmetadata_client", fake_client)
    monkeypatch.setattr(durability, "_login", lambda *_a: None)
    cfg: Any = SimpleNamespace(environ={"OPENLAKEFORGE_CATALOG_DATABASE_FQN": "svc.db"})

    entities = durability._entities(cfg)

    assert entities == ["domain:sales_orders", "dataProduct:sales_orders", "table:svc.db.gold.orders"]
    baseline = durability.capture(_sources(entities=lambda: entities), stage="dev")
    lost_product = _sources(entities=lambda: [e for e in entities if e != "dataProduct:sales_orders"])
    assert durability.verify(baseline, lost_product) == ["OpenMetadata entity dataProduct:sales_orders is missing"]



def _lake_with_only_the_default_product(monkeypatch: pytest.MonkeyPatch, *, gold_rows: str) -> None:
    """Trino answers as after the smoke suite: the default product's Silver and Gold hold tables, nothing else does."""
    from conftest import E2E_INVENTORY

    from olf.infra.e2e import _trino

    product = E2E_INVENTORY.default_product
    domain = E2E_INVENTORY.domain_for_product(product)
    tables = {
        domain.silver_namespace: [entity.name for entity in E2E_INVENTORY.floe_entities(domain)][:1],
        product.gold_namespace: ["mart"],
    }
    monkeypatch.setattr(_trino, "check_trino_catalog", lambda _cfg: None)
    monkeypatch.setattr(_trino, "trino_query", lambda _cfg, sql: "\n".join(tables.get(sql.rsplit(".", 1)[-1], [])))
    monkeypatch.setattr(_trino, "trino_scalar", lambda _cfg, _sql: gold_rows)
    monkeypatch.setattr(durability, "cluster_sources", lambda _cfg: _sources())
    monkeypatch.setattr(durability, "secret_values", lambda _cfg: {})


def test_a_lake_loaded_by_the_smoke_suite_can_be_captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from conftest import E2E_INVENTORY, e2e_cfg

    # The upgrade drill loads data with the smoke suite: other domains and products stay empty.
    assert len(E2E_INVENTORY.domains) > 1
    _lake_with_only_the_default_product(monkeypatch, gold_rows="5")

    durability.capture_to_file(e2e_cfg(tmp_path, suite="smoke"), tmp_path / "baseline.json")

    assert (tmp_path / "baseline.json").exists()


def test_a_lake_whose_default_product_never_reached_gold_is_not_captured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from conftest import e2e_cfg

    _lake_with_only_the_default_product(monkeypatch, gold_rows="0")

    with pytest.raises(E2EError, match="to contain rows"):
        durability.capture_to_file(e2e_cfg(tmp_path, suite="smoke"), tmp_path / "baseline.json")
    assert not (tmp_path / "baseline.json").exists()
