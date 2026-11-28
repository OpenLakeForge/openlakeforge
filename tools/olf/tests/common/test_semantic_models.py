from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import write_two_product_fixture
from openlakeforge_core import SUPPORTED_OSSIE_VERSION, load_lakehouse_inventory

from olf.common import contracts_check

SCHEMA_YML = """\
version: 2
models:
  - name: fct_lines
    access: public
    columns:
      - name: line_id
        description: One line.
        data_tests: [unique, not_null]
      - name: account_id
        data_tests:
          - relationships:
              to: ref('dim_account')
              field: account_id
      - name: order_status
      - name: net_amount
      - name: order_date
  - name: dim_account
    access: public
    columns:
      - name: account_id
        data_tests: [unique, not_null]
      - name: segment
"""

OSSIE = """\
version: 0.1.1
semantic_model:
  - name: widgets_core
    datasets:
      - name: fct_lines
        source: widgets_alpha_gold.fct_lines
        primary_key: [line_id]
        fields:
          - name: line_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: line_id}]}
          - name: account_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: account_id}]}
          - name: order_status
            expression: {dialects: [{dialect: ANSI_SQL, expression: order_status}]}
          - name: net_amount
            expression: {dialects: [{dialect: ANSI_SQL, expression: net_amount}]}
          - name: order_date
            expression: {dialects: [{dialect: ANSI_SQL, expression: order_date}]}
            dimension: {is_time: true}
      - name: dim_account
        source: widgets_alpha_gold.dim_account
        primary_key: [account_id]
        fields:
          - name: account_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: account_id}]}
    relationships:
      - name: lines_to_account
        from: fct_lines
        to: dim_account
        from_columns: [account_id]
        to_columns: [account_id]
    metrics:
      - name: net_revenue
        description: Net revenue of fulfilled lines.
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(CASE WHEN fct_lines.order_status = 'fulfilled' THEN fct_lines.net_amount END)
        ai_context:
          synonyms: [net sales]
          instructions: fulfilled orders only
"""


def _project(root: Path, ossie: str = OSSIE, schema: str = SCHEMA_YML) -> Path:
    write_two_product_fixture(root)
    models = root / "lakehouse_code" / "gold" / "models"
    (models / "widgets_alpha").mkdir(parents=True)
    (models / "widgets_beta").mkdir()
    (models / "widgets_alpha" / "schema.yml").write_text(schema, encoding="utf-8")
    folder = root / "lakehouse_code" / "semantic" / "widgets_alpha"
    folder.mkdir(parents=True)
    (folder / "widgets.ossie.yaml").write_text(ossie, encoding="utf-8")
    return root


def _check(root: Path) -> tuple[bool, str]:
    repo = Path(__file__).resolve().parents[4]
    result = next(
        r
        for r in contracts_check.run_contracts_check(root, distribution_root=repo).results
        if r.name == "semantic_models"
    )
    return result.ok, result.detail


def _breaks(tmp_path: Path, old: str, new: str, *, schema: str = SCHEMA_YML) -> str:
    assert old in OSSIE or old in schema
    root = _project(tmp_path, OSSIE.replace(old, new), schema.replace(old, new) if old in schema else schema)
    ok, detail = _check(root)
    assert not ok
    return detail


def test_a_valid_model_passes_and_is_exposed_by_the_inventory(tmp_path: Path) -> None:
    root = _project(tmp_path)

    ok, detail = _check(root)
    found = load_lakehouse_inventory(root / "lakehouse_code").semantic_inventory

    assert ok, detail
    assert found.diagnostics == ()
    (model,) = found.models
    assert (model.product, model.name, model.version) == ("widgets_alpha", "widgets_core", SUPPORTED_OSSIE_VERSION)
    assert model.file == "lakehouse_code/semantic/widgets_alpha/widgets.ossie.yaml"
    assert model.content_hash.startswith("sha256:")
    assert [d.name for d in model.datasets] == ["fct_lines", "dim_account"]
    assert model.datasets[0].primary_key == ("line_id",)
    assert model.datasets[0].fields[-1].is_time
    (metric,) = model.metrics
    assert metric.references == (("fct_lines", "order_status"), ("fct_lines", "net_amount"))
    assert metric.ai_context is not None and metric.ai_context.synonyms == ("net sales",)
    assert (model.relationships[0].from_dataset, model.relationships[0].to_dataset) == ("fct_lines", "dim_account")


