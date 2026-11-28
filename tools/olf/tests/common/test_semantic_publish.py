"""The Ossie models of `lakehouse_code/semantic/` published to a fake OpenMetadata 1.13.6, and compared with it."""

from __future__ import annotations

import copy
import dataclasses
import html
from pathlib import Path
from typing import Any

import pytest
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_core import SemanticModel, inventory_for
from openlakeforge_core.semantic import parse_ossie_model
from openlakeforge_services.governance import MetadataConfig, MetadataReconciler
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError
from openlakeforge_services.progress import SILENT
from openlakeforge_services.semantic_publish import SemanticPublisher, metric_terms, metric_type

CODE_ROOT = Path(__file__).resolve().parents[4] / "lakehouse_code"
COLUMN_TERMS = {
    "sales_core": {
        key: tuple(term.fqn for term in terms)
        for key, terms in inventory_for(CODE_ROOT.parent).column_terms("sales_core").items()
    }
}
SCHEMA = "polaris.lakehouse_dev.sales_core_gold"
TABLES = {
    "fct_order_lines": ["line_id", "account_id", "channel_id", "product_id", "promotion_id", "net_amount"],
    "dim_account": ["account_id"],
    "dim_channel": ["channel_id"],
    "dim_product": ["product_id"],
    "dim_promotion": ["promotion_id"],
}


class FakeWithMetrics(FakeOpenMetadata):
    """The metric, lineage and JSON Patch `/-` routes 1.13.6 serves, on top of the shared fake."""

    def __init__(self) -> None:
        super().__init__()
        self.types["metric"] = {"id": "type-metric", "name": "metric", "customProperties": []}
        self.edges: list[tuple[str, str]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        route = path.partition("?")[0]
        if route.startswith("/api/v1/lineage/metric/"):
            self.calls.append((method, path))
            metric_id = route.rsplit("/", 1)[1]
            return {"upstreamEdges": [{"fromEntity": a, "toEntity": b} for a, b in self.edges if b == metric_id]}
        if route == "/api/v1/lineage":
            self.calls.append((method, path))
            edge = kwargs["json_body"]["edge"]
            self.edges.append((edge["fromEntity"]["id"], edge["toEntity"]["id"]))
            return {}
        body = kwargs.get("json_body")
        if method == "POST" and route == "/api/v1/metrics":
            products = [self.entities["dataProducts"][name] for name in body.get("dataProducts", [])]
            kwargs["json_body"] = {k: v for k, v in body.items() if k != "dataProducts"}
            created = super().request(method, path, **kwargs)
            created["dataProducts"] = [{"id": p["id"], "type": "dataProduct"} for p in products]
            return created
        if method == "PATCH" and any(op["path"].endswith("/-") for op in body):
            self.calls.append((method, path))
            entity = next(e for e in self.entities["metrics"].values() if e["id"] == route.rsplit("/", 1)[1])
            for op in body:
                entity.setdefault("tags", []).append(op["value"])
            return entity
        return super().request(method, path, **kwargs)


def _config() -> MetadataConfig:
    return MetadataConfig(
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
        catalog_database_fqn="polaris.lakehouse_dev",
        catalog_silver_schema_fqns={},
        catalog_gold_schema_fqns={"sales_core": SCHEMA},
        storage_service="s",
        storage_display_name="s",
        storage_endpoint="",
        storage_region="",
        storage_bronze_bucket="b",
        storage_silver_bucket="s",
        storage_gold_bucket="g",
    )


def _model() -> SemanticModel:
    model, errors = parse_ossie_model(
        CODE_ROOT / "semantic" / "sales_core" / "sales.ossie.yaml", "sales_core", CODE_ROOT
    )
    assert model is not None, errors
    return model


@pytest.fixture
def om() -> FakeWithMetrics:
    fake = FakeWithMetrics()
    fake.seed("domains", "sales", {})
    fake.seed("dataProducts", "sales_core", {"owners": [{"id": "team-1", "type": "team", "name": "sales-team"}]})
    for name, columns in TABLES.items():
        fake.seed("tables", f"{SCHEMA}.{name}", {"columns": [{"name": c} for c in columns]})
    for term in ("NetRevenue", "GrossRevenue"):
        fake.seed("glossaryTerms", f"sales.{term}", {})
    return fake


def _publisher(fake: FakeOpenMetadata, models: list[SemanticModel] | None = None) -> SemanticPublisher:
    config = _config()
    reconciler = MetadataReconciler(config, OpenMetadataClient("http://x"), revision="abc")
    reconciler.client.request = fake.request  # type: ignore[method-assign]
    return SemanticPublisher(
        reconciler,
        config,
        models if models is not None else [_model()],
        COLUMN_TERMS,
        {"sales_core": "sales"},
        progress=SILENT,
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("SUM(CASE WHEN a = 'x)' THEN b END)", "SUM"),
        ("count(distinct a.b)", "COUNT"),
        ("AVG(a.b)", "AVERAGE"),
        ("SUM(a.b) / SUM(a.c)", "OTHER"),
        ("CASE WHEN a.b > 1 THEN 1 END", "OTHER"),
        ("SUM(a.b) + 1", "OTHER"),
    ],
)
def test_metric_type_comes_from_the_outer_aggregate(expression: str, expected: str) -> None:
    assert metric_type(expression) == expected


