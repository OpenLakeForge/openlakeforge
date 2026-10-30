import json
import re
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

from olf.common import contracts_check
from olf.common.contracts_check import _hcl

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures"


def _repo_with_schemas(tmp_path: Path) -> Path:
    """A synthetic repo root carrying the real, unmodified schema files."""
    schema_dir = tmp_path / "docs" / "schema"
    schema_dir.mkdir(parents=True)
    shutil.copy(ROOT / "docs/schema/governance.schema.json", schema_dir / "governance.schema.json")
    shutil.copy(ROOT / "docs/schema/source.schema.json", schema_dir / "source.schema.json")
    shutil.copy(
        ROOT / "docs/schema/deployment-profile.schema.json", schema_dir / "deployment-profile.schema.json"
    )
    return tmp_path


def _write_descriptor(repo_root: Path, lakehouse_fixture: str, source_fixture: str = "valid_source.yaml") -> None:
    """Write the canonical project layout: governance.yml plus one source."""
    lakehouse_code = repo_root / "lakehouse_code"
    source_dir = lakehouse_code / "bronze" / "crm"
    source_dir.mkdir(parents=True, exist_ok=True)
    (lakehouse_code / "governance.yml").write_text(
        (FIXTURES / "descriptors" / lakehouse_fixture).read_text()
    )
    (source_dir / "source.yaml").write_text((FIXTURES / "descriptors" / source_fixture).read_text())


