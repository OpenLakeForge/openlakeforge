import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "packages/runtime"))
PROJECTS = sorted(REPO_ROOT.glob("lakehouse_code/gold/*/dbt"))
# No default: an unset stage catalog fails the run instead of reaching DEV.
STAGE_CATALOG = "\"{{ env_var('OPENLAKEFORGE_CATALOG_NAME') }}\""


def test_every_product_profile_takes_its_stage_from_the_environment() -> None:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    assert len(PROJECTS) == 3
    for project in PROJECTS:
        profile = render_profile(project)
        assert profile.startswith(f"{project.parent.name}:\n  target: runtime\n")
        assert "type: trino" in profile
        # No default: an unset stage binding fails the run rather than
        # reaching a DEV catalog or host from another stage.
        assert "database: \"{{ env_var('OPENLAKEFORGE_CATALOG_NAME') }}\"" in profile
        assert "host: \"{{ env_var('OPENLAKEFORGE_QUERY_TRINO_HOST') }}\"" in profile
        assert "user: \"{{ env_var('OPENLAKEFORGE_DBT_TRINO_USER') }}\"" in profile
        assert (
            f"schema: \"{{{{ env_var('OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX', '') }}}}{project.parent.name}_gold\""
            in profile
        )
        assert "lakehouse_dev" not in profile
        assert "olf-system" not in profile


def test_every_gold_session_may_plan_a_75_table_union() -> None:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    output = yaml.safe_load(render_profile(PROJECTS[0]))[PROJECTS[0].parent.name]["outputs"]["runtime"]

    # About two stages per table read; Trino's default of 150 refused 75 tables at 152 stages (#706).
    assert output["session_properties"]["query_max_stage_count"] >= 2 * 75 + 2


def test_profile_reaches_trino_over_https_only_when_the_pod_is_given_the_ca() -> None:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    profile = render_profile(PROJECTS[0])

    assert "env_var('OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME', 'http')" in profile
    assert "env_var('OPENLAKEFORGE_QUERY_TRINO_CA', '') or none" in profile
    assert "env_var('OPENLAKEFORGE_DBT_TRINO_PASSWORD', '') or none" in profile


def test_an_unknown_adapter_is_refused() -> None:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    with pytest.raises(ValueError, match="Unsupported dbt adapter 'duckdb'"):
        render_profile(PROJECTS[0], adapter="duckdb")


def test_one_profiles_file_serves_every_product(tmp_path: Path) -> None:
    from openlakeforge_runtime.dbt.render_profiles import write_profiles

    profiles = yaml.safe_load(write_profiles(PROJECTS, tmp_path).read_text(encoding="utf-8"))

    assert sorted(profiles) == sorted(project.parent.name for project in PROJECTS)
    assert all(set(profile["outputs"]) == {"runtime"} for profile in profiles.values())


