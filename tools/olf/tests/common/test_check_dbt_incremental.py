"""`olf check dbt` validates incremental models without a warehouse (#752).

Needs a real dbt, which only the `olf check dbt` environment installs, so the
plain test gate skips this module where `dbt` is not on PATH.
"""

import os
import shutil
from pathlib import Path

import pytest
import typer

DBT = shutil.which("dbt")
if DBT is None:
    pytest.skip("dbt is installed only in the check environment", allow_module_level=True)

from openlakeforge_runtime.dbt import render_profiles  # noqa: E402

from olf.common.commands.checks import _dbt  # noqa: E402
from olf.common.scaffold import _templates  # noqa: E402

_INCREMENTAL = """{{ config(materialized='incremental', unique_key='code', incremental_strategy='merge') }}
select code from @@FROM@@
{% if is_incremental() %}
where last_modified_t > (select coalesce(max(last_modified_t), 0) from {{ this }})
{% endif %}
"""


def _product(root: Path, model_from: str) -> Path:
    project = root / "lakehouse_code/gold/food_quality/dbt"
    files = {
        "dbt_project.yml": _templates.render_dbt_project_yml(domain="quality", product="food_quality"),
        "packages.yml": _templates.render_dbt_packages_yml(),
        "models/sources.yml": _templates.render_dbt_sources_yml(domain="quality", tables=(("products", "Validated."),)),
        "models/gold/mart_incremental.sql": _INCREMENTAL.replace("@@FROM@@", model_from),
    }
    for relative, text in files.items():
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_text(text, encoding="utf-8")
    return project


def _check(project: Path, tmp_path: Path) -> None:
    env = {
        **os.environ,
        **render_profiles.dbt_environment(),
        "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
        "OPENLAKEFORGE_DBT_TRINO_USER": "openlakeforge-dbt",
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino",
        "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
        "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "iceberg",
    }
    profiles = tmp_path / "profiles"
    render_profiles.write_profile(project, output_dir=profiles)
    assert DBT is not None
    _dbt._check_product(DBT, project, profiles_dir=str(profiles), cwd=tmp_path, env=env)


def test_an_incremental_model_passes_without_a_warehouse(tmp_path: Path) -> None:
    _check(_product(tmp_path, "{{ source('silver', 'products') }}"), tmp_path)


def test_an_incremental_model_with_an_unknown_source_still_fails(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit):
        _check(_product(tmp_path, "{{ source('silver', 'nope') }}"), tmp_path)


def test_an_incremental_model_with_an_unknown_ref_still_fails(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit):
        _check(_product(tmp_path, "{{ ref('nope') }}"), tmp_path)