def test_descriptor_schema_conformance_passes_for_valid_governance(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "valid_governance.yml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert result.ok, result.detail












def test_descriptor_schema_conformance_rejects_provider_field_at_lakehouse_root(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "invalid_governance_provider_field.yml")

    result = contracts_check._check_descriptor_schema_conformance(repo_root)

    assert not result.ok
    assert "catalog" in result.detail.lower() or "provider-neutral" in result.detail.lower()




def test_descriptor_schema_conformance_rejects_physical_path_in_source(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_descriptor(repo_root, "valid_governance.yml", source_fixture="invalid_source_physical_path.yaml")

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
    tmp_path: Path,
    contracts_tf_fixture: str,
    main_tf_fixture: str = "valid_main.tf",
    outputs_tf_fixture: str | None = None,
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
        shutil.copy(ROOT / "infra/terraform/environments" / env / "outputs.tf", env_dir / "outputs.tf")

    local_dir = tmp_path / "infra/terraform/environments/local"
    local_dir.mkdir(parents=True)
    (local_dir / "contracts.tf").write_text((hcl_fixtures / contracts_tf_fixture).read_text())
    (local_dir / "main.tf").write_text((hcl_fixtures / main_tf_fixture).read_text())
    outputs_source = (
        hcl_fixtures / outputs_tf_fixture
        if outputs_tf_fixture
        else ROOT / "infra/terraform/environments/local/outputs.tf"
    )
    (local_dir / "outputs.tf").write_text(outputs_source.read_text())

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


def test_hcl_structured_contracts_rejects_secret_valued_root_outputs(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(
        tmp_path, "valid_local_contracts.tf", outputs_tf_fixture="invalid_secret_outputs.tf"
    )

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert "'s3_secret_key' is sensitive" in result.detail
    assert "'unwrapped' exposes a secret value" in result.detail


def test_hcl_structured_contracts_rejects_secret_values_in_the_contract_surface(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "invalid_secret_literal_contract.tf")

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert "secret_access_key looks like" in result.detail
    assert "root_client_secret looks like" in result.detail
    assert "secret_access_key_key" not in result.detail


def test_hcl_structured_contracts_rejects_a_secret_output_in_any_root_file(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "valid_local_contracts.tf")
    extra = repo_root / "infra/terraform/environments/local/extra.tf"
    extra.write_text('output "leak" {\n  value     = module.seaweedfs.s3_secret_key\n  sensitive = true\n}\n')

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert "local/extra.tf: output 'leak' is sensitive" in result.detail


def test_secret_value_fields_flags_values_but_not_references() -> None:
    contract = {
        "ok": {"credentials_secret_name": "s", "secret_access_key_key": "K", "token_uri": "http://x", "secrets": {}},
        "bad": [{"root_client_secret": "hunter2"}, {"password": "x"}],
    }

    assert _hcl.secret_value_fields(contract) == ["bad[0].root_client_secret", "bad[1].password"]


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*provider-contracts*.json")), ids=lambda p: p.name)
def test_provider_contract_fixtures_carry_no_secret_values(fixture: Path) -> None:
    assert _hcl.secret_value_fields(json.loads(fixture.read_text())) == []


def test_hcl_phase_two_invariants_rejects_secret_valued_contract_field(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts",
        lambda terraform_dir, *, environ=None: {"catalog": {}, "storage": {"secret_access_key": "hunter2"}},
    )

    result = contracts_check._check_hcl_phase_two_invariants(tmp_path)

    assert not result.ok
    assert "storage.secret_access_key" in result.detail


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
    from olf.common.distribution import RuntimeLayout

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
    monkeypatch.setattr("olf.common.distribution.runtime_layout", lambda *a, **k: layout)

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

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", fake_load)

    result = contracts_check._check_hcl_phase_two_invariants(tmp_path)

    assert result.ok, result.detail
    assert "applied environment" in result.detail


def test_hcl_phase_two_invariants_rejects_forbidden_resolved_field(tmp_path: Path, monkeypatch) -> None:
    def fake_load(terraform_dir: str, *, environ=None):
        return {"schema_version": "2.0.0", "catalog": {"catalog_namespaces": ["silver", "gold"]}}

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", fake_load)

    result = contracts_check._check_hcl_phase_two_invariants(tmp_path)

    assert not result.ok
    assert "catalog_namespaces" in result.detail


def _floe_project(repo_root: Path) -> Path:
    """A copy of the real Floe project to break one rule in."""
    shutil.copytree(ROOT / "lakehouse_code" / "silver", repo_root / "lakehouse_code" / "silver")
    return repo_root / "lakehouse_code" / "silver" / "sales" / "contracts" / "floe"


def test_floe_contract_structure_rejects_an_entity_writing_a_shared_silver_namespace(tmp_path: Path) -> None:
    floe = _floe_project(tmp_path)
    entity = floe / "orders.yml"
    entity.write_text(
        entity.read_text(encoding="utf-8")
        + 'sink: { accepted: { iceberg: { catalog: "iceberg_catalog", namespace: "silver" } } }\n',
        encoding="utf-8",
    )

    result = contracts_check._check_floe_contract_structure(tmp_path)

    assert not result.ok
    assert "orders.yml" in result.detail and "'sales_silver'" in result.detail


def test_floe_contract_structure_rejects_a_provider_named_catalog(tmp_path: Path) -> None:
    domain = _floe_project(tmp_path) / "_domain.yml"
    domain.write_text(domain.read_text(encoding="utf-8").replace('"iceberg_catalog"', '"polaris"'), encoding="utf-8")

    result = contracts_check._check_floe_contract_structure(tmp_path)

    assert not result.ok
    assert "'iceberg_catalog'" in result.detail


def test_floe_contract_structure_rejects_a_domain_named_unlike_its_folder(tmp_path: Path) -> None:
    domain = _floe_project(tmp_path) / "_domain.yml"
    domain.write_text(domain.read_text(encoding="utf-8").replace('name: "sales"', 'name: "revenue"'), encoding="utf-8")

    result = contracts_check._check_floe_contract_structure(tmp_path)

    assert not result.ok
    assert "domain folder 'sales'" in result.detail


def test_floe_contract_structure_passes_against_real_repo() -> None:
    result = contracts_check._check_floe_contract_structure(ROOT)

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




def test_governance_schema_accepts_a_product_less_domain() -> None:
    """A domain may be declared before its first product."""
    schema = json.loads((ROOT / "docs/schema/governance.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    document = yaml.safe_load((FIXTURES / "descriptors" / "valid_governance.yml").read_text())
    document["domains"].append(
        {
            "name": "hr",
            "displayName": "Hr",
            "description": "HR domain.",
            "status": "planned",
        }
    )

    errors = list(validator.iter_errors(document))

    assert errors == []


def test_allow_incomplete_accepts_a_project_without_governance_yml(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    (repo_root / "lakehouse_code").mkdir()

    assert contracts_check.descriptor_schema_errors(repo_root) != []
    assert contracts_check.descriptor_schema_errors(repo_root, allow_incomplete=True) == []






def test_allow_incomplete_does_not_relax_source_descriptors(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    lakehouse_code = repo_root / "lakehouse_code"
    (lakehouse_code / "bronze" / "crm").mkdir(parents=True)
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
    _write_descriptor(project, "valid_governance.yml")

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


def test_the_schema_accepts_the_github_issuer_and_rejects_a_hand_written_mapping(tmp_path: Path) -> None:
    repo_root = _repo_with_schemas(tmp_path)
    _write_profile(repo_root, "valid_github_identity.yaml")
    assert contracts_check.profile_schema_errors(repo_root) == []

    profile = repo_root / "openlakeforge.yaml"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("issuer: github\n", "issuer: github\n    role_claim: groups\n"),
        encoding="utf-8",
    )

    assert contracts_check.profile_schema_errors(repo_root)


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


def test_hcl_structured_contracts_rejects_credential_handling_in_an_identity_module(tmp_path: Path) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "valid_local_contracts.tf")
    module = repo_root / "infra/terraform/modules/identity/dex"
    module.mkdir(parents=True)
    (module / "main.tf").write_text(
        'resource "random_password" "admin" {\n  length = 32\n}\n'
        'resource "kubernetes_secret_v1" "admin" {\n  data = {}\n}\n'
        'data "kubernetes_secret_v1" "admin" {\n  metadata {\n    name = "admin"\n  }\n}\n'
        'output "leak" {\n  value     = "x"\n  sensitive = true\n}\n'
    )

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    for expected in (
        "resource random_password.admin",
        "resource kubernetes_secret_v1.admin",
        "data kubernetes_secret_v1.admin",
        "output 'leak' is sensitive",
    ):
        assert expected in result.detail


@pytest.mark.parametrize("module_path", ["storage/postgresql", "catalog/polaris", "analytics/superset"])
def test_hcl_structured_contracts_rejects_credential_handling_in_job_modules(tmp_path: Path, module_path: str) -> None:
    repo_root = _repo_with_local_contracts(tmp_path, "valid_local_contracts.tf")
    module = repo_root / "infra/terraform/modules" / module_path
    module.mkdir(parents=True)
    (module / "credentials.tf").write_text('resource "random_password" "admin" {\n  length = 32\n}\n')

    result = contracts_check._check_hcl_structured_contracts(repo_root)

    assert not result.ok
    assert f"{module_path}/credentials.tf: resource random_password.admin" in result.detail


def test_hcl_structured_contracts_accepts_the_real_job_credential_modules() -> None:
    assert _hcl._job_credential_module_errors(ROOT) == []


def test_dex_config_expands_env_only_where_dex_does() -> None:
    """Dex expands `$NAME` in connector and storage config but not in a static client's `secret`;
    a client must name its env var with `secretEnv` (verified against the pinned image)."""
    main = (ROOT / "infra/terraform/modules/identity/dex/main.tf").read_text()
    placeholders = re.findall(r'"\$[A-Z_]+"', main)
    assert sorted(placeholders) == ['"$DEX_DB_PASSWORD"', '"$GITHUB_CLIENT_ID"', '"$GITHUB_CLIENT_SECRET"']
    clients = main[main.index("staticClients") :]
    assert "secretEnv" in clients and not re.search(r"^\s*secret\s*=", clients, re.MULTILINE)


def _project_with_incremental_orders(repo_root: Path) -> Path:
    """A copy of the real project whose `crm/orders` resource loads incrementally."""
    shutil.copytree(ROOT / "lakehouse_code", repo_root / "lakehouse_code")
    source = repo_root / "lakehouse_code" / "bronze" / "crm" / "source.yaml"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "  - name: orders\n",
            "  - name: orders\n    load: { mode: incremental, cursor: updated_at, primary_key: [order_id] }\n",
        ),
        encoding="utf-8",
    )
    return repo_root / "lakehouse_code" / "silver" / "sales" / "contracts" / "floe" / "orders.yml"


def test_silver_load_modes_reject_overwriting_an_incremental_resource(tmp_path: Path) -> None:
    _project_with_incremental_orders(tmp_path)

    result = contracts_check._check_silver_load_modes(tmp_path)

    assert not result.ok
    assert "orders.yml" in result.detail and "crm/orders" in result.detail


@pytest.mark.parametrize("write_mode", ["merge_scd1", "merge_scd2"])
def test_silver_load_modes_accept_new_files_merged_on_keys(tmp_path: Path, write_mode: str) -> None:
    entity = _project_with_incremental_orders(tmp_path)
    entity.write_text(
        entity.read_text(encoding="utf-8") + f'incremental_mode: "file"\nsink: {{ write_mode: "{write_mode}" }}\n',
        encoding="utf-8",
    )

    result = contracts_check._check_silver_load_modes(tmp_path)

    assert result.ok, result.detail


def test_silver_load_modes_reject_appending_a_full_resource(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "lakehouse_code", tmp_path / "lakehouse_code")
    entity = tmp_path / "lakehouse_code" / "silver" / "supply_chain" / "contracts" / "floe" / "warehouses.yml"
    entity.write_text(entity.read_text(encoding="utf-8") + 'sink: { write_mode: "append" }\n', encoding="utf-8")

    result = contracts_check._check_silver_load_modes(tmp_path)

    assert not result.ok
    assert "warehouses.yml" in result.detail and "every row again" in result.detail


def test_silver_load_modes_pass_against_real_repo() -> None:
    result = contracts_check._check_silver_load_modes(ROOT)

    assert result.ok, result.detail


def test_source_schema_requires_a_cursor_for_an_incremental_resource() -> None:
    schema = json.loads((ROOT / "docs" / "schema" / "source.schema.json").read_text(encoding="utf-8"))
    document = yaml.safe_load((ROOT / "lakehouse_code" / "bronze" / "crm" / "source.yaml").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    document["resources"][0]["load"] = {"mode": "incremental", "primary_key": ["account_id"]}
    assert any("cursor" in error.message for error in validator.iter_errors(document))
    document["resources"][0]["load"]["cursor"] = "updated_at"
    assert not list(validator.iter_errors(document))


def test_silver_load_modes_reject_appending_an_incremental_resource(tmp_path: Path) -> None:
    """A Bronze full refresh lands every row again, which an appended table would duplicate."""
    entity = _project_with_incremental_orders(tmp_path)
    entity.write_text(
        entity.read_text(encoding="utf-8") + 'incremental_mode: "file"\nsink: { write_mode: "append" }\n',
        encoding="utf-8",
    )

    result = contracts_check._check_silver_load_modes(tmp_path)

    assert not result.ok
    assert "orders.yml" in result.detail and "'append'" in result.detail
