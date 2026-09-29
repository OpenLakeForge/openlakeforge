from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import olf
from olf.cli import app
from olf.deployment.context import Provider

runner = CliRunner()


def test_version_command_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == olf.__version__


def test_layers_enabled_exits_nonzero_for_a_disabled_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_ANALYTICS_ENABLED", "false")

    result = runner.invoke(app, ["layers", "enabled", "--layer", "analytics"])

    assert result.exit_code == 1


def test_artifacts_deploy_optional_layers_skips_disabled_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_ANALYTICS_ENABLED", "false")
    monkeypatch.setenv("OPENLAKEFORGE_GOVERNANCE_ENABLED", "false")
    monkeypatch.setattr(
        "olf.commands.artifacts.deploy_superset_reports", lambda: pytest.fail("reports should be skipped")
    )
    monkeypatch.setattr(
        "olf.commands.artifacts.deploy_openmetadata_metadata", lambda: pytest.fail("metadata should be skipped")
    )

    result = runner.invoke(app, ["artifacts", "deploy-optional-layers"])

    assert result.exit_code == 0
    assert "Skipping Superset report assets" in result.output
    assert "Skipping OpenMetadata governance metadata" in result.output


def _hydrate_stage_contract(monkeypatch: pytest.MonkeyPatch, stage: str = "dev") -> None:
    """Stand in for the stage bindings `provider_contract_environment` exports.

    The report commands resolve their Superset and Trino target from those
    bindings, so a test that stubs the contract hydration out has to supply
    them; without this the command correctly refuses to guess a stage.
    """
    monkeypatch.setenv("OPENLAKEFORGE_CONTRACT_STAGE", stage)
    monkeypatch.setenv("OPENLAKEFORGE_KUBE_NAMESPACE", f"olf-{stage}")
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CATALOG", f"lakehouse_{stage}")
    monkeypatch.setenv("OPENLAKEFORGE_DBT_TRINO_USER", f"olf-{stage}")
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_HOST", "trino.olf-system")
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_PORT", "8080")
    monkeypatch.setenv(
        "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI", f"trino://olf-{stage}@trino.olf-system:8080/lakehouse_{stage}"
    )


def test_superset_deploy_reports_hydrates_selected_provider_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, str] = {}
    calls: list[str] = []
    monkeypatch.setattr(
        "olf.commands.runtime.provider_contract_environment",
        lambda **kwargs: options.update(kwargs) or nullcontext(),
    )
    monkeypatch.setattr("olf.commands.superset.deploy_superset_reports", lambda **_: calls.append("reports"))

    result = runner.invoke(app, ["superset", "deploy-reports", "--provider", "aws"])

    assert result.exit_code == 0
    assert options["provider"] == "aws"
    assert calls == ["reports"]


def test_superset_deploy_reports_threads_a_custom_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, str] = {}
    monkeypatch.setattr(
        "olf.commands.runtime.provider_contract_environment",
        lambda **kwargs: options.update(kwargs) or nullcontext(),
    )
    monkeypatch.setattr("olf.commands.superset.deploy_superset_reports", lambda **_: None)

    result = runner.invoke(app, ["superset", "deploy-reports", "--project-root", "/srv/my-project"])

    assert result.exit_code == 0
    assert options["project_root"] == "/srv/my-project"


def test_superset_report_commands_hydrate_the_named_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both commands must select the stage they were told to, not a default.

    `provider_contract_environment` is what resolves the stage's namespace,
    Superset instance, and Trino catalog, so the stage has to reach it.
    """
    options: list[dict] = []
    monkeypatch.setattr(
        "olf.commands.runtime.provider_contract_environment",
        lambda **kwargs: options.append(kwargs) or nullcontext(),
    )
    monkeypatch.setattr("olf.commands.superset.deploy_superset_reports", lambda **_: None)
    monkeypatch.setattr("olf.commands.superset.export_superset_reports", lambda **_: None)

    deployed = runner.invoke(app, ["superset", "deploy-reports", "--stage", "prod"])
    exported = runner.invoke(app, ["superset", "export-reports", "--stage", "uat"])

    assert (deployed.exit_code, exported.exit_code) == (0, 0)
    assert [entry["stage"] for entry in options] == ["prod", "uat"]


def test_superset_export_reports_requires_an_explicit_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export overwrites the checked-in bundle from whichever Superset it
    reads, and #130 forbids rebuilding report state from PROD's UI. Naming
    the source stage is therefore the operator's decision, never a default.

    Asserted as the exit code rather than the message: Typer renders the
    usage error through rich, which falls back to an 80-column width when
    stdout is not a terminal and truncated the option name on a CI runner.
    """
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    monkeypatch.setattr("olf.commands.superset.export_superset_reports", lambda **_: None)

    missing = runner.invoke(app, ["superset", "export-reports"])
    supplied = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    # 2 is click's usage error: the argument parser refused the call.
    assert missing.exit_code == 2
    assert supplied.exit_code == 0


