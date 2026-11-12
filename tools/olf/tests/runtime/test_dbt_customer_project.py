"""A customer project's Gold dbt project parses against the distribution (#627).

The project sits in its own repository: no `openlakeforge_runtime/` beside `lakehouse_code/`,
and products the distribution's own project has never seen. Only
`olf check project-code` installs dbt, so the plain test gate skips this.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml

if shutil.which("dbt") is None:
    pytest.skip("dbt is installed only in the project-code check environment", allow_module_level=True)

from openlakeforge_runtime.dbt import render_profiles  # noqa: E402

from olf.common.scaffold import _templates  # noqa: E402

_PRODUCTS = {"food_quality": ("quality", "inspections"), "rna_catalog": ("genomics", "samples")}


def _customer_project(root: Path) -> Path:
    gold = root / "lakehouse_code/gold"
    files = {
        "dbt_project.yml": _templates.render_dbt_project_yml(products=tuple(_PRODUCTS)),
        "packages.yml": _templates.render_dbt_packages_yml(),
        _templates.GOLD_SCHEMA_MACRO_PATH: _templates.render_dbt_schema_macro(),
    }
    for product, (domain, silver) in _PRODUCTS.items():
        files[f"models/_sources/{domain}.yml"] = _templates.render_dbt_sources_yml(
            domain=domain, tables=((silver, "Validated."),)
        )
        files[f"models/{product}/mart_{product}.sql"] = _templates.render_gold_mart_sql(
            mart=f"mart_{product}", domain=domain, first_silver_input=silver
        )
        files[f"models/{product}/schema.yml"] = _templates.render_gold_schema_yml(
            product=product, gold_tables=((f"mart_{product}", "Mart."),)
        )
    for relative, text in files.items():
        (gold / relative).parent.mkdir(parents=True, exist_ok=True)
        (gold / relative).write_text(text, encoding="utf-8")
    return gold


def test_a_customer_project_parses_with_the_distributions_package_and_its_own_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "acme-lakehouse"
    project = _customer_project(repository)
    profiles = tmp_path / "check-cache/dbt-profiles"
    monkeypatch.setenv(render_profiles.PROFILES_DIR_ENV, str(profiles))
    # Nothing but the bake itself may point dbt at the package.
    monkeypatch.delenv(render_profiles.PACKAGES_DIR_ENV, raising=False)
    assert not (repository / "openlakeforge_runtime").exists()

    manifest = json.loads(render_profiles.ensure_baked_manifest(project).read_text(encoding="utf-8"))

    models = {node["name"]: node for node in manifest["nodes"].values() if node["resource_type"] == "model"}
    assert sorted(models) == sorted(f"mart_{product}" for product in _PRODUCTS)
    for product in _PRODUCTS:
        model = models[f"mart_{product}"]
        # The package's generate_schema_name named the schema after the model's folder.
        assert model["schema"] == f"{product}_gold"
        assert model["group"] == product
        assert model["access"] == "private"
    assert "macro.openlakeforge_dbt.generate_schema_name" in manifest["macros"]
    assert sorted(yaml.safe_load((profiles / "profiles.yml").read_text(encoding="utf-8"))) == ["gold"]


def test_a_model_outside_a_product_folder_fails_to_parse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project = _customer_project(tmp_path / "acme-lakehouse")
    (project / "models/loose.sql").write_text("select 1 as one\n", encoding="utf-8")
    monkeypatch.setenv(render_profiles.PROFILES_DIR_ENV, str(tmp_path / "profiles"))

    with pytest.raises(Exception, match="returned non-zero exit status"):
        render_profiles.ensure_baked_manifest(project)
