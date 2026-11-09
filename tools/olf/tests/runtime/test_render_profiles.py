from pathlib import Path

import pytest
from openlakeforge_runtime.dbt import render_profiles


def _gold_product(root: Path, product: str) -> Path:
    dbt = root / "lakehouse_code/gold" / product / "dbt"
    dbt.mkdir(parents=True)
    (dbt / "dbt_project.yml").write_text(f"name: {product}\nprofile: {product}\n", encoding="utf-8")
    return dbt


def test_baked_profiles_follow_the_project_root_not_the_module_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(render_profiles.PROFILES_DIR_ENV, raising=False)
    monkeypatch.delenv(render_profiles.PROJECT_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert render_profiles.resolve_profiles_dir() == tmp_path / "generated/dbt-profiles"

    monkeypatch.setenv(render_profiles.PROJECT_ROOT_ENV, str(tmp_path / "elsewhere"))
    assert render_profiles.resolve_profiles_dir() == tmp_path / "elsewhere/generated/dbt-profiles"


def test_cli_discovers_the_gold_products_under_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _gold_product(tmp_path, "orders")
    monkeypatch.setattr("sys.argv", ["render_profiles", "--root", str(tmp_path)])

    render_profiles.main()

    assert "orders" in (tmp_path / "generated/dbt-profiles/profiles.yml").read_text(encoding="utf-8")


def test_dbt_threads_default_to_one_and_follow_the_stage_override(tmp_path: Path) -> None:
    profile = render_profiles.render_profile(_gold_product(tmp_path, "orders"))

    # dbt evaluates env_var() at run time, so the rendered text is checked for the variable it reads and the
    # default it falls back to; a literal number would leave a stage no way to size its own concurrency.
    (threads,) = [line for line in profile.splitlines() if line.strip().startswith("threads:")]
    assert "env_var('OPENLAKEFORGE_DBT_THREADS', '1')" in threads