def test_a_metric_takes_the_term_of_the_one_tagged_column_it_reads() -> None:
    model = _model()
    metrics = {m.name: m for m in model.metrics}
    terms = COLUMN_TERMS["sales_core"]

    assert metric_terms(model, metrics["net_revenue"], terms) == (("sales.NetRevenue",), "")
    assert metric_terms(model, metrics["gross_revenue"], terms) == (("sales.GrossRevenue",), "")


def test_a_metric_reading_columns_with_different_terms_or_none_gets_no_term_and_a_reason() -> None:
    model = _model()
    net = next(m for m in model.metrics if m.name == "net_revenue")
    terms = dict(COLUMN_TERMS["sales_core"])

    both = dataclasses.replace(net, references=(("fct_order_lines", "net_amount"), ("fct_order_lines", "gross_amount")))
    none = dataclasses.replace(net, references=(("fct_order_lines", "order_status"),))
    same = {**terms, ("fct_order_lines", "gross_amount"): ("sales.NetRevenue",)}

    found, why = metric_terms(model, both, terms)
    assert found == () and "different glossary terms" in why and "gross_amount" in why
    assert metric_terms(model, none, terms) == ((), "no column it reads carries a glossary term")
    assert metric_terms(model, both, same) == (("sales.NetRevenue",), "")


def test_publish_writes_both_metrics_with_their_governance_lineage_and_constraints(om: FakeWithMetrics) -> None:
    _publisher(om).publish()

    net = om.entities["metrics"]["net_revenue_dev"]
    assert net["metricType"] == "SUM"
    assert net["metricExpression"]["language"] == "SQL"
    assert "fct_order_lines.net_amount" in net["metricExpression"]["code"]
    assert [o["id"] for o in net["owners"]] == ["team-1"]
    assert net["domains"][0]["id"] == om.entities["domains"]["sales"]["id"]
    assert net["dataProducts"][0]["id"] == om.entities["dataProducts"]["sales_core"]["id"]
    assert [t["tagFQN"] for t in net["tags"]] == ["sales.NetRevenue"]
    assert net["displayName"] == "net_revenue"
    assert net["extension"]["olfManaged"] == "git:abc"
    assert net["extension"]["olfStage"] == "dev"
    assert net["extension"]["olfOssieVersion"] == "0.1.1"
    assert net["extension"]["olfSourceFile"] == "lakehouse_code/semantic/sales_core/sales.ossie.yaml"
    assert net["extension"]["olfContentHash"].startswith("sha256:")
    assert net["extension"]["olfAiSynonyms"] == "net sales, net revenue"
    assert "Fulfilled orders only" in net["extension"]["olfAiInstructions"]
    assert {p["name"] for p in om.types["metric"]["customProperties"]} >= {"olfManaged", "olfStage", "olfContentHash"}
    assert "gross_revenue_dev" in om.entities["metrics"]

    fact = om.entities["tables"][f"{SCHEMA}.fct_order_lines"]
    assert {"constraintType": "PRIMARY_KEY", "columns": ["line_id"]} in fact["tableConstraints"]
    assert {
        "constraintType": "FOREIGN_KEY",
        "columns": ["account_id"],
        "referredColumns": [f"{SCHEMA}.dim_account.account_id"],
        "relationshipType": "MANY_TO_ONE",
    } in fact["tableConstraints"]
    assert om.entities["tables"][f"{SCHEMA}.dim_account"]["tableConstraints"] == [
        {"constraintType": "PRIMARY_KEY", "columns": ["account_id"]}
    ]
    sources = {a for a, _ in om.edges}
    assert sources == {om.entities["tables"][f"{SCHEMA}.fct_order_lines"]["id"]}
    assert len(om.edges) == 2


def test_a_repeat_publish_writes_nothing_and_the_drift_report_is_empty(om: FakeWithMetrics) -> None:
    _publisher(om).publish()
    om.calls.clear()

    _publisher(om).publish()

    assert om.writes == []
    assert _publisher(om).drift() == []
    assert om.writes == []


def test_the_servers_escaped_expression_is_not_a_difference(om: FakeWithMetrics) -> None:
    _publisher(om).publish()
    metric = om.entities["metrics"]["net_revenue_dev"]
    metric["metricExpression"]["code"] = html.escape(metric["metricExpression"]["code"])
    om.calls.clear()

    _publisher(om).publish()

    assert om.writes == []