def test_the_content_hash_follows_the_file(tmp_path: Path) -> None:
    root = _project(tmp_path)
    before = load_lakehouse_inventory(root / "lakehouse_code").semantic_inventory.models[0].content_hash
    path = root / "lakehouse_code" / "semantic" / "widgets_alpha" / "widgets.ossie.yaml"
    path.write_text(OSSIE + "\n# note\n", encoding="utf-8")

    after = load_lakehouse_inventory(root / "lakehouse_code").semantic_inventory.models[0].content_hash

    assert before != after


@pytest.mark.parametrize("version", ["0.3.0", "0.3.0.dev0", "0.2.0"])
def test_any_other_version_is_refused_naming_the_supported_one(tmp_path: Path, version: str) -> None:
    detail = _breaks(tmp_path, "version: 0.1.1", f"version: '{version}'")

    assert "widgets.ossie.yaml:1: [ossie-version]" in detail
    assert "only Ossie 0.1.1 is supported" in detail


def test_a_second_model_in_one_file_is_refused(tmp_path: Path) -> None:
    detail = _breaks(
        tmp_path,
        "  - name: widgets_core\n",
        "  - name: other\n    datasets: [{name: x, source: widgets_alpha_gold.dim_account}]\n  - name: widgets_core\n",
    )

    assert "[one-model-per-file]" in detail


def test_custom_extensions_are_refused_with_their_line(tmp_path: Path) -> None:
    detail = _breaks(
        tmp_path, "        primary_key: [line_id]\n", "        primary_key: [line_id]\n        custom_extensions: []\n"
    )

    assert "widgets.ossie.yaml:8: [no-custom-extensions]" in detail


def test_a_field_written_as_an_expression_is_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "expression: net_amount}", "expression: net_amount * 2}")

    assert "[field-equals-column]" in detail


def test_an_unqualified_metric_reference_is_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "THEN fct_lines.net_amount END", "THEN net_amount END")

    assert "[metric-qualified-references]" in detail


def test_a_metric_reading_an_undeclared_field_is_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "fct_lines.net_amount END", "fct_lines.gross END")

    assert "[metric-reference]" in detail and "fct_lines.gross" in detail


def test_a_stage_in_the_source_is_refused(tmp_path: Path) -> None:
    detail = _breaks(
        tmp_path, "source: widgets_alpha_gold.dim_account", "source: lakehouse_dev.widgets_alpha_gold.dim_account"
    )

    assert "[logical-source]" in detail


def test_a_source_that_dbt_does_not_declare_is_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "widgets_alpha_gold.dim_account", "widgets_alpha_gold.dim_missing")

    assert "[source-exists]" in detail


def test_a_source_in_another_product_needs_a_dependency_on_it(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "widgets_alpha_gold.dim_account", "widgets_beta_gold.dim_account")

    assert "[source-exists]" in detail


def test_a_declared_field_without_a_dbt_column_is_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "      - name: net_amount\n      - name: order_date\n", "      - name: order_date\n")

    assert "[field-is-column]" in detail and "net_amount" in detail


def test_a_primary_key_without_a_unique_test_is_refused(tmp_path: Path) -> None:
    detail = _breaks(
        tmp_path,
        "data_tests: [unique, not_null]\n      - name: account_id\n        data_tests:",
        "data_tests: [not_null]\n      - name: account_id\n        data_tests:",
    )

    assert "[key-test]" in detail and "fct_lines" in detail


def test_a_primary_key_without_a_not_null_test_is_refused(tmp_path: Path) -> None:
    detail = _breaks(
        tmp_path,
        "description: One line.\n        data_tests: [unique, not_null]",
        "description: One line.\n        data_tests: [unique]",
    )

    assert "[key-test]" in detail and "not_null" in detail


def test_a_relationship_without_a_dbt_relationships_test_is_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "field: account_id", "field: segment")

    assert "[relationship-test]" in detail


def test_duplicate_names_are_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "      - name: dim_account\n        source", "      - name: fct_lines\n        source")

    assert "[duplicate-name]" in detail


def test_a_folder_that_is_not_a_product_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakehouse_code" / "semantic" / "nonsense").mkdir()

    ok, detail = _check(root)

    assert not ok and "'nonsense' is not a product in governance.yml" in detail


def test_a_description_that_differs_from_dbt_warns_and_still_passes(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        OSSIE.replace(
            "          - name: line_id\n", "          - name: line_id\n            description: Something else.\n"
        ),
    )

    ok, detail = _check(root)

    assert ok and "[description-differs]" in detail


