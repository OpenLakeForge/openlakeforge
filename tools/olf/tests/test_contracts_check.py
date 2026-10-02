import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

from olf import contracts_check
from olf.contracts_check import _hcl

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures"


def _repo_with_schemas(tmp_path: Path) -> Path:
    """A synthetic repo root carrying the real, unmodified schema files."""
    schema_dir = tmp_path / "docs" / "schema"
    schema_dir.mkdir(parents=True)
    shutil.copy(ROOT / "docs/schema/lakehouse.schema.json", schema_dir / "lakehouse.schema.json")
    shutil.copy(ROOT / "docs/schema/source.schema.json", schema_dir / "source.schema.json")
    shutil.copy(
        ROOT / "docs/schema/deployment-profile.schema.json", schema_dir / "deployment-profile.schema.json"
    )
    return tmp_path


def _write_descriptor(repo_root: Path, lakehouse_fixture: str, source_fixture: str = "valid_source.yaml") -> None:
    """Write the canonical lakehouse layout: lakehouse.yaml plus one source."""
    lakehouse_code = repo_root / "lakehouse_code"
    source_dir = lakehouse_code / "bronze" / "crm"
    source_dir.mkdir(parents=True, exist_ok=True)
    (lakehouse_code / "lakehouse.yaml").write_text(
        (FIXTURES / "descriptors" / lakehouse_fixture).read_text()
    )
    (source_dir / "source.yaml").write_text((FIXTURES / "descriptors" / source_fixture).read_text())