def test_superset_deploy_reports_fails_closed_for_a_stage_without_analytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    monkeypatch.setattr("olf.superset.deploy_reports", lambda *a, **k: pytest.fail("must not reach Superset"))
    _hydrate_stage_contract(monkeypatch, "prod")
    monkeypatch.setenv("OPENLAKEFORGE_ANALYTICS_ENABLED", "false")

    result = runner.invoke(app, ["superset", "deploy-reports", "--stage", "prod"])

    assert result.exit_code == 1
    assert "analytics disabled" in result.output


def test_openmetadata_deploy_hydrates_selected_provider_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, str] = {}
    calls: list[str] = []
    monkeypatch.setattr(
        "olf.commands.runtime.provider_contract_environment",
        lambda **kwargs: options.update(kwargs) or nullcontext(),
    )
    monkeypatch.setattr("olf.commands.openmetadata.deploy_openmetadata_metadata", lambda: calls.append("metadata"))

    result = runner.invoke(app, ["openmetadata", "deploy-metadata", "--provider", "azure"])

    assert result.exit_code == 0
    assert options["provider"] == "azure"
    assert calls == ["metadata"]


def test_artifacts_upload_hydrates_selected_provider_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, str] = {}
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "olf.commands.runtime.provider_contract_environment",
        lambda **kwargs: options.update(kwargs) or nullcontext(),
    )
    monkeypatch.setattr("olf.commands.artifacts.upload_manifests", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(
        app,
        [
            "artifacts",
            "upload-manifests",
            "--provider",
            "aws",
            "--namespace",
            "custom-lakehouse",
            "--via",
            "direct",
        ],
    )

    assert result.exit_code == 0
    assert options["provider"] == "aws"
    assert options["namespace"] == "custom-lakehouse"
    assert calls == [{"via": "direct", "manifest_root": "", "runtime_root": ""}]


def test_dbt_parse_hydrates_selected_provider_contracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project = tmp_path / "gold/demo/dbt"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    options: dict[str, str] = {}
    commands: list[list[str]] = []

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            commands.append(argv)

    class Resolver:
        def resolve(self, name: str) -> Path:
            assert name == "dbt"
            return Path("dbt")

    tools = SimpleNamespace(runner=Runner(), resolver=Resolver())
    monkeypatch.setattr(
        "olf.commands.runtime.provider_contract_environment",
        lambda **kwargs: options.update(kwargs) or nullcontext(),
    )
    monkeypatch.setattr("olf.commands.dbt.Toolkit.default", lambda: tools)

    result = runner.invoke(
        app,
        [
            "dbt",
            "parse",
            "--project-dir",
            str(project),
            "--provider",
            "aws",
            "--profile",
            "slim",
            "--namespace",
            "custom-lakehouse",
        ],
    )

    assert result.exit_code == 0
    assert options == {
        "provider": "aws",
        "profile": "slim",
        "namespace": "custom-lakehouse",
        "cluster_name": "",
        "kubeconfig_path": "",
        "project_root": "",
    }
    assert commands[-1][-2:] == ["--target", "aws_runtime"]


def test_dbt_parse_discovers_projects_under_the_contract_environments_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Project discovery must read config.repo_root() *after* entering the
    contract environment, since that's what applies --project-root's
    OPENLAKEFORGE_REPO_ROOT - reading it beforehand always resolved the
    ambient/default root instead of a custom --project-root selection.
    """
    project_root = tmp_path / "custom-project"
    project = project_root / "lakehouse_code/gold/demo/dbt"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    commands: list[list[str]] = []

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            commands.append(argv)

    class Resolver:
        def resolve(self, name: str) -> Path:
            assert name == "dbt"
            return Path("dbt")

    tools = SimpleNamespace(runner=Runner(), resolver=Resolver())

    @contextmanager
    def fake_provider_contract_environment(**kwargs):  # noqa: ANN003, ANN202
        monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", kwargs["project_root"])
        # The selected project root holds no `libs`, which stays
        # distribution-owned; without this the render_profiles import below
        # resolves against the project root and fails.
        monkeypatch.setenv("OLF_DISTRIBUTION_ROOT", str(Path(__file__).resolve().parents[3]))
        yield

    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", fake_provider_contract_environment)
    monkeypatch.setattr("olf.commands.dbt.Toolkit.default", lambda: tools)

    result = runner.invoke(app, ["dbt", "parse", "--project-root", str(project_root)])

    assert result.exit_code == 0
    assert commands[-1][-2:] == ["--target", "local_runtime"]
    assert str(project) in commands[-1]


def test_dbt_parse_imports_libs_from_the_distribution_root_not_the_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`libs` is distribution-owned, not project-owned.

    A --project-root selection built per the image-build contract (only
    lakehouse_code, libs supplied by the distribution) must still resolve
    `from libs.dbt.render_profiles import ...` - only inserting the
    project root on sys.path leaves that import unresolvable.
    """
    distribution_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "custom-project"
    project = project_root / "lakehouse_code/gold/demo/dbt"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    commands: list[list[str]] = []

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            commands.append(argv)

    class Resolver:
        def resolve(self, name: str) -> Path:
            assert name == "dbt"
            return Path("dbt")

    tools = SimpleNamespace(runner=Runner(), resolver=Resolver())

    @contextmanager
    def fake_provider_contract_environment(**kwargs):  # noqa: ANN003, ANN202
        monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", kwargs["project_root"])
        monkeypatch.setenv("OLF_DISTRIBUTION_ROOT", str(distribution_root))
        yield

    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", fake_provider_contract_environment)
    monkeypatch.setattr("olf.commands.dbt.Toolkit.default", lambda: tools)

    result = runner.invoke(app, ["dbt", "parse", "--project-root", str(project_root)])

    assert result.exit_code == 0
    assert (project / "profiles.yml").is_file()
    assert commands[-1][-2:] == ["--target", "local_runtime"]


def test_dbt_parse_selects_outputs_declared_by_provider_profiles() -> None:
    from olf.commands import dbt

    assert dbt._target_for_provider("local") == "local_runtime"
    assert dbt._target_for_provider("aws") == "aws_runtime"
    assert dbt._target_for_provider("azure") == "local_runtime"


def test_floe_generation_passes_the_selected_namespace_to_the_local_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    options: dict[str, str] = {}
    generated: list[dict] = []

    class FakeLocalProvider:
        context = SimpleNamespace(provider=Provider.LOCAL)
        config = SimpleNamespace(floe=object())
        tools = object()
        env = {}

    context = SimpleNamespace(
        paths=SimpleNamespace(
            repo_root=tmp_path, distribution_root=tmp_path, platform_terraform_dir=tmp_path / "contracts"
        ),
        namespace="custom-lakehouse",
        features=SimpleNamespace(governance_enabled=True),
    )
    monkeypatch.setattr(
        "olf.commands.deployment._build_context",
        lambda *args, **kwargs: options.update(kwargs) or context,
    )
    monkeypatch.setattr(
        "olf.commands.deployment._build_engine",
        lambda *args, **kwargs: SimpleNamespace(provider=FakeLocalProvider()),
    )
    monkeypatch.setattr("olf.deployment.local.provider.LocalProvider", FakeLocalProvider)
    monkeypatch.setattr(
        "olf.deployment.local.artifacts.applied_contract_environment", lambda *args, **kwargs: nullcontext({})
    )
    monkeypatch.setattr(
        "olf.deployment.floe_manifests.generate_local_manifests", lambda *args, **kwargs: generated.append(kwargs)
    )

    result = runner.invoke(app, ["floe", "generate-manifests", "--namespace", "custom-lakehouse"])

    assert result.exit_code == 0
    assert options["namespace"] == "custom-lakehouse"
    assert generated == [
        {
            "repo_root": tmp_path,
            "distribution_root": tmp_path,
            "namespace": "custom-lakehouse",
            "governance_enabled": True,
            "environ": {},
            "env": {},
        }
    ]


def test_floe_generation_honors_custom_contract_root_for_cloud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeBackend:
        def generate_floe_manifests(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            captured["generation"] = kwargs

    class FakeCloudProvider:
        foundation_facts = SimpleNamespace(kube_context="custom-cluster")
        context = SimpleNamespace(provider=Provider.AWS)
        config = object()
        backend = FakeBackend()
        tools = object()
        env = {}

    contract_root = tmp_path / "custom-contracts"
    context = SimpleNamespace(
        paths=SimpleNamespace(
            repo_root=tmp_path,
            distribution_root=tmp_path,
            platform_terraform_dir=tmp_path / "default-contracts",
            kubeconfig_path=tmp_path / "kubeconfig.yaml",
            port_forward_log_prefix=tmp_path / "port-forward",
        ),
        namespace="custom-lakehouse",
        features=SimpleNamespace(governance_enabled=True),
    )
    monkeypatch.setenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(contract_root))
    monkeypatch.setattr("olf.commands.deployment._build_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(
        "olf.commands.deployment._build_engine",
        lambda *args, **kwargs: SimpleNamespace(provider=FakeCloudProvider()),
    )
    monkeypatch.setattr(
        "olf.deployment.contract_env.applied_contract_environment",
        lambda **kwargs: captured.update(kwargs) or nullcontext({}),
    )

    result = runner.invoke(app, ["floe", "generate-manifests", "--provider", "aws"])

    assert result.exit_code == 0
    assert captured["contract_terraform_dir"] == contract_root
    assert captured["generation"] == {
        "repo_root": tmp_path,
        "distribution_root": tmp_path,
        "namespace": "custom-lakehouse",
        "governance_enabled": True,
        "environ": {},
        "env": {},
    }


def test_revision_compute_command_prints_runtime_artifact_revision(tmp_path: Path) -> None:
    path = tmp_path / "manifests/sales/sales.manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")

    result = runner.invoke(app, ["floe", "revision", "compute", "--runtime-root", str(tmp_path)])

    assert result.exit_code == 0
    assert result.output.startswith("sha256:")


