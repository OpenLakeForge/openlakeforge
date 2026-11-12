import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import typer
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "packages/runtime"))
GOLD = REPO_ROOT / "lakehouse_code/gold"
# No default: an unset stage catalog fails the run instead of reaching DEV.
STAGE_CATALOG = "\"{{ env_var('OPENLAKEFORGE_CATALOG_NAME') }}\""


def _runtime_output() -> dict:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    return yaml.safe_load(render_profile(GOLD))["gold"]["outputs"]["runtime"]


def test_the_gold_profile_takes_its_stage_from_the_environment() -> None:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    profile = render_profile(GOLD)
    assert profile.startswith("gold:\n  target: runtime\n")
    assert "type: trino" in profile
    # No default: an unset stage binding fails the run rather than
    # reaching a DEV catalog or host from another stage.
    assert "database: \"{{ env_var('OPENLAKEFORGE_CATALOG_NAME') }}\"" in profile
    assert "host: \"{{ env_var('OPENLAKEFORGE_QUERY_TRINO_HOST') }}\"" in profile
    assert "user: \"{{ env_var('OPENLAKEFORGE_DBT_TRINO_USER') }}\"" in profile
    assert "lakehouse_dev" not in profile
    assert "olf-system" not in profile


def test_every_gold_session_may_plan_a_75_table_union() -> None:
    # About two stages per table read; Trino's default of 150 refused 75 tables at 152 stages (#706).
    assert _runtime_output()["session_properties"]["query_max_stage_count"] >= 2 * 75 + 2


def test_profile_reaches_trino_over_plain_http_with_no_password() -> None:
    output = _runtime_output()

    assert output["method"] == "none"
    assert output["http_scheme"] == "http"
    assert not {"password", "cert"} & set(output)


def test_an_unknown_adapter_is_refused() -> None:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    with pytest.raises(ValueError, match="Unsupported dbt adapter 'duckdb'"):
        render_profile(GOLD, adapter="duckdb")


def test_the_reference_project_is_one_gold_project_with_a_folder_per_product() -> None:
    from openlakeforge_runtime.dbt.render_profiles import gold_project_dir, product_names

    assert gold_project_dir(GOLD) == GOLD
    assert product_names(GOLD) == ["customer_health", "inventory_reliability", "order_revenue"]
    assert not list(GOLD.glob("*/dbt/dbt_project.yml"))


def test_a_gold_root_without_a_project_has_none(tmp_path: Path) -> None:
    from openlakeforge_runtime.dbt.render_profiles import gold_project_dir, product_names

    assert gold_project_dir(tmp_path) is None
    assert product_names(tmp_path) == []


def test_every_product_is_a_dbt_group_whose_models_are_private_by_default() -> None:
    config = yaml.safe_load((GOLD / "dbt_project.yml").read_text(encoding="utf-8"))["models"]["gold"]

    assert config["+access"] == "private"
    for product in ("customer_health", "inventory_reliability", "order_revenue"):
        assert config[product]["+group"] == product
        groups = yaml.safe_load((GOLD / "models" / product / "schema.yml").read_text(encoding="utf-8"))["groups"]
        assert [group["name"] for group in groups] == [product]


def test_the_baked_manifest_is_parsed_with_placeholders_not_the_callers_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openlakeforge_runtime.dbt import render_profiles

    project = tmp_path / "gold"
    (project / "target").mkdir(parents=True)
    (project / "target/partial_parse.msgpack").write_bytes(b"stale")
    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_NAME", "lakehouse_prod")
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_HOST", "trino.prod")
    parses: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(render_profiles.subprocess, "run", lambda argv, check, env: parses.append((argv, env)))

    render_profiles.bake_manifest(project, tmp_path / "profiles")

    argv, env = parses[-1]
    assert argv[1] == "parse" and "--no-partial-parse" in argv
    assert env["OPENLAKEFORGE_CATALOG_NAME"] == "stage_catalog"
    assert env["OPENLAKEFORGE_QUERY_TRINO_HOST"] == "stage-query-host"
    # Left in place, dagster-dbt would seed every run's partial parse with it.
    assert not (project / "target/partial_parse.msgpack").exists()