def test_descriptor_schema_conformance_passes_for_valid_lakehouse(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "valid_lakehouse.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert result.ok, result.detail


def test_descriptor_schema_conformance_rejects_missing_source_mapping(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_lakehouse_missing_bronze.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "source" in result.detail


def test_descriptor_schema_conformance_rejects_physical_fqn(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_lakehouse_physical_fqn.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "fqn" in result.detail.lower() or "physical" in result.detail.lower()


def test_descriptor_schema_conformance_rejects_physical_path_in_lakehouse_table(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_lakehouse_physical_path.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "path" in result.detail.lower() or "physical" in result.detail.lower()


def test_descriptor_schema_conformance_rejects_non_identifier_table_name(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_lakehouse_table_name.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "name" in result.detail.lower() or "pattern" in result.detail.lower()


def test_descriptor_schema_conformance_rejects_provider_field_in_lakehouse_table(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_lakehouse_provider_field.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "catalog" in result.detail.lower() or "provider-neutral" in result.detail.lower()


def test_descriptor_schema_conformance_rejects_provider_field_at_lakehouse_root(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_lakehouse_provider_field_root.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "catalog" in result.detail.lower() or "provider-neutral" in result.detail.lower()


def test_descriptor_schema_conformance_rejects_provider_field_on_table_group(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_lakehouse_table_group_field.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "catalog" in result.detail.lower() or "provider-neutral" in result.detail.lower()


def test_descriptor_schema_conformance_rejects_physical_path_in_source(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "valid_lakehouse.yaml", source_fixture="invalid_source_physical_path.yaml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "path" in result.detail.lower() or "physical" in result.detail.lower()


def test_descriptor_schema_conformance_fails_when_no_lakehouse_found(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    (repo_root / "lakehouse_code").mkdir()

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok


def test_descriptor_schema_conformance_passes_against_real_repo() -> None:
    result = contracts_check._check_descriptor_schema_conformance(ROOT)

    assert result.ok, result.detail


def _repo_with_local_contracts(
    tmp_path: Path, contracts_tf_fixture: str, main_tf_fixture: str = "valid_main.tf"
) -> Path:
    """A synthetic repo root where `local`'s contracts.tf/main.tf come from a
    fixture under test, while azure-poc/aws-poc and the aws-glue module are
    copied unmodified from the real repo -- isolating the fixture as the one
    variable under test."""
    hcl_fixtures = FIXTURES / "hcl"
    for env in ("azure-poc", "aws-poc"):
        env_dir = tmp_path / "infra/terraform/environments" / env
        env_dir.mkdir(parents=True)
        shutil.copy(ROOT / "infra/terraform/environments" / env / "contracts.tf", env_dir / "contracts.tf")
        shutil.copy(ROOT / "infra/terraform/environments" / env / "main.tf", env_dir / "main.tf")

    local_dir = tmp_path / "infra/terraform/environments/local"
    local_dir.mkdir(parents=True)
    (local_dir / "contracts.tf").write_text((hcl_fixtures / contracts_tf_fixture).read_text())
    (local_dir / "main.tf").write_text((hcl_fixtures / main_tf_fixture).read_text())

    glue_dir = tmp_path / "infra/terraform/modules/catalog/aws-glue"
    glue_dir.mkdir(parents=True)
    shutil.copy(
        ROOT / "infra/terraform/modules/catalog/aws-glue/main.tf",
        glue_dir / "main.tf",
    )
    return tmp_path


def test_hcl_structured_contracts_passes_for_valid_fixture(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "valid_local_contracts.tf")

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert result.ok, result.detail


def test_hcl_structured_contracts_rejects_missing_required_local(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "invalid_missing_required_local.tf")

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert "storage_contract" in result.detail


def test_hcl_structured_contracts_rejects_missing_check_block(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "invalid_missing_check_block.tf")

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert "local_contract_adapters_are_explicit" in result.detail


@pytest.mark.parametrize("env", ["azure-poc", "aws-poc"])
def test_hcl_structured_contracts_requires_the_stage_isolation_invariants_of_every_provider(
    tmp_path: Path, env: str
) -> None:
    """#134: a cloud root that stops pinning each stage's services to their own
    namespace and metadata database is one where DEV and PROD share a Dagster
    control plane. The invariant was only demanded of `local` while the split
    was landing, so a cloud regression passed every gate."""
    repo_root = _repo_with_local_contracts(tmp_path, "valid_local_contracts.tf")
    contracts_tf = repo_root / "infra/terraform/environments" / env / "contracts.tf"
    contracts_tf.write_text(
        contracts_tf.read_text().replace('check "stage_metadata_state_is_not_shared"', 'check "renamed"')
    )

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert f"{env}/contracts.tf" in result.detail
    assert "stage_metadata_state_is_not_shared" in result.detail


@pytest.mark.parametrize("env", ["azure-poc", "aws-poc"])
def test_hcl_structured_contracts_requires_the_stage_topology_locals_of_every_provider(
    tmp_path: Path, env: str
) -> None:
    """The per-stage service instances are indexed off these, so a root that
    drops one cannot express one Dagster instance per stage at all."""
    repo_root = _repo_with_local_contracts(tmp_path, "valid_local_contracts.tf")
    main_tf = repo_root / "infra/terraform/environments" / env / "main.tf"
    main_tf.write_text(main_tf.read_text().replace("stage_databases = merge(", "renamed_databases = merge("))

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert f"{env}/main.tf" in result.detail
    assert "stage_databases" in result.detail


def test_hcl_structured_contracts_rejects_forbidden_phase2_field(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "invalid_forbidden_phase2_field.tf")

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert "catalog_namespaces" in result.detail


def test_hcl_structured_contracts_rejects_missing_glue_removed_block(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "valid_local_contracts.tf")
    (repo_root / "infra/terraform/modules/catalog/aws-glue/main.tf").write_text(
        'locals {\n  rest_uri = "https://glue.example.amazonaws.com/iceberg"\n}\n'
    )

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert "removed" in result.detail.lower()


def test_hcl_structured_contracts_passes_against_real_repo() -> None:
    result = contracts_check._check_hcl_structured_contracts(ROOT)

    assert result.ok, result.detail


def test_installed_contract_environ_returns_none_for_a_source_checkout() -> None:
    assert _hcl._installed_contract_environ("local") is None


def test_installed_contract_environ_sets_state_and_data_roots_for_an_installed_layout(
    tmp_path: Path, monkeypatch
) -> None:
    from olf.distribution import RuntimeLayout

    resolved = tmp_path.resolve()
    layout = RuntimeLayout(
        mode="installed",
        distribution_root=resolved / "dist",
        project_root=resolved / "project",
        state_root=resolved / "state",
        work_root=resolved / "work",
        cache_root=resolved / "cache",
        catalog_path=resolved / "catalog.yaml",
        distribution_version="0.2.0",
        payload_sha256="0" * 64,
    )
    monkeypatch.setattr("olf.distribution.runtime_layout", lambda *a, **k: layout)

    environ = _hcl._installed_contract_environ("local")

    assert environ is not None
    assert environ["OPENLAKEFORGE_TERRAFORM_STATE_ROOT"] == str(resolved / "state" / "local")
    assert environ["OPENLAKEFORGE_TERRAFORM_DATA_ROOT"] == str(resolved / "work" / "local" / "terraform-data")


def test_hcl_phase_two_invariants_skips_when_no_applied_state(tmp_path: Path) -> None:
    result = contracts_check._check_hcl_phase_two_invariants(tmp_path)

    assert result.ok
    assert "skipped" in result.detail.lower()


def test_hcl_phase_two_invariants_passes_when_applied_state_is_clean(tmp_path: Path, monkeypatch) -> None:
    def fake_load(terraform_dir: str, *, environ=None):
        return {"schema_version": "2.0.0", "catalog": {"catalog_database_fqn": "polaris.lakehouse_dev"}}

    monkeypatch.setattr(_hcl.contracts_module, "load_provider_contracts", fake_load)

    result = contracts_check._check_hcl_phase_two_invariants(tmp_path)

    assert result.ok, result.detail
    assert "applied environment" in result.detail


def test_hcl_phase_two_invariants_rejects_forbidden_resolved_field(tmp_path: Path, monkeypatch) -> None:
    def fake_load(terraform_dir: str, *, environ=None):
        return {"schema_version": "2.0.0", "catalog": {"catalog_namespaces": ["silver", "gold"]}}

    monkeypatch.setattr(_hcl.contracts_module, "load_provider_contracts", fake_load)

    result = contracts_check._check_hcl_phase_two_invariants(tmp_path)

    assert not result.ok
    assert "catalog_namespaces" in result.detail


def test_floe_rendered_profile_passes_against_real_repo() -> None:
    result = contracts_check._check_floe_rendered_profile()

    assert result.ok, result.detail


def _write_floe_contract(repo_root: Path, domain: str, filename: str, fixture_name: str) -> None:
    contracts_dir = repo_root / "lakehouse_code" / "silver" / domain / "contracts" / "floe"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / filename).write_text((FIXTURES / "floe" / fixture_name).read_text())


def test_floe_contract_structure_rejects_shared_silver_namespace(tmp_path: Path) -> None:
    _write_floe_contract(tmp_path, "sales", "fixture.yml", "invalid_contract_shared_silver_namespace.yml")

    result = contracts_check._check_floe_contract_structure(tmp_path)

    assert not result.ok
    assert "silver" in result.detail


def test_floe_contract_structure_passes_against_real_repo() -> None:
    result = contracts_check._check_floe_contract_structure(ROOT)

    assert result.ok, result.detail


def test_floe_profile_templates_passes_against_real_repo() -> None:
    result = contracts_check._check_floe_profile_templates(ROOT)

    assert result.ok, result.detail


def test_helm_values_as_data_rejects_missing_compute_log_manager(tmp_path: Path) -> None:
    values_dir = tmp_path / "infra/helm/values/local"
    values_dir.mkdir(parents=True)
    (values_dir / "dagster.yaml").write_text(
        (FIXTURES / "helm" / "invalid_dagster_values_missing_compute_log_manager.yaml").read_text()
    )

    result = contracts_check._check_helm_values_as_data(tmp_path)

    assert not result.ok
    assert "computeLogManager" in result.detail


def test_helm_values_as_data_passes_against_real_repo() -> None:
    result = contracts_check._check_helm_values_as_data(ROOT)

    assert result.ok, result.detail


def test_run_contracts_check_rejects_missing_repo_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        contracts_check.run_contracts_check(tmp_path / "does-not-exist")


def test_run_contracts_check_passes_against_real_repo() -> None:
    report = contracts_check.run_contracts_check(ROOT)

    assert report.ok, report.render()


def test_run_contracts_check_passes_for_an_installed_project_layout(tmp_path: Path) -> None:
    """An installed project's root carries only `openlakeforge.yaml` and
    `lakehouse_code/` (ADR 0009) -- `docs/schema`, `infra/`, and `libs/` all
    live in the separate, immutable distribution root instead.
    Copying the real repo's `lakehouse_code/` (which carries real Floe
    contracts under `silver/*/contracts/floe/`) and `openlakeforge.yaml` into
    an otherwise-bare project dir, and pointing `distribution_root` at the
    real repo, reproduces exactly what `olf init` leaves behind."""
    project_root = tmp_path / "my-lakehouse"
    shutil.copytree(
        ROOT / "lakehouse_code", project_root / "lakehouse_code", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy(ROOT / "openlakeforge.yaml", project_root / "openlakeforge.yaml")

    report = contracts_check.run_contracts_check(project_root, distribution_root=ROOT)

    assert report.ok, report.render()


def test_lakehouse_schema_rejects_a_lakehouse_with_no_products_anywhere() -> None:
    """The versioned JSON Schema alone -- independent of the canonical
    model, for editors and external tooling that only validate against it
    -- must reject a lakehouse where every domain has an empty products
    array, the same state the canonical model rejects."""
    schema = json.loads((ROOT / "docs/schema/lakehouse.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    document = yaml.safe_load(
        (FIXTURES / "descriptors" / "invalid_lakehouse_no_products_anywhere.yaml").read_text()
    )

    errors = list(validator.iter_errors(document))

    assert errors
    assert any("does not contain items matching" in error.message for error in errors)


def test_lakehouse_schema_accepts_a_product_less_domain_alongside_a_productful_one() -> None:
    """A single product-less domain is still valid, as long as some other
    domain in the lakehouse has at least one product."""
    schema = json.loads((ROOT / "docs/schema/lakehouse.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    document = yaml.safe_load((FIXTURES / "descriptors" / "valid_lakehouse.yaml").read_text())
    document["domains"].append(
        {
            "name": "hr",
            "displayName": "Hr",
            "description": "HR domain.",
            "status": "planned",
            "silver_tables": {"tables": [{"name": "employees", "source": "crm", "resource": "employees"}]},
            "products": [],
        }
    )

    errors = list(validator.iter_errors(document))

    assert errors == []


_TRANSITIONAL_LAKEHOUSE = """\
apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: openlakeforge
displayName: OpenLakeForge
description: Empty OpenLakeForge project.
status: planned
sources: []
domains: []
dashboards: []
"""


def test_allow_incomplete_waives_only_the_transitional_cardinality_gaps(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    lakehouse_code = repo_root / "lakehouse_code"
    lakehouse_code.mkdir()
    (lakehouse_code / "lakehouse.yaml").write_text(_TRANSITIONAL_LAKEHOUSE)

    assert contracts_check.descriptor_schema_errors(repo_root) != []
    assert contracts_check.descriptor_schema_errors(repo_root, allow_incomplete=True) == []


def test_allow_incomplete_still_rejects_every_other_schema_violation(tmp_path: Path) -> None:
    """The transitional waiver is a cardinality escape hatch, not a bypass.

    A scaffold step that emits a structurally wrong descriptor must still
    fail, even while the project legitimately has no product yet.
    """
    repo_root = _repo_with_schemas(tmp_path)
    lakehouse_code = repo_root / "lakehouse_code"
    lakehouse_code.mkdir()
    document = yaml.safe_load(_TRANSITIONAL_LAKEHOUSE)
    document["name"] = "Not An Identifier"
    (lakehouse_code / "lakehouse.yaml").write_text(yaml.safe_dump(document))

    errors = contracts_check.descriptor_schema_errors(repo_root, allow_incomplete=True)

    assert any("name" in error for error in errors), errors


def test_allow_incomplete_does_not_relax_source_descriptors(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    lakehouse_code = repo_root / "lakehouse_code"
    (lakehouse_code / "bronze" / "crm").mkdir(parents=True)
    (lakehouse_code / "lakehouse.yaml").write_text(_TRANSITIONAL_LAKEHOUSE)
    source = yaml.safe_load((FIXTURES / "descriptors" / "valid_source.yaml").read_text())
    source["resources"] = []
    (lakehouse_code / "bronze" / "crm" / "source.yaml").write_text(yaml.safe_dump(source))

    errors = contracts_check.descriptor_schema_errors(repo_root, allow_incomplete=True)

    assert any("resources" in error for error in errors), errors


def test_schema_root_resolves_schemas_outside_the_project(tmp_path: Path) -> None:
    """Installed mode keeps the schemas in the immutable payload while the
    descriptors live in the user's writable project."""
    distribution = _repo_with_schemas(tmp_path / "distribution")
    project = tmp_path / "project"
    _write_descriptor(project, "valid_lakehouse.yaml")

    assert contracts_check.descriptor_schema_errors(
        project, schema_root=distribution / "docs" / "schema"
    ) == []


def _write_profile(repo_root: Path, fixture_name: str) -> None:
    (repo_root / "openlakeforge.yaml").write_text(
        (FIXTURES / "profiles" / fixture_name).read_text(), encoding="utf-8"
    )


def test_deployment_profile_schema_conformance_passes_for_a_valid_profile(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_profile(repo_root, "valid_slim_local.yaml")

    assert contracts_check.profile_schema_errors(repo_root) == []


def test_deployment_profile_schema_conformance_reports_canonical_model_rejection(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_profile(repo_root, "invalid_no_stage_enabled.yaml")

    errors = contracts_check.profile_schema_errors(repo_root)

    assert any("canonical model rejected profile" in error for error in errors)


def test_deployment_profile_schema_conformance_reports_missing_profile(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)

    errors = contracts_check.profile_schema_errors(repo_root)

    assert any("no openlakeforge.yaml found" in error for error in errors)


def test_deployment_profile_schema_root_resolves_schemas_outside_the_project(tmp_path: Path) -> None:
    distribution = _repo_with_schemas(tmp_path / "distribution")
    project = tmp_path / "project"
    project.mkdir()
    _write_profile(project, "valid_slim_local.yaml")

    assert (
        contracts_check.profile_schema_errors(project, schema_root=distribution / "docs" / "schema") == []
    )
