"""A customer project's dbt products parse against the distribution (#627).

The project sits in its own repository: no `libs/` beside `lakehouse_code/`,
and product names the distribution's baked profiles have never seen. Only
`olf check project-code` installs dbt, so the plain test gate skips this.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml

if shutil.which("dbt") is None:
    pytest.skip("dbt is installed only in the project-code check environment", allow_module_level=True)

from libs.dbt import render_profiles  # noqa: E402

from olf.common.scaffold import _templates  # noqa: E402

_PRODUCTS = {"food_quality": ("quality", "inspections"), "rna_catalog": ("genomics", "samples")}


def _customer_project(root: Path) -> list[Path]:
    projects = []
    for product, (domain, silver) in _PRODUCTS.items():
        dbt = root / "lakehouse_code/gold" / product / "dbt"
        files = {
            "dbt_project.yml": _templates.render_dbt_project_yml(domain=domain, product=product),
            "packages.yml": _templates.render_dbt_packages_yml(),
            "models/sources.yml": _templates.render_dbt_sources_yml(domain=domain, tables=((silver, "Validated."),)),
            f"models/gold/mart_{product}.sql": _templates.render_gold_mart_sql(
                mart=f"mart_{product}", first_silver_input=silver
            ),
            "models/gold/schema.yml": _templates.render_gold_schema_yml(gold_tables=((f"mart_{product}", "Mart."),)),
        }
        for relative, text in files.items():
            (dbt / relative).parent.mkdir(parents=True, exist_ok=True)
            (dbt / relative).write_text(text, encoding="utf-8")
        projects.append(dbt)
    return projects


def test_a_customer_project_parses_with_the_distributions_package_and_its_own_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "acme-lakehouse"
    projects = _customer_project(repository)
    profiles = tmp_path / "check-cache/dbt-profiles"
    monkeypatch.setenv(render_profiles.PROFILES_DIR_ENV, str(profiles))
    # Nothing but the bake itself may point dbt at the package.
    monkeypatch.delenv(render_profiles.PACKAGES_DIR_ENV, raising=False)
    assert not (repository / "libs").exists()

    for project in projects:
        manifest = json.loads(render_profiles.ensure_baked_manifest(project).read_text(encoding="utf-8"))

        product = project.parent.name
        models = [node for node in manifest["nodes"].values() if node["resource_type"] == "model"]
        assert [model["name"] for model in models] == [f"mart_{product}"]
        # The package's generate_schema_name, not dbt's default, named the schema.
        assert models[0]["schema"] == f"{product}_gold"
        assert "macro.openlakeforge_dbt.generate_schema_name" in manifest["macros"]

    assert sorted(yaml.safe_load((profiles / "profiles.yml").read_text(encoding="utf-8"))) == sorted(_PRODUCTS)