def test_a_changed_metric_is_patched_and_drift_names_the_field(om: FakeWithMetrics) -> None:
    _publisher(om).publish()
    om.entities["metrics"]["net_revenue_dev"]["metricExpression"]["code"] = "SUM(1)"
    om.entities["metrics"]["net_revenue_dev"]["metricType"] = "OTHER"

    drift = _publisher(om).drift()

    assert [(d.kind, d.fqn, d.detail) for d in drift] == [
        ("changed", "net_revenue_dev", "metricType, metricExpression")
    ]
    _publisher(om).publish()
    assert om.entities["metrics"]["net_revenue_dev"]["metricType"] == "SUM"
    assert _publisher(om).drift() == []


def test_a_metric_removed_from_git_is_stale_and_never_deleted(om: FakeWithMetrics) -> None:
    _publisher(om).publish()
    model = _model()
    kept = dataclasses.replace(model, metrics=tuple(m for m in model.metrics if m.name == "net_revenue"))
    om.calls.clear()

    _publisher(om, [kept]).publish()
    drift = _publisher(om, [kept]).drift()

    assert [(d.kind, d.fqn) for d in drift] == [("stale", "gross_revenue_dev")]
    assert "gross_revenue_dev" in om.entities["metrics"]
    assert all(method != "DELETE" for method, _ in om.calls)


def test_another_stages_metric_is_not_stale_here(om: FakeWithMetrics) -> None:
    om.seed("metrics", "net_revenue_prod", {"extension": {"olfManaged": "git:abc", "olfStage": "prod"}})
    om.seed("metrics", "hand_made", {"extension": {}})

    assert [d.fqn for d in _publisher(om).drift() if d.kind == "stale"] == []


def test_a_metric_whose_glossary_term_is_not_published_yet_is_tagged_by_a_later_deploy(om: FakeWithMetrics) -> None:
    del om.entities["glossaryTerms"]["sales.NetRevenue"]
    _publisher(om).publish()
    assert not om.entities["metrics"]["net_revenue_dev"].get("tags")

    om.seed("glossaryTerms", "sales.NetRevenue", {})
    _publisher(om).publish()

    assert [t["tagFQN"] for t in om.entities["metrics"]["net_revenue_dev"]["tags"]] == ["sales.NetRevenue"]


def test_constraints_wait_for_the_crawled_columns_and_keep_a_persons_sort_key(om: FakeWithMetrics) -> None:
    fact = om.entities["tables"][f"{SCHEMA}.fct_order_lines"]
    fact["columns"] = []
    sort_key = {"constraintType": "SORT_KEY", "columns": ["net_amount"]}
    _publisher(om).publish()
    assert "tableConstraints" not in fact

    fact["columns"] = [{"name": c} for c in TABLES["fct_order_lines"]]
    fact["tableConstraints"] = [sort_key, {"constraintType": "PRIMARY_KEY", "columns": ["stale_col"]}]
    _publisher(om).publish()

    held = fact["tableConstraints"]
    assert sort_key in held
    assert {"constraintType": "PRIMARY_KEY", "columns": ["line_id"]} in held
    assert {"constraintType": "PRIMARY_KEY", "columns": ["stale_col"]} not in held


def test_a_metric_without_its_product_in_openmetadata_fails_before_any_write(om: FakeWithMetrics) -> None:
    del om.entities["dataProducts"]["sales_core"]

    with pytest.raises(OpenMetadataError, match="data product of product 'sales_core' not found"):
        _publisher(om).publish()

    assert om.writes == []


def test_a_table_not_crawled_yet_gets_its_lineage_on_a_later_deploy(om: FakeWithMetrics) -> None:
    held = om.entities["tables"].pop(f"{SCHEMA}.fct_order_lines")
    _publisher(om).publish()
    assert om.edges == []

    om.entities["tables"][f"{SCHEMA}.fct_order_lines"] = copy.deepcopy(held)
    _publisher(om).publish()

    assert len(om.edges) == 2


def test_the_e2e_check_finds_the_published_state_complete_and_names_what_is_missing(om: FakeWithMetrics) -> None:
    from olf.infra.e2e._semantic import semantic_problems

    _publisher(om).publish()
    client = OpenMetadataClient("http://x")
    client.request = om.request  # type: ignore[method-assign]
    gold = {"sales_core": SCHEMA}

    assert semantic_problems(client, [_model()], stage="dev", gold_schema_fqns=gold) == []

    metric = om.entities["metrics"]["gross_revenue_dev"]
    metric["tags"] = []
    del metric["extension"]["olfContentHash"]
    om.edges.clear()
    om.entities["tables"][f"{SCHEMA}.fct_order_lines"]["tableConstraints"] = []

    problems = semantic_problems(client, [_model()], stage="dev", gold_schema_fqns=gold)

    assert any("gross_revenue_dev: no glossary tag" in p for p in problems)
    assert any("gross_revenue_dev: no extension olfContentHash" in p for p in problems)
    assert any("net_revenue_dev: no lineage edge from fct_order_lines" in p for p in problems)
    assert any("fct_order_lines: no PRIMARY_KEY" in p for p in problems)
