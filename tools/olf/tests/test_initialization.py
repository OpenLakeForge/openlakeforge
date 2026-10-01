from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from olf.cli import app
from olf.distribution import RuntimeLayout
from olf.initialization import InitializationError, ProjectInitializer, default_profile_name
from olf.profile import Preset, StageName, validate_deployment_profile
from olf.scaffold._commit import commit_plan
from olf.scaffold._shared import ScaffoldError
from olf.scaffold.product import plan_product_new
from olf.scaffold.source import plan_source_new

runner = CliRunner()


def _layout(tmp_path: Path) -> RuntimeLayout:
    distribution = tmp_path / "distribution"
    (distribution / "openlakeforge.yaml").parent.mkdir(parents=True)
    (distribution / "openlakeforge.yaml").write_text(
        "apiVersion: openlakeforge.io/v1alpha1\nkind: DeploymentProfile\n",
        encoding="utf-8",
    )
    definitions = distribution / "lakehouse_code" / "definitions.py"
    definitions.parent.mkdir(parents=True)
    definitions.write_text("defs = object()\n", encoding="utf-8")
    (definitions.parent / "__init__.py").write_text("", encoding="utf-8")
    (definitions.parent / "demo.txt").write_text("demo\n", encoding="utf-8")
    (definitions.parent / "__pycache__").mkdir()
    (definitions.parent / "__pycache__" / "definitions.pyc").write_bytes(b"stale")
    schema = distribution / "docs" / "schema"
    schema.mkdir(parents=True)
    root = Path(__file__).resolve().parents[3]
    for name in ("lakehouse.schema.json", "source.schema.json"):
        (schema / name).write_bytes((root / "docs" / "schema" / name).read_bytes())
    project = tmp_path / "project"
    project.mkdir()
    return RuntimeLayout(
        mode="installed",
        distribution_root=distribution,
        project_root=project,
        state_root=tmp_path / "state",
        work_root=tmp_path / "work",
        cache_root=tmp_path / "cache",
        catalog_path=distribution / "release" / "component-catalog.yaml",
        distribution_version="0.1.0-alpha.1",
        payload_sha256="a" * 64,
    )


class _Manager:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_all(self) -> dict[str, Path]:
        self.calls += 1
        return {}


def _initializer(layout: RuntimeLayout, manager: _Manager | None = None) -> ProjectInitializer:
    active_manager = manager or _Manager()
    docker = SimpleNamespace(
        resolve_current_engine_endpoint=lambda **_kwargs: "unix:///tmp/docker.sock",
        version=lambda **_kwargs: SimpleNamespace(stdout="Docker Engine 1.0\n"),
    )
    tools = SimpleNamespace(docker=docker, resolver=SimpleNamespace(resolve=lambda _name: Path("/usr/bin/tool")))
    return ProjectInitializer(
        layout_resolver=lambda _env: layout,
        manager_factory=lambda _path: active_manager,
        toolkit_factory=lambda **_kwargs: tools,
    )


def test_default_init_copies_a_writable_demo_and_keeps_unowned_paths(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout.project_root / ".git").mkdir()
    manager = _Manager()

    result = _initializer(layout, manager).initialize(environ={"OLF_TOOLCHAIN_MODE": "managed"})

    assert result.lakehouse_root == layout.project_root / "lakehouse_code"
    assert (layout.project_root / "openlakeforge.yaml").is_file()
    assert result.next_command == "olf platform apply -f openlakeforge.yaml"
    assert (result.lakehouse_root / "demo.txt").read_text(encoding="utf-8") == "demo\n"
    assert os.access(result.lakehouse_root / "demo.txt", os.W_OK)
    assert (layout.project_root / ".git").is_dir()
    assert not (result.lakehouse_root / "__pycache__").exists()
    assert manager.calls == 1