def test_the_baked_manifest_is_parsed_with_placeholders_not_the_callers_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openlakeforge_runtime.dbt import render_profiles

    project = tmp_path / "gold/demo/dbt"
    (project / "target").mkdir()
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

    project = tmp_path / "gold/demo/dbt"
    (project / "target").mkdir()
    (project / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    (project / "target/manifest.json").write_text("{}")
    monkeypatch.setattr(render_profiles.subprocess, "run", lambda *args, **kwargs: pytest.fail("re-parsed"))

    manifest = render_profiles.ensure_baked_manifest(project, tmp_path / "profiles")

    assert manifest == project / "target/manifest.json"
    # A checkout's manifest can come from `olf check dbt`, which leaves no
    # profile behind; the run still needs one.
    assert "demo" in yaml.safe_load((tmp_path / "profiles/profiles.yml").read_text())


def test_a_check_points_the_runtime_at_its_own_profiles_and_the_bake_at_the_distributions_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openlakeforge_runtime.dbt import render_profiles

    project = tmp_path / "gold/food_quality/dbt"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: food_quality\nprofile: food_quality\n")
    monkeypatch.setenv(render_profiles.PROFILES_DIR_ENV, str(tmp_path / "check-profiles"))
    monkeypatch.delenv(render_profiles.PACKAGES_DIR_ENV, raising=False)
    parses: list[dict[str, str]] = []
    monkeypatch.setattr(render_profiles.subprocess, "run", lambda argv, check, env: parses.append(env))

    render_profiles.ensure_baked_manifest(project)

    assert "food_quality" in yaml.safe_load((tmp_path / "check-profiles/profiles.yml").read_text())
    package_dir = REPO_ROOT / "packages/runtime/openlakeforge_runtime/dbt"
    assert {env[render_profiles.PACKAGES_DIR_ENV] for env in parses} == {str(package_dir)}
    assert (package_dir / "openlakeforge_dbt/dbt_project.yml").is_file()


def test_products_sharing_a_profile_name_are_refused(tmp_path: Path) -> None:
    from openlakeforge_runtime.dbt.render_profiles import write_profiles

    projects = []
    for product in ("orders", "returns"):
        project = tmp_path / f"gold/{product}/dbt"
        project.mkdir(parents=True)
        (project / "dbt_project.yml").write_text(f"name: {product}\nprofile: shared\n")
        projects.append(project)

    with pytest.raises(ValueError, match="distinct profile names; shared: shared"):
        write_profiles(projects, tmp_path / "profiles")
    assert not (tmp_path / "profiles/profiles.yml").exists()


def test_all_models_request_atomic_trino_replacement() -> None:
    for project in PROJECTS:
        project_text = (project / "dbt_project.yml").read_text(encoding="utf-8")
        assert "+materialized: table" in project_text
        assert "+on_table_exists: replace" in project_text
        assert f"+database: {STAGE_CATALOG}" in project_text


def test_all_silver_sources_use_the_lineage_catalog() -> None:
    for project in PROJECTS:
        source_text = (project / "models/sources.yml").read_text(encoding="utf-8")
        assert f"database: {STAGE_CATALOG}" in source_text


def test_all_gold_models_use_explicit_join_keys_for_trino() -> None:
    gold_models = [model for project in PROJECTS for model in (project / "models/gold").glob("*.sql")]
    assert gold_models
    for model in gold_models:
        sql = model.read_text(encoding="utf-8")
        assert not re.search(r"\busing\s*\(", sql, flags=re.IGNORECASE), model


def _manifest_project(tmp_path: Path, view_security: str | None) -> Path:
    import json
    import shutil

    shutil.copytree(REPO_ROOT / "lakehouse_code", tmp_path / "lakehouse_code", ignore=shutil.ignore_patterns("target"))
    project = tmp_path / "lakehouse_code/gold/order_revenue/dbt"
    (project / "target").mkdir()
    config = {} if view_security is None else {"view_security": view_security}
    schema = "order_revenue_gold"
    node = {"resource_type": "model", "name": "mart", "database": "cat", "schema": schema, "config": config}
    (project / "target/manifest.json").write_text(json.dumps({"nodes": {"m": node}, "sources": {}}), encoding="utf-8")
    return project


@pytest.mark.parametrize(("view_security", "passes"), [("invoker", True), ("definer", False), (None, False)])
def test_check_dbt_requires_invoker_views_on_gold_models(
    tmp_path: Path, view_security: str | None, passes: bool
) -> None:
    import typer

    from olf.common.commands.checks import _dbt

    project = _manifest_project(tmp_path, view_security)
    if passes:
        _dbt._validate_dbt_relation_contract(project, root=tmp_path, catalog="cat")
    else:
        with pytest.raises(typer.Exit):
            _dbt._validate_dbt_relation_contract(project, root=tmp_path, catalog="cat")


def test_every_product_profile_forwards_the_polaris_token_and_builds_invoker_views() -> None:
    from openlakeforge_runtime.dbt.render_profiles import render_profile

    output = yaml.safe_load(render_profile(PROJECTS[0]))[PROJECTS[0].parent.name]["outputs"]["runtime"]

    header = "token={{ env_var('OPENLAKEFORGE_POLARIS_TOKEN', '') }}"
    assert output["http_headers"] == {"X-Trino-Extra-Credential": header}
    for project in PROJECTS:
        config = yaml.safe_load((project / "dbt_project.yml").read_text(encoding="utf-8"))
        assert config["models"][project.parent.name]["+view_security"] == "invoker"
