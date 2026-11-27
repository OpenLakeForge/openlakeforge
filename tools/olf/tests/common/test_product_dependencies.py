from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_two_product_fixture
from openlakeforge_core import load_lakehouse_inventory, scan_product_dependencies

from olf.common import contracts_check


def _model(root: Path, product: str, name: str, sql: str) -> None:
    folder = root / "lakehouse_code" / "gold" / "models" / product
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.sql").write_text(sql, encoding="utf-8")


def _schema(root: Path, product: str, **access: str) -> None:
    folder = root / "lakehouse_code" / "gold" / "models" / product
    folder.mkdir(parents=True, exist_ok=True)
    models = "".join(f"  - name: {name}\n    access: {level}\n" for name, level in access.items())
    (folder / "schema.yml").write_text(f"version: 2\nmodels:\n{models}", encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    write_two_product_fixture(tmp_path)
    _model(tmp_path, "widgets_alpha", "dim_widget", "select 1 as widget_id from {{ source('widgets', 'source') }}")
    _model(tmp_path, "widgets_beta", "mart_widgets", "select * from {{ ref('dim_widget') }}")
    return tmp_path


def _gold(root: Path) -> Path:
    return root / "lakehouse_code" / "gold"


def test_a_ref_to_a_public_model_of_another_product_is_a_dependency(project: Path) -> None:
    _schema(project, "widgets_alpha", dim_widget="public")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.violations == ()
    assert found.producers_by_consumer == {"widgets_beta": ("widgets_alpha",)}
    assert found.consumers_by_producer == {"widgets_alpha": ("widgets_beta",)}
    assert [(d.consumer, d.producer, d.models) for d in found.dependencies] == [
        ("widgets_beta", "widgets_alpha", ("dim_widget",))
    ]


def test_a_ref_to_a_model_that_is_not_public_is_refused(project: Path) -> None:
    _schema(project, "widgets_alpha", dim_widget="protected")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert len(found.violations) == 1
    assert "widgets_beta.mart_widgets refs widgets_alpha.dim_widget" in found.violations[0]


def test_a_model_without_an_access_level_is_private(project: Path) -> None:
    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.violations


def test_a_ref_inside_a_product_or_a_comment_is_no_dependency(project: Path) -> None:
    _model(project, "widgets_alpha", "mart_alpha", "select * from {{ ref('dim_widget') }}")
    _model(project, "widgets_beta", "mart_other", "{# ref('dim_widget') #} select 1")
    _model(project, "widgets_beta", "mart_widgets", "select 1 as x")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.dependencies == ()
    assert found.violations == ()


def test_a_cycle_between_products_is_refused(project: Path) -> None:
    _schema(project, "widgets_alpha", dim_widget="public")
    _schema(project, "widgets_beta", mart_widgets="public")
    _model(project, "widgets_alpha", "dim_widget", "select * from {{ ref('mart_widgets') }}")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert any("cycle" in violation for violation in found.violations)


def test_the_inventory_exports_the_product_graph(project: Path) -> None:
    _schema(project, "widgets_alpha", dim_widget="public")

    inventory = load_lakehouse_inventory(project / "lakehouse_code")

    assert inventory.product_dependencies.producers_by_consumer == {"widgets_beta": ("widgets_alpha",)}


def test_contracts_check_reports_the_product_dependency(project: Path) -> None:
    _schema(project, "widgets_alpha", dim_widget="public")

    result = contracts_check._check_product_dependencies(project)

    assert result.ok
    assert "widgets_beta -> widgets_alpha (dim_widget)" in result.detail


def test_contracts_check_fails_on_a_private_cross_product_ref(project: Path) -> None:
    result = contracts_check._check_product_dependencies(project)

    assert not result.ok
    assert "access: public" in result.detail


def test_contracts_check_passes_when_no_product_reads_another(tmp_path: Path) -> None:
    write_two_product_fixture(tmp_path)

    result = contracts_check._check_product_dependencies(tmp_path)

    assert result.ok


def test_launch_order_puts_a_producer_before_its_consumers_and_keeps_the_rest_in_place(project: Path) -> None:
    _schema(project, "widgets_alpha", dim_widget="public")
    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.launch_order(["widgets_beta", "widgets_alpha"]) == ["widgets_alpha", "widgets_beta"]
    assert found.launch_order(["widgets_beta"]) == ["widgets_beta"]
    assert found.launch_order(["other", "widgets_beta", "widgets_alpha"]) == ["other", "widgets_alpha", "widgets_beta"]


def test_a_ref_in_a_plain_sql_comment_is_no_dependency_but_one_in_jinja_is(project: Path) -> None:
    _model(project, "widgets_beta", "mart_widgets", "-- was ref('dim_widget')\nselect 1 as x")

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).dependencies == ()

    _model(project, "widgets_beta", "mart_widgets", "-- depends_on: {{ ref('dim_widget') }}\nselect 1 as x")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])
    assert found.producers_by_consumer == {"widgets_beta": ("widgets_alpha",)}


def test_access_set_in_the_models_own_config_makes_it_public(project: Path) -> None:
    _model(project, "widgets_alpha", "dim_widget", "{{ config(access='public') }} select 1 as widget_id")

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).violations == ()


def test_access_set_in_dbt_project_yml_for_a_product_folder_makes_its_models_public(project: Path) -> None:
    (_gold(project) / "dbt_project.yml").write_text(
        "name: gold\nmodels:\n  gold:\n    +access: private\n    widgets_alpha:\n      +access: public\n",
        encoding="utf-8",
    )

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).violations == ()