def test_floe_revision_publish_fails_closed_when_no_ops_bucket_is_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "manifests/sales/sales.manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    monkeypatch.delenv("OPENLAKEFORGE_OPS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME", raising=False)

    result = runner.invoke(app, ["floe", "revision", "publish", "--runtime-root", str(tmp_path)])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_project_build_fails_closed_when_no_ops_bucket_is_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openlakeforge_domain import inventory_for

    from olf import config

    monkeypatch.delenv("OPENLAKEFORGE_OPS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME", raising=False)
    inventory_for(config.repo_root())  # sanity: reference project resolves before the command runs

    result = runner.invoke(
        app,
        [
            "project",
            "build",
            "--project",
            str(config.repo_root()),
            "--image",
            "ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "a" * 64,
        ],
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_superset_export_reports_defaults_come_from_the_first_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    from openlakeforge_domain import inventory_for

    from olf import config

    inventory = inventory_for(config.repo_root())
    default_dashboard = inventory.dashboards[0]
    default_product = next(product for product in inventory.products if product.id == default_dashboard.products[0])
    calls: list[dict] = []
    monkeypatch.setattr(
        "olf.superset.export_report",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 0
    assert calls[0]["report_source_dir"] == default_dashboard.report_source_dir
    assert calls[0]["bundle_name"] == default_dashboard.superset_export_bundle_name
    assert calls[0]["dashboard_title"] == default_product.display_name


def test_superset_export_reports_dashboard_title_prefers_the_bundles_own_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundle's own dashboard_title must win over displayName when they differ."""
    lakehouse_dir = tmp_path / "lakehouse_code"
    source_dir = lakehouse_dir / "bronze" / "crm"
    source_dir.mkdir(parents=True)
    (source_dir / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: orders
""",
        encoding="utf-8",
    )
    (lakehouse_dir / "lakehouse.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - crm
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: planned
    silver_tables:
      tables:
        - name: orders
          source: crm
          resource: orders
    products:
      - id: orders
        displayName: Sales Orders Product Metadata Name
        description: Sales orders.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_orders
dashboards:
  - name: orders
    products: [orders]
""",
        encoding="utf-8",
    )
    dashboards_dir = lakehouse_dir / "dashboards" / "superset" / "orders" / "dashboards"
    dashboards_dir.mkdir(parents=True)
    (dashboards_dir / "Live_1.yaml").write_text(
        "dashboard_title: The Actual Live Dashboard Title\nslug: sales-orders-live\n", encoding="utf-8"
    )

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    calls: list[dict] = []
    monkeypatch.setattr("olf.superset.export_report", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 0
    assert calls[0]["dashboard_title"] == "The Actual Live Dashboard Title"


def _seed_project_with_no_declared_dashboards(tmp_path: Path, *, bundle_dir_name: str) -> Path:
    """A project like #205's `--with-report` scaffold: one declared product,
    zero declared dashboards, and an undeclared report bundle directory that
    has no `dashboards/*.yaml` yet -- the draft state export-reports must be
    able to target."""
    lakehouse_dir = tmp_path / "lakehouse_code"
    source_dir = lakehouse_dir / "bronze" / "crm"
    source_dir.mkdir(parents=True)
    (source_dir / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: orders
""",
        encoding="utf-8",
    )
    (lakehouse_dir / "lakehouse.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - crm
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: planned
    silver_tables:
      tables:
        - name: orders
          source: crm
          resource: orders
    products:
      - id: orders
        displayName: Sales Orders Product Metadata Name
        description: Sales orders.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_orders
dashboards: []
""",
        encoding="utf-8",
    )
    (lakehouse_dir / "dashboards" / "superset" / bundle_dir_name).mkdir(parents=True)
    return tmp_path


def test_superset_export_reports_refuses_with_no_declared_dashboard_and_no_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = _seed_project_with_no_declared_dashboards(tmp_path, bundle_dir_name="orders")
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(repo_root))
    monkeypatch.delenv("SUPERSET_REPORT_SOURCE_DIR", raising=False)
    monkeypatch.setattr("olf.superset.export_report", lambda *a, **k: pytest.fail("must not export"))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 2
    assert "declares no dashboard to export" in result.output


def test_superset_export_reports_refuses_a_target_that_is_not_an_existing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = _seed_project_with_no_declared_dashboards(tmp_path, bundle_dir_name="orders")
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("SUPERSET_REPORT_SOURCE_DIR", "lakehouse_code/dashboards/superset/typo_ordrs")
    monkeypatch.setattr("olf.superset.export_report", lambda *a, **k: pytest.fail("must not export"))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_superset_export_reports_exports_an_undeclared_target_named_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#229: a project's first dashboard has no declared entry yet, but the
    scaffolded bundle directory already exists -- naming it explicitly must
    be enough to export it."""
    repo_root = _seed_project_with_no_declared_dashboards(tmp_path, bundle_dir_name="orders")
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("SUPERSET_REPORT_SOURCE_DIR", "lakehouse_code/dashboards/superset/orders")
    calls: list[dict] = []
    monkeypatch.setattr("olf.superset.export_report", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 0
    assert calls[0]["report_source_dir"] == "lakehouse_code/dashboards/superset/orders"
    assert calls[0]["bundle_name"] == "orders_superset_assets_export.zip"
    # No bundle title checked in yet, and nothing declares this bundle as any
    # particular product's dashboard, so the fallback is its own directory
    # name rather than a guessed product match.
    assert calls[0]["dashboard_title"] == "orders"


def test_superset_export_reports_refuses_an_absolute_target_outside_the_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`unpack_export_bundle` rmtrees metadata.yaml/databases/datasets/charts/
    dashboards under whatever SUPERSET_REPORT_SOURCE_DIR resolves to. An
    absolute override discards project_root under Path.__truediv__, so this
    must refuse it rather than let a mistyped/hostile value delete an
    arbitrary directory the process can write to."""
    repo_root = _seed_project_with_no_declared_dashboards(tmp_path, bundle_dir_name="orders")
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("SUPERSET_REPORT_SOURCE_DIR", str(tmp_path.parent))
    monkeypatch.setattr("olf.superset.export_report", lambda *a, **k: pytest.fail("must not export"))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    # Message not asserted: rich wraps typer's usage error at 80 columns when
    # stdout isn't a terminal, which splits this phrase across lines on CI
    # (see test_superset_export_reports_requires_an_explicit_stage above).
    assert result.exit_code == 2


def test_superset_export_reports_refuses_a_target_that_escapes_the_report_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `..`-bearing override can walk out of the report tree without ever
    tripping an unresolved-string comparison; only resolving both sides and
    checking real containment catches it."""
    repo_root = _seed_project_with_no_declared_dashboards(tmp_path, bundle_dir_name="orders")
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("SUPERSET_REPORT_SOURCE_DIR", "..")
    monkeypatch.setattr("olf.superset.export_report", lambda *a, **k: pytest.fail("must not export"))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 2


def test_superset_export_reports_refuses_an_out_of_tree_override_with_declared_dashboards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same destructive `unpack_export_bundle` call is reached from the
    declared-dashboard branch too -- an override there was previously used
    unvalidated, so this guard has to cover both branches, not just the one
    #229 added."""
    from olf import config

    monkeypatch.setenv("SUPERSET_REPORT_SOURCE_DIR", str(tmp_path))
    monkeypatch.setattr("olf.superset.export_report", lambda *a, **k: pytest.fail("must not export"))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)
    assert config.repo_root()  # sanity: the reference project (3 declared dashboards) resolves

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 2


def test_superset_export_reports_accepts_a_legitimate_override_with_declared_dashboards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative override naming a real, in-tree bundle must keep working --
    declared or not -- once the containment guard is in place."""
    from openlakeforge_domain import inventory_for

    from olf import config

    inventory = inventory_for(config.repo_root())
    other_dashboard = inventory.dashboards[1]
    monkeypatch.setenv("SUPERSET_REPORT_SOURCE_DIR", other_dashboard.report_source_dir)
    calls: list[dict] = []
    monkeypatch.setattr("olf.superset.export_report", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 0
    assert calls[0]["report_source_dir"] == other_dashboard.report_source_dir


def test_catalog_sync_namespaces_dispatches_to_glue_for_aws_glue_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from olf.commands import catalog

    calls: list[dict] = []
    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_PROVIDER", "aws-glue")
    monkeypatch.setattr(
        catalog,
        "_sync_glue_namespaces",
        lambda *, desired, dry_run, prune, namespace_prefix: calls.append(
            {"backend": "glue", "dry_run": dry_run, "prune": prune, "namespace_prefix": namespace_prefix}
        ),
    )
    monkeypatch.setattr(
        catalog, "_sync_polaris_namespaces", lambda **kwargs: calls.append({"backend": "polaris"})
    )
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())

    result = runner.invoke(app, ["catalog", "sync-namespaces", "--dry-run"])

    assert result.exit_code == 0
    assert calls == [{"backend": "glue", "dry_run": True, "prune": False, "namespace_prefix": "lakehouse_dev_"}]


def test_catalog_sync_namespaces_dispatches_to_polaris_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.commands import catalog

    calls: list[dict] = []
    monkeypatch.delenv("OPENLAKEFORGE_CATALOG_PROVIDER", raising=False)
    monkeypatch.setattr(
        catalog,
        "_sync_polaris_namespaces",
        lambda *, desired, dry_run, prune: calls.append(
            {"backend": "polaris", "dry_run": dry_run, "prune": prune}
        ),
    )
    monkeypatch.setattr(
        catalog, "_sync_glue_namespaces", lambda **kwargs: calls.append({"backend": "glue"})
    )
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())

    result = runner.invoke(app, ["catalog", "sync-namespaces", "--prune"])

    assert result.exit_code == 0
    assert calls == [{"backend": "polaris", "dry_run": False, "prune": True}]


def test_catalog_sync_namespaces_treats_false_prune_environment_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from olf.commands import catalog

    calls: list[dict] = []
    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_PRUNE_NAMESPACES", "false")
    monkeypatch.setattr(
        catalog,
        "_sync_polaris_namespaces",
        lambda *, desired, dry_run, prune: calls.append({"dry_run": dry_run, "prune": prune}),
    )
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())

    result = runner.invoke(app, ["catalog", "sync-namespaces"])

    assert result.exit_code == 0
    assert calls == [{"dry_run": False, "prune": False}]


def test_catalog_sync_namespaces_rejects_an_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.commands import catalog

    calls: list[str] = []
    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_PROVIDER", "snowflake")
    monkeypatch.setattr(catalog, "_sync_polaris_namespaces", lambda **kwargs: calls.append("polaris"))
    monkeypatch.setattr(catalog, "_sync_glue_namespaces", lambda **kwargs: calls.append("glue"))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())

    result = runner.invoke(app, ["catalog", "sync-namespaces"])

    assert result.exit_code == 1
    assert calls == []
    assert "snowflake" in result.output


def test_catalog_sync_namespaces_hydrates_selected_provider_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    """`olf catalog sync-namespaces` is a standalone Phase 2 command, same as
    `olf superset deploy-reports` etc. - it must hydrate the selected
    provider/project's contract environment before reading domain
    descriptors, not just the installed default.
    """
    from olf.commands import catalog

    options: dict[str, str] = {}
    monkeypatch.setattr(
        "olf.commands.runtime.provider_contract_environment",
        lambda **kwargs: options.update(kwargs) or nullcontext(),
    )
    monkeypatch.setattr(catalog, "_sync_polaris_namespaces", lambda **kwargs: None)

    result = runner.invoke(
        app, ["catalog", "sync-namespaces", "--provider", "aws", "--project-root", "/srv/my-project"]
    )

    assert result.exit_code == 0
    assert options["provider"] == "aws"
    assert options["project_root"] == "/srv/my-project"


class _Engine:
    """Records the phases an invocation drives, in the order ADR 0002 requires."""

    def __init__(self, *, changes: bool = False) -> None:
        self.changes = changes
        self.planned: list[str] = []
        self.deployed: list[str] = []

    def plan(self, phase) -> bool:  # noqa: ANN001
        self.planned.append(phase.value)
        return self.changes

    def deploy(self, phase) -> None:  # noqa: ANN001
        self.deployed.append(phase.value)


@pytest.fixture
def platform_cli(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """`olf platform` with its profile resolution and provider construction stubbed."""
    from olf.commands import platform as platform_module

    engine = _Engine()
    monkeypatch.setattr(platform_module, "deployment_context_for_profile", lambda _file, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(platform_module, "_engine", lambda context, *, var_file: engine)
    return engine


def test_platform_plan_reports_a_clean_tree_without_a_detailed_exit_code(platform_cli) -> None:  # noqa: ANN001
    result = runner.invoke(app, ["platform", "plan", "--file", "openlakeforge.yaml"])

    assert result.exit_code == 0
    assert "no changes" in result.output
    assert platform_cli.planned == ["all"]


def test_platform_plan_returns_two_only_when_asked_for_a_detailed_exit_code(platform_cli) -> None:  # noqa: ANN001
    """CI distinguishes "drift" from "failed" by this code, so a pending plan
    must stay exit 0 unless the caller opted into the third outcome."""
    platform_cli.changes = True

    assert runner.invoke(app, ["platform", "plan", "-f", "openlakeforge.yaml"]).exit_code == 0

    detailed = runner.invoke(app, ["platform", "plan", "-f", "openlakeforge.yaml", "--detailed-exitcode"])

    assert detailed.exit_code == 2
    assert "pending" in detailed.output


def test_platform_apply_all_runs_the_static_phases_in_order(platform_cli) -> None:  # noqa: ANN001
    """Artifacts are deliberately absent: `olf platform` owns only what Terraform owns."""
    result = runner.invoke(app, ["platform", "apply", "-f", "openlakeforge.yaml"])

    assert result.exit_code == 0
    assert platform_cli.deployed == ["foundation", "prefetch", "platform"]


def test_platform_apply_a_single_phase_runs_only_that_phase(platform_cli) -> None:  # noqa: ANN001
    result = runner.invoke(app, ["platform", "apply", "-f", "openlakeforge.yaml", "--phase", "foundation"])

    assert result.exit_code == 0
    assert platform_cli.deployed == ["foundation"]


def test_platform_rejects_a_phase_it_does_not_own(platform_cli) -> None:  # noqa: ANN001
    """`artifacts` is a real DeploymentPhase, which is exactly why it is rejected here."""
    result = runner.invoke(app, ["platform", "apply", "-f", "openlakeforge.yaml", "--phase", "artifacts"])

    assert result.exit_code != 0
    assert platform_cli.deployed == []


def test_platform_plan_reports_a_deployment_error_as_a_failure(platform_cli, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """A provider that cannot plan is a failed command, never a clean tree."""
    from olf.deployment.errors import DeploymentError

    def _raise(phase):  # noqa: ANN001, ANN202
        raise DeploymentError("terraform init failed")

    monkeypatch.setattr(platform_cli, "plan", _raise)

    result = runner.invoke(app, ["platform", "plan", "-f", "openlakeforge.yaml"])

    assert result.exit_code != 0
    assert "no changes" not in result.output


def test_platform_apply_reports_a_deployment_error_as_a_failure(platform_cli, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from olf.deployment.errors import DeploymentError

    def _raise(phase):  # noqa: ANN001, ANN202
        raise DeploymentError("terraform apply failed")

    monkeypatch.setattr(platform_cli, "deploy", _raise)

    result = runner.invoke(app, ["platform", "apply", "-f", "openlakeforge.yaml"])

    assert result.exit_code != 0


def test_platform_surfaces_a_provider_it_cannot_build_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable profile has to fail before Terraform runs, not during it."""
    from olf.commands import platform as platform_module
    from olf.deployment import engine as engine_module
    from olf.deployment.errors import UnsupportedProviderError

    monkeypatch.setattr(
        platform_module,
        "deployment_context_for_profile",
        lambda _file: SimpleNamespace(command_env=lambda *, base: dict(base)),
    )

    def _unbuildable(context, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        raise UnsupportedProviderError("provider 'gcp' has no adapter")

    monkeypatch.setattr(engine_module, "build_provider", _unbuildable)

    result = runner.invoke(app, ["platform", "plan", "-f", "openlakeforge.yaml"])

    assert result.exit_code != 0
    assert "no changes" not in result.output


def test_report_validate_accepts_the_reference_project_bundles(external_project: Path) -> None:
    result = runner.invoke(app, ["report", "validate", "--project-root", str(external_project)])

    assert result.exit_code == 0
    assert "promotable" in result.output


def test_report_validate_exits_nonzero_for_a_stage_bound_bundle(external_project: Path) -> None:
    database = next((external_project / "lakehouse_code/dashboards/superset").glob("*/databases/*.yaml"))
    database.write_text(database.read_text().replace("/iceberg", "/lakehouse_prod"))

    result = runner.invoke(app, ["report", "validate", "--project-root", str(external_project)])

    assert result.exit_code == 1
    assert "stage-bound physical name" in result.output


def test_report_validate_rejects_an_undeclared_dashboard(external_project: Path) -> None:
    result = runner.invoke(app, ["report", "validate", "nope", "--project-root", str(external_project)])

    assert result.exit_code != 0


def test_superset_export_reports_refuses_a_directory_nested_inside_a_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle is one directory under the report root, so a bundle's own
    `datasets/` is its contents, not another bundle. Accepting it would make
    `unpack_export_bundle` delete the managed entries inside that subtree."""
    root = _seed_project_with_no_declared_dashboards(tmp_path, bundle_dir_name="orders")
    nested = root / "lakehouse_code/dashboards/superset/orders/datasets"
    nested.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(root))
    monkeypatch.setenv("SUPERSET_REPORT_SOURCE_DIR", "lakehouse_code/dashboards/superset/orders/datasets")
    monkeypatch.setattr("olf.superset.export_report", lambda *a, **k: pytest.fail("must not export"))
    monkeypatch.setattr("olf.commands.runtime.provider_contract_environment", lambda **kwargs: nullcontext())
    _hydrate_stage_contract(monkeypatch)

    result = runner.invoke(app, ["superset", "export-reports", "--stage", "dev"])

    assert result.exit_code == 2
    assert "directly under" in result.output


def test_platform_contract_prints_the_applied_contract(platform_cli, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    import json

    from olf import contracts
    from olf.commands import platform as platform_module

    applied = {"schema_version": "3.0.0", "stages": {}}
    context = SimpleNamespace(paths=SimpleNamespace(platform_terraform_dir=Path("/platform")))
    monkeypatch.setattr(platform_module, "deployment_context_for_profile", lambda _file, **_kwargs: context)
    monkeypatch.setattr(contracts, "load_provider_contracts", lambda terraform_dir: applied)

    result = runner.invoke(app, ["platform", "contract", "-f", "openlakeforge.yaml"])

    assert result.exit_code == 0
    assert json.loads(result.output) == applied

    monkeypatch.setattr(contracts, "load_provider_contracts", lambda terraform_dir: None)
    assert runner.invoke(app, ["platform", "contract", "-f", "openlakeforge.yaml"]).exit_code != 0
