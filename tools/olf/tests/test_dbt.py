import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
PROJECTS = sorted(REPO_ROOT.glob("lakehouse_code/gold/*/dbt"))
LINEAGE_DATABASE_ENV = "OPENLAKEFORGE_CATALOG_NAME"
LINEAGE_DATABASE_DEFAULT = "lakehouse_dev"


def test_every_product_profile_takes_its_stage_from_the_environment() -> None:
    from libs.dbt.render_profiles import render_profile

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


def test_profile_reaches_trino_over_https_only_when_the_pod_is_given_the_ca() -> None:
    from libs.dbt.render_profiles import render_profile

    profile = render_profile(PROJECTS[0])

    assert "env_var('OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME', 'http')" in profile
    assert "env_var('OPENLAKEFORGE_QUERY_TRINO_CA', '') or none" in profile
    assert "env_var('OPENLAKEFORGE_DBT_TRINO_PASSWORD', '') or none" in profile


def test_an_unknown_adapter_is_refused() -> None:
    from libs.dbt.render_profiles import render_profile

    with pytest.raises(ValueError, match="Unsupported dbt adapter 'duckdb'"):
        render_profile(PROJECTS[0], adapter="duckdb")


def test_one_profiles_file_serves_every_product(tmp_path: Path) -> None:
    from libs.dbt.render_profiles import write_profiles

    profiles = yaml.safe_load(write_profiles(PROJECTS, tmp_path).read_text(encoding="utf-8"))

    assert sorted(profiles) == sorted(project.parent.name for project in PROJECTS)
    assert all(set(profile["outputs"]) == {"runtime"} for profile in profiles.values())


def test_the_baked_manifest_is_parsed_with_placeholders_not_the_callers_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from libs.dbt import render_profiles

    project = tmp_path / "gold/demo/dbt"
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


def test_a_baked_manifest_is_used_as_is(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from libs.dbt import render_profiles

    project = tmp_path / "gold/demo/dbt"
    (project / "target").mkdir(parents=True)
    (project / "target/manifest.json").write_text("{}")
    monkeypatch.setattr(render_profiles.subprocess, "run", lambda *args, **kwargs: pytest.fail("re-parsed"))

    assert render_profiles.ensure_baked_manifest(project, tmp_path / "profiles") == project / "target/manifest.json"


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
