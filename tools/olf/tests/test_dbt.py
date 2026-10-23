import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
PROJECTS = sorted(REPO_ROOT.glob("lakehouse_code/gold/*/dbt"))
LINEAGE_DATABASE_ENV = "OPENLAKEFORGE_CATALOG_NAME"
LINEAGE_DATABASE_DEFAULT = "lakehouse_dev"


def test_all_product_profiles_use_trino_and_catalog_contract() -> None:
    from libs.dbt.render_profiles import render_profile

    assert len(PROJECTS) == 3
    for project in PROJECTS:
        profile = render_profile(project, environment="local")
        assert "type: trino" in profile
        assert "env_var('OPENLAKEFORGE_DBT_TRINO_METHOD', 'none')" in profile
        assert (
            f"database: \"{{{{ env_var('{LINEAGE_DATABASE_ENV}', '{LINEAGE_DATABASE_DEFAULT}') }}}}\""
            in profile
        )
        assert "OPENLAKEFORGE_DBT_TRINO_USER" in profile
        assert "duckdb" not in profile.lower()


def test_all_models_request_atomic_trino_replacement() -> None:
    for project in PROJECTS:
        project_text = (project / "dbt_project.yml").read_text(encoding="utf-8")
        assert "+materialized: table" in project_text
        assert "+on_table_exists: replace" in project_text
        assert (
            f"+database: \"{{{{ env_var('{LINEAGE_DATABASE_ENV}', '{LINEAGE_DATABASE_DEFAULT}') }}}}\""
            in project_text
        )


def test_all_silver_sources_use_the_lineage_catalog() -> None:
    for project in PROJECTS:
        source_text = (project / "models/sources.yml").read_text(encoding="utf-8")
        assert (
            f"database: \"{{{{ env_var('{LINEAGE_DATABASE_ENV}', '{LINEAGE_DATABASE_DEFAULT}') }}}}\""
            in source_text
        )


def test_all_gold_models_use_explicit_join_keys_for_trino() -> None:
    gold_models = [model for project in PROJECTS for model in (project / "models/gold").glob("*.sql")]
    assert gold_models
    for model in gold_models:
        sql = model.read_text(encoding="utf-8")
        assert not re.search(r"\busing\s*\(", sql, flags=re.IGNORECASE), model


def test_local_profile_reaches_trino_over_https_only_when_the_run_pod_is_given_the_ca() -> None:
    profile = (REPO_ROOT / "libs/dbt/profiles/local.yml").read_text(encoding="utf-8")

    assert profile.count("OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME', 'http'") == 2
    assert profile.count("env_var('OPENLAKEFORGE_QUERY_TRINO_CA', '') or none") == 2
    assert profile.count("env_var('OPENLAKEFORGE_DBT_TRINO_PASSWORD', '') or none") == 2