def test_the_vendored_schema_matches_its_provenance_record() -> None:
    schemas = Path(__file__).resolve().parents[4] / "docs" / "schema"
    record = json.loads((schemas / "ossie-0.1.1.schema.provenance.json").read_text(encoding="utf-8"))
    vendored = (schemas / record["file"]).read_bytes()

    assert hashlib.sha256(vendored).hexdigest() == record["sha256"]
    assert json.loads(vendored)["properties"]["version"]["const"] == SUPPORTED_OSSIE_VERSION
    assert record["tag"].startswith(f"osi-{SUPPORTED_OSSIE_VERSION}")


def test_a_qualified_reference_with_spaces_around_the_dot_is_accepted(tmp_path: Path) -> None:
    root = _project(tmp_path, OSSIE.replace("fct_lines.net_amount END", "fct_lines . net_amount END"))

    ok, detail = _check(root)

    assert ok, detail


def test_a_misspelled_unqualified_metric_field_is_refused(tmp_path: Path) -> None:
    detail = _breaks(tmp_path, "THEN fct_lines.net_amount END", "THEN net_amunt END")

    assert "[metric-qualified-references]" in detail and "net_amunt" in detail


@pytest.mark.parametrize("key", ["datasets", "relationships", "metrics"])
def test_a_collection_that_is_not_a_list_is_a_diagnostic_not_a_crash(tmp_path: Path, key: str) -> None:
    root = _project(tmp_path, OSSIE.replace(f"    {key}:\n", f"    {key}: 42\n    ignored_{key}:\n", 1))

    ok, detail = _check(root)

    assert not ok and f"`{key}` must be a list" in detail


def test_a_relationship_into_a_column_without_a_unique_test_is_refused(tmp_path: Path) -> None:
    detail = _breaks(
        tmp_path, "      - name: account_id\n        data_tests: [unique, not_null]", "      - name: account_id"
    )

    assert "[relationship-target-key]" in detail


def test_models_with_the_same_name_in_two_files_are_both_left_out(tmp_path: Path) -> None:
    root = _project(tmp_path)
    folder = root / "lakehouse_code" / "semantic" / "widgets_alpha"
    (folder / "copy.ossie.yaml").write_text(OSSIE, encoding="utf-8")

    ok, detail = _check(root)
    found = load_lakehouse_inventory(root / "lakehouse_code").semantic_inventory

    assert not ok and detail.count("[duplicate-name]") == 2
    assert found.models == ()


def test_a_semantic_folder_without_governance_fails(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakehouse_code" / "governance.yml").unlink()

    ok, detail = _check(root)

    assert not ok and "requires a readable governance.yml" in detail


def _cross_product(root: Path, access: str) -> Path:
    _project(root, OSSIE.replace("widgets_alpha_gold", "widgets_beta_gold"))
    beta = root / "lakehouse_code" / "gold" / "models" / "widgets_beta"
    (beta / "mart.sql").write_text(
        "select * from {{ ref('fct_lines') }} join {{ ref('dim_account') }} using (account_id)"
    )
    alpha = root / "lakehouse_code" / "gold" / "models" / "widgets_alpha"
    (alpha / "schema.yml").write_text(SCHEMA_YML.replace("access: public", f"access: {access}"), encoding="utf-8")
    (alpha / "fct_lines.sql").write_text("select 1 as line_id")
    (alpha / "dim_account.sql").write_text("select 1 as account_id")
    folder = root / "lakehouse_code" / "semantic"
    (folder / "widgets_beta").mkdir()
    (folder / "widgets_beta" / "w.ossie.yaml").write_text(OSSIE.replace("widgets_alpha_gold", "widgets_alpha_gold"))
    (folder / "widgets_alpha" / "widgets.ossie.yaml").unlink()
    return root


def test_a_public_model_of_a_product_it_reads_can_be_a_source(tmp_path: Path) -> None:
    ok, detail = _check(_cross_product(tmp_path, "public"))

    assert ok, detail


def test_a_private_model_of_another_product_cannot_be_a_source(tmp_path: Path) -> None:
    ok, detail = _check(_cross_product(tmp_path, "protected"))

    assert not ok and "[source-exists]" in detail


def test_the_sales_core_model_in_the_repository_is_valid() -> None:
    repo = Path(__file__).resolve().parents[4]

    found = load_lakehouse_inventory(repo / "lakehouse_code").semantic_inventory

    assert found.diagnostics == ()
    (model,) = (m for m in found.models if m.product == "sales_core")
    assert [d.name for d in model.datasets] == [
        "fct_order_lines",
        "dim_account",
        "dim_channel",
        "dim_product",
        "dim_promotion",
    ]
    assert len(model.relationships) == 4
    assert {m.name for m in model.metrics} == {"net_revenue", "gross_revenue"}
    assert next(f for f in model.datasets[0].fields if f.name == "order_date").is_time