def test_empty_init_has_no_demo_entities_and_scaffolds_to_a_strict_project(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    initializer = _initializer(layout)

    result = initializer.initialize(empty=True)

    document = yaml.safe_load((result.lakehouse_root / "lakehouse.yaml").read_text(encoding="utf-8"))
    assert document["sources"] == []
    assert document["domains"] == []
    assert document["dashboards"] == []
    assert not (result.lakehouse_root / "demo.txt").exists()
    assert "olf source new" in result.next_command

    schema_root = layout.distribution_root / "docs" / "schema"
    source_plan = plan_source_new(layout.project_root, source="crm", display_name=None, resources=("accounts",))
    with pytest.raises(ScaffoldError, match="domains must be a non-empty array"):
        commit_plan(layout.project_root, source_plan, schema_root=schema_root)
    commit_plan(layout.project_root, source_plan, schema_root=schema_root, allow_transitional=True)
    product_plan = plan_product_new(
        layout.project_root,
        target="sales/accounts_report",
        display_name=None,
        silver_inputs=(),
        inputs=(("crm", "accounts"),),
        gold_tables=("mart_accounts",),
        with_report=False,
    )
    commit_plan(layout.project_root, product_plan, schema_root=schema_root)

    from openlakeforge_domain import load_lakehouse_inventory

    assert [product.id for product in load_lakehouse_inventory(layout.project_root).products] == ["accounts_report"]


def test_init_refuses_an_existing_lakehouse_before_provisioning_tools(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout.project_root / "lakehouse_code").mkdir()
    manager = _Manager()

    with pytest.raises(InitializationError, match="refusing to overwrite"):
        _initializer(layout, manager).initialize()

    assert manager.calls == 0


def test_init_refuses_an_existing_profile_before_provisioning_tools(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout.project_root / "openlakeforge.yaml").write_text("existing\n", encoding="utf-8")
    manager = _Manager()

    with pytest.raises(InitializationError, match="refusing to overwrite"):
        _initializer(layout, manager).initialize()

    assert manager.calls == 0


def test_default_init_writes_a_dev_only_slim_profile_named_after_the_directory(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    _initializer(layout).initialize()

    profile = validate_deployment_profile(yaml.safe_load((layout.project_root / "openlakeforge.yaml").read_text()))
    assert profile.name == "project"
    assert profile.preset is Preset.SLIM
    assert {stage.name.value for stage in profile.stages if stage.enabled} == {"dev"}


def test_init_renders_the_requested_stages_preset_and_name(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    _initializer(layout).initialize(stages=(StageName.DEV, StageName.PROD), preset=Preset.FULL, name="acme-data")

    profile = validate_deployment_profile(yaml.safe_load((layout.project_root / "openlakeforge.yaml").read_text()))
    assert profile.name == "acme-data"
    assert profile.preset is Preset.FULL
    assert {stage.name.value for stage in profile.stages if stage.enabled} == {"dev", "prod"}


def test_init_rejects_an_invalid_topology_before_touching_the_project(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    manager = _Manager()

    with pytest.raises(InitializationError, match="dev"):
        _initializer(layout, manager).initialize(stages=(StageName.PROD,))

    assert manager.calls == 0
    assert list(layout.project_root.iterdir()) == []


def test_profile_only_adds_a_profile_to_an_existing_project_without_tools(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout.project_root / "lakehouse_code").mkdir()
    (layout.project_root / "lakehouse_code" / "lakehouse.yaml").write_text("mine\n", encoding="utf-8")
    manager = _Manager()

    result = _initializer(layout, manager).initialize(profile_only=True, stages=(StageName.DEV, StageName.PROD))

    assert result.profile_only
    assert manager.calls == 0
    assert (layout.project_root / "lakehouse_code" / "lakehouse.yaml").read_text(encoding="utf-8") == "mine\n"
    profile = validate_deployment_profile(yaml.safe_load((layout.project_root / "openlakeforge.yaml").read_text()))
    assert {stage.name.value for stage in profile.stages if stage.enabled} == {"dev", "prod"}

    with pytest.raises(InitializationError, match="refusing to overwrite"):
        _initializer(layout, manager).initialize(profile_only=True)


@pytest.mark.parametrize(
    ("directory", "expected"),
    [("My Lakehouse", "my-lakehouse"), ("2026_sales--data_", "sales-data"), ("___", "openlakeforge")],
)
def test_default_profile_name_is_a_valid_label(directory: str, expected: str) -> None:
    assert default_profile_name(Path("/x") / directory) == expected


def test_init_leaves_no_project_when_docker_is_unreachable(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    manager = _Manager()
    from olf.deployment.errors import CommandExecutionError

    docker = SimpleNamespace(
        resolve_current_engine_endpoint=lambda **_kwargs: None,
        version=lambda **_kwargs: (_ for _ in ()).throw(CommandExecutionError(["docker", "version"], 1)),
    )
    tools = SimpleNamespace(docker=docker, resolver=SimpleNamespace(resolve=lambda _name: Path("/usr/bin/tool")))
    initializer = ProjectInitializer(
        layout_resolver=lambda _env: layout,
        manager_factory=lambda _path: manager,
        toolkit_factory=lambda **_kwargs: tools,
    )

    with pytest.raises(InitializationError, match="Docker engine"):
        initializer.initialize(environ={"OLF_TOOLCHAIN_MODE": "managed"})

    assert manager.calls == 1
    assert not (layout.project_root / "lakehouse_code").exists()


def test_cli_init_renders_the_follow_up_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = SimpleNamespace(
        lakehouse_root=tmp_path / "lakehouse_code",
        next_command="olf platform apply -f openlakeforge.yaml",
        profile_only=False,
    )
    monkeypatch.setattr("olf.commands.init.initialize_project", lambda **_kwargs: result)

    invocation = runner.invoke(app, ["init"])

    assert invocation.exit_code == 0, invocation.output
    assert "Next: olf platform apply -f openlakeforge.yaml" in invocation.output


def test_init_rejects_a_corrupt_distribution_before_touching_the_project(tmp_path: Path) -> None:
    from olf.distribution import DistributionError

    project = tmp_path / "project"
    project.mkdir()
    manager = _Manager()
    initializer = ProjectInitializer(
        layout_resolver=lambda _env: (_ for _ in ()).throw(DistributionError("payload sha256 mismatch")),
        manager_factory=lambda _path: manager,
        toolkit_factory=lambda **_kwargs: SimpleNamespace(),
    )

    with pytest.raises(InitializationError, match="payload sha256 mismatch"):
        initializer.initialize()

    assert manager.calls == 0
    assert list(project.iterdir()) == []


def test_host_toolchain_mode_resolves_every_managed_tool_without_provisioning(tmp_path: Path) -> None:
    from olf.toolchain.spec import MANAGED_TOOLS

    layout = _layout(tmp_path)
    manager = _Manager()
    resolved: list[str] = []
    docker = SimpleNamespace(
        resolve_current_engine_endpoint=lambda **_kwargs: "unix:///tmp/docker.sock",
        version=lambda **_kwargs: SimpleNamespace(stdout="Docker Engine 1.0\n"),
    )
    tools = SimpleNamespace(
        docker=docker,
        resolver=SimpleNamespace(resolve=lambda name: resolved.append(name) or Path("/usr/bin/tool")),
    )
    initializer = ProjectInitializer(
        layout_resolver=lambda _env: layout,
        manager_factory=lambda _path: manager,
        toolkit_factory=lambda **_kwargs: tools,
    )

    initializer.initialize(environ={"OLF_TOOLCHAIN_MODE": "host"})

    assert manager.calls == 0
    assert sorted(resolved) == sorted(MANAGED_TOOLS)


def test_init_rejects_an_unknown_toolchain_mode(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    with pytest.raises(InitializationError, match="OLF_TOOLCHAIN_MODE"):
        _initializer(layout).initialize(environ={"OLF_TOOLCHAIN_MODE": "bring-your-own"})

    assert not (layout.project_root / "lakehouse_code").exists()


def test_init_reports_toolchain_provisioning_failure_and_writes_nothing(tmp_path: Path) -> None:
    from olf.deployment.errors import ToolchainError

    layout = _layout(tmp_path)

    class _FailingManager:
        calls = 0

        def ensure_all(self) -> dict[str, Path]:
            raise ToolchainError("terraform", reason="checksum mismatch")

    initializer = ProjectInitializer(
        layout_resolver=lambda _env: layout,
        manager_factory=lambda _path: _FailingManager(),
        toolkit_factory=lambda **_kwargs: SimpleNamespace(),
    )

    with pytest.raises(InitializationError, match="checksum mismatch"):
        initializer.initialize(environ={"OLF_TOOLCHAIN_MODE": "managed"})

    assert list(layout.project_root.iterdir()) == []


def test_init_reports_a_missing_docker_cli(tmp_path: Path) -> None:
    from olf.deployment.errors import ExecutableNotFoundError

    layout = _layout(tmp_path)
    docker = SimpleNamespace(
        resolve_current_engine_endpoint=lambda **_kwargs: (_ for _ in ()).throw(
            ExecutableNotFoundError("docker")
        ),
    )
    tools = SimpleNamespace(docker=docker, resolver=SimpleNamespace(resolve=lambda _name: Path("/usr/bin/tool")))
    initializer = ProjectInitializer(
        layout_resolver=lambda _env: layout,
        manager_factory=lambda _path: _Manager(),
        toolkit_factory=lambda **_kwargs: tools,
    )

    with pytest.raises(InitializationError, match="docker"):
        initializer.initialize()

    assert list(layout.project_root.iterdir()) == []


def test_init_leaves_no_staging_directory_when_the_distribution_has_no_template(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    shutil.rmtree(layout.distribution_root / "lakehouse_code")

    with pytest.raises(InitializationError, match="missing demo template"):
        _initializer(layout).initialize()

    assert list(layout.project_root.iterdir()) == []


def test_empty_init_writes_only_the_canonical_package_skeleton(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    result = _initializer(layout).initialize(empty=True)

    assert (result.project_root / "openlakeforge.yaml").is_file()

    tree = sorted(str(p.relative_to(result.lakehouse_root)) for p in result.lakehouse_root.rglob("*"))
    assert tree == [
        "__init__.py",
        "bronze",
        "bronze/__init__.py",
        "dashboards",
        "dashboards/__init__.py",
        "dashboards/superset",
        "dashboards/superset/__init__.py",
        "definitions.py",
        "gold",
        "gold/__init__.py",
        "lakehouse.yaml",
        "pipelines",
        "pipelines/__init__.py",
        "pipelines/dagster",
        "pipelines/dagster/__init__.py",
        "silver",
        "silver/__init__.py",
    ]


def test_empty_init_is_not_deployable_until_it_has_a_product(tmp_path: Path) -> None:
    from olf.deployment.inspection import project_not_runnable_reason

    layout = _layout(tmp_path)
    result = _initializer(layout).initialize(empty=True)

    reason = project_not_runnable_reason(result.project_root)

    assert reason is not None
    assert "source.yaml" in reason


def test_cli_init_empty_prints_the_scaffold_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from olf.initialization import InitializationResult

    captured: dict[str, bool] = {}

    def _fake(*, empty: bool, **_kwargs: object) -> InitializationResult:
        captured["empty"] = empty
        return InitializationResult(
            project_root=tmp_path, lakehouse_root=tmp_path / "lakehouse_code", empty=empty
        )

    monkeypatch.setattr("olf.commands.init.initialize_project", _fake)

    invocation = runner.invoke(app, ["init", "--empty"])

    assert invocation.exit_code == 0, invocation.output
    assert captured["empty"] is True
    assert "olf source new" in invocation.output
    assert "olf product new" in invocation.output


def test_cli_init_reports_a_failure_as_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(**_kwargs: object) -> None:
        raise InitializationError("refusing to overwrite existing project path: ./lakehouse_code")

    monkeypatch.setattr("olf.commands.init.initialize_project", _fail)

    invocation = runner.invoke(app, ["init"])

    assert invocation.exit_code != 0
    assert "refusing to overwrite" in invocation.output