def test_a_manifest_already_there_is_used_as_is_and_still_gets_a_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openlakeforge_runtime.dbt import render_profiles

    project = tmp_path / "gold"
    (project / "target").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: gold\nprofile: gold\n")
    (project / "target/manifest.json").write_text("{}")
    monkeypatch.setattr(render_profiles.subprocess, "run", lambda *args, **kwargs: pytest.fail("re-parsed"))

    manifest = render_profiles.ensure_baked_manifest(project, tmp_path / "profiles")

    assert manifest == project / "target/manifest.json"
    # A checkout's manifest can come from `olf check dbt`, which leaves no
    # profile behind; the run still needs one.
    assert "gold" in yaml.safe_load((tmp_path / "profiles/profiles.yml").read_text())


def test_a_check_points_the_runtime_at_its_own_profiles_and_the_bake_at_the_distributions_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openlakeforge_runtime.dbt import render_profiles

    project = tmp_path / "gold"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: gold\nprofile: gold\n")
    monkeypatch.setenv(render_profiles.PROFILES_DIR_ENV, str(tmp_path / "check-profiles"))
    monkeypatch.delenv(render_profiles.PACKAGES_DIR_ENV, raising=False)
    parses: list[dict[str, str]] = []
    monkeypatch.setattr(render_profiles.subprocess, "run", lambda argv, check, env: parses.append(env))

    render_profiles.ensure_baked_manifest(project)

    assert "gold" in yaml.safe_load((tmp_path / "check-profiles/profiles.yml").read_text())
    package_dir = REPO_ROOT / "packages/runtime/openlakeforge_runtime/dbt"
    assert {env[render_profiles.PACKAGES_DIR_ENV] for env in parses} == {str(package_dir)}
    assert (package_dir / "openlakeforge_dbt/dbt_project.yml").is_file()


def test_all_models_request_atomic_trino_replacement() -> None:
    project_text = (GOLD / "dbt_project.yml").read_text(encoding="utf-8")
    assert "+materialized: table" in project_text
    assert "+on_table_exists: replace" in project_text
    assert f"+database: {STAGE_CATALOG}" in project_text


def test_every_domain_declares_its_silver_once_in_the_lineage_catalog() -> None:
    files = sorted((GOLD / "models/_sources").glob("*.yml"))
    assert [path.stem for path in files] == ["sales", "supply_chain"]
    for path in files:
        source_text = path.read_text(encoding="utf-8")
        assert f"database: {STAGE_CATALOG}" in source_text
        sources = yaml.safe_load(source_text)["sources"]
        assert [source["name"] for source in sources] == [path.stem]
        assert sources[0]["schema"].endswith(f"{path.stem}_silver")


def test_all_gold_models_use_explicit_join_keys_for_trino() -> None:
    gold_models = sorted((GOLD / "models").rglob("*.sql"))
    assert gold_models
    for model in gold_models:
        sql = model.read_text(encoding="utf-8")
        assert not re.search(r"\busing\s*\(", sql, flags=re.IGNORECASE), model


def _model(name: str, product: str | None, *, schema: str, view_security: str | None = "invoker", reads=()) -> dict:
    return {
        "resource_type": "model",
        "package_name": "gold",
        "name": name,
        "fqn": ["gold", name] if product is None else ["gold", product, name],
        "database": "cat",
        "schema": schema,
        "config": {} if view_security is None else {"view_security": view_security},
        "depends_on": {"nodes": [f"source.gold.{domain}.{table}" for domain, table in reads]},
    }


def _source(domain: str, table: str, *, schema: str | None = None) -> dict:
    return {"source_name": domain, "name": table, "database": "cat", "schema": schema or f"{domain}_silver"}