def test_the_properties_file_overrides_dbt_project_yml(project: Path) -> None:
    (_gold(project) / "dbt_project.yml").write_text(
        "name: gold\nmodels:\n  gold:\n    widgets_alpha:\n      +access: public\n", encoding="utf-8"
    )
    _schema(project, "widgets_alpha", dim_widget="private")

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).violations


def test_launch_order_survives_a_cycle_in_the_order_given(project: Path) -> None:
    _schema(project, "widgets_alpha", dim_widget="public")
    _schema(project, "widgets_beta", mart_widgets="public")
    _model(project, "widgets_alpha", "dim_widget", "select * from {{ ref('mart_widgets') }}")
    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.launch_order(["widgets_beta", "widgets_alpha"]) == ["widgets_beta", "widgets_alpha"]


def test_whitespace_between_ref_and_its_parenthesis_still_finds_the_dependency(project: Path) -> None:
    _model(project, "widgets_beta", "mart_widgets", "select * from {{ ref ('dim_widget') }}")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.producers_by_consumer == {"widgets_beta": ("widgets_alpha",)}


def test_a_disabled_model_adds_no_dependency(project: Path) -> None:
    _model(project, "widgets_beta", "mart_widgets", "{{ config(enabled=false) }} select * from {{ ref('dim_widget') }}")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert (found.dependencies, found.violations) == ((), ())


def test_a_model_disabled_in_its_properties_file_adds_no_dependency(project: Path) -> None:
    folder = project / "lakehouse_code" / "gold" / "models" / "widgets_beta"
    (folder / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: mart_widgets\n    config:\n      enabled: false\n", encoding="utf-8"
    )

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).dependencies == ()


def test_a_ref_into_another_dbt_package_is_not_a_product_dependency(project: Path) -> None:
    (_gold(project) / "dbt_project.yml").write_text("name: gold\n", encoding="utf-8")
    _model(project, "widgets_beta", "mart_widgets", "select * from {{ ref('some_package', 'dim_widget') }}")

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).dependencies == ()


def test_a_ref_qualified_with_this_projects_name_is_a_product_dependency(project: Path) -> None:
    (_gold(project) / "dbt_project.yml").write_text("name: gold\n", encoding="utf-8")
    _schema(project, "widgets_alpha", dim_widget="public")
    _model(project, "widgets_beta", "mart_widgets", "select * from {{ ref('gold', 'dim_widget') }}")

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.producers_by_consumer == {"widgets_beta": ("widgets_alpha",)}


def test_access_and_enablement_come_from_executable_jinja_not_sql_comments(project: Path) -> None:
    _model(
        project,
        "widgets_alpha",
        "dim_widget",
        "-- config(access='private')\n{{ config(access='public') }} select 1 as widget_id",
    )

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).violations == ()

    _model(
        project,
        "widgets_alpha",
        "dim_widget",
        "-- config(access='public')\n{{ config(access='private') }} select 1 as widget_id",
    )

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).violations

    _model(project, "widgets_beta", "mart_widgets", "-- config(enabled=false)\nselect * from {{ ref('dim_widget') }}")

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).dependencies


def test_a_relationships_test_in_schema_yml_is_a_dependency_without_a_ref_in_sql(project: Path) -> None:
    _model(project, "widgets_beta", "mart_widgets", "select 1 as widget_id")
    _schema(project, "widgets_alpha", dim_widget="public")
    folder = project / "lakehouse_code" / "gold" / "models" / "widgets_beta"
    (folder / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: mart_widgets\n    columns:\n      - name: widget_id\n"
        "        data_tests:\n          - relationships:\n              to: ref('dim_widget')\n"
        "              field: widget_id\n",
        encoding="utf-8",
    )

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert found.producers_by_consumer == {"widgets_beta": ("widgets_alpha",)}
    assert found.violations == ()


def test_a_relationships_test_against_a_private_model_of_another_product_is_refused(project: Path) -> None:
    _model(project, "widgets_beta", "mart_widgets", "select 1 as widget_id")
    folder = project / "lakehouse_code" / "gold" / "models" / "widgets_beta"
    (folder / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: mart_widgets\n    data_tests:\n      - relationships:\n"
        "          to: ref('dim_widget')\n          field: widget_id\n",
        encoding="utf-8",
    )

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert any("widgets_beta.schema.yml refs widgets_alpha.dim_widget" in v for v in found.violations)


def test_a_properties_file_with_the_yaml_extension_is_scanned(project: Path) -> None:
    folder = project / "lakehouse_code" / "gold" / "models" / "widgets_alpha"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.yaml").write_text("version: 2\nmodels:\n  - name: dim_widget\n    access: public\n")

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).violations == ()


def test_a_ref_mentioned_in_a_description_is_not_a_dependency(project: Path) -> None:
    _model(project, "widgets_beta", "mart_widgets", "select 1 as widget_id")
    folder = project / "lakehouse_code" / "gold" / "models" / "widgets_beta"
    (folder / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: mart_widgets\n    description: Previously used ref('dim_widget')\n"
        "    columns:\n      - name: widget_id\n        description: ref('dim_widget') key\n",
        encoding="utf-8",
    )

    assert scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"]).dependencies == ()


def test_a_disabled_models_schema_tests_add_no_dependency(project: Path) -> None:
    _model(project, "widgets_beta", "mart_widgets", "select 1 as widget_id")
    folder = project / "lakehouse_code" / "gold" / "models" / "widgets_beta"
    (folder / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: mart_widgets\n    config:\n      enabled: false\n    data_tests:\n"
        "      - relationships:\n          to: ref('dim_widget')\n          field: widget_id\n",
        encoding="utf-8",
    )

    found = scan_product_dependencies(_gold(project), ["widgets_alpha", "widgets_beta"])

    assert (found.dependencies, found.violations) == ((), ())