def _manifest_project(tmp_path: Path, nodes: list[dict], sources: list[dict]) -> Path:
    shutil.copytree(REPO_ROOT / "lakehouse_code", tmp_path / "lakehouse_code", ignore=shutil.ignore_patterns("target"))
    project = tmp_path / "lakehouse_code/gold"
    (project / "target").mkdir()
    manifest = {
        "metadata": {"project_name": "gold"},
        "nodes": {f"model.gold.{node['name']}": node for node in nodes},
        "sources": {f"source.gold.{source['source_name']}.{source['name']}": source for source in sources},
    }
    (project / "target/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return project


def _contract_errors(project: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> str | None:
    from olf.common.commands.checks import _dbt

    try:
        _dbt._validate_dbt_relation_contract(project, root=root, catalog="cat")
    except typer.Exit:
        return capsys.readouterr().err
    return None


@pytest.mark.parametrize(("view_security", "passes"), [("invoker", True), ("definer", False), (None, False)])
def test_check_dbt_requires_invoker_views_on_gold_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], view_security: str | None, passes: bool
) -> None:
    model = _model("mart", "order_revenue", schema="order_revenue_gold", view_security=view_security)
    project = _manifest_project(tmp_path, [model], [])

    assert (_contract_errors(project, tmp_path, capsys) is None) is passes


def test_check_dbt_accepts_each_product_in_its_own_schema_reading_its_own_domain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nodes = [
        _model("revenue", "order_revenue", schema="order_revenue_gold", reads=[("sales", "orders")]),
        _model(
            "stock", "inventory_reliability", schema="inventory_reliability_gold", reads=[("supply_chain", "shipments")]
        ),
    ]
    project = _manifest_project(tmp_path, nodes, [_source("sales", "orders"), _source("supply_chain", "shipments")])

    assert _contract_errors(project, tmp_path, capsys) is None


@pytest.mark.parametrize(
    ("nodes", "sources", "message"),
    [
        (
            [_model("leaky", "order_revenue", schema="customer_health_gold")],
            [],
            "Gold models must use cat.order_revenue_gold: leaky",
        ),
        ([_model("loose", None, schema="gold")], [], "Gold model loose is not in a governance.yml product's folder"),
        ([_model("stray", "unknown", schema="unknown_gold")], [], "Gold model stray is not in a governance.yml"),
        (
            [_model("mart", "order_revenue", schema="order_revenue_gold", reads=[("supply_chain", "shipments")])],
            [_source("supply_chain", "shipments")],
            "product order_revenue of domain sales must not read supply_chain.shipments: mart",
        ),
        ([], [_source("sales", "orders", schema="sales_gold")], "Silver sources must use cat.sales_silver: orders"),
        ([], [_source("hr", "people")], "Silver source 'hr' is not a governance.yml domain: people"),
    ],
)
def test_check_dbt_rejects_a_model_or_source_outside_its_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], nodes: list[dict], sources: list[dict], message: str
) -> None:
    project = _manifest_project(tmp_path, nodes, sources)

    errors = _contract_errors(project, tmp_path, capsys)

    assert errors is not None and message in errors


def test_the_gold_profile_forwards_the_polaris_token_and_builds_invoker_views() -> None:
    header = "token={{ env_var('OPENLAKEFORGE_POLARIS_TOKEN', '') }}"
    assert _runtime_output()["http_headers"] == {"X-Trino-Extra-Credential": header}
    config = yaml.safe_load((GOLD / "dbt_project.yml").read_text(encoding="utf-8"))
    assert config["models"]["gold"]["+view_security"] == "invoker"


def test_gold_maintenance_covers_one_schema_per_product(monkeypatch: pytest.MonkeyPatch) -> None:
    from openlakeforge_runtime import gold_maintenance

    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX", "lakehouse_dev_")

    assert gold_maintenance.gold_schemas(GOLD) == [
        "lakehouse_dev_customer_health_gold",
        "lakehouse_dev_inventory_reliability_gold",
        "lakehouse_dev_order_revenue_gold",
    ]
