from __future__ import annotations

from pathlib import Path

from olf.deployment.context import DeploymentContext
from olf.deployment.project_code import project_code_build_context


def test_custom_project_context_uses_platform_assets_and_selected_project(tmp_path: Path) -> None:
    distribution = tmp_path / "distribution"
    project = tmp_path / "project"
    (distribution / "images/project-code").mkdir(parents=True)
    (distribution / "images/project-code/pyproject.toml").write_text("name = 'platform'\n")
    (distribution / "packages/domain-model").mkdir(parents=True)
    (distribution / "packages/domain-model/pyproject.toml").write_text("name = 'domain'\n")
    (distribution / "libs").mkdir()
    (distribution / "libs/platform.py").write_text("PLATFORM = True\n")
    (distribution / "lakehouse_code").mkdir()
    (distribution / "lakehouse_code/demo.py").write_text("DEMO = True\n")
    (project / "lakehouse_code").mkdir(parents=True)
    (project / "lakehouse_code/custom.py").write_text("CUSTOM = True\n")

    context = DeploymentContext.local(
        repo_root=project,
        distribution_root=distribution,
        state_root=tmp_path / "state",
        work_root=tmp_path / "work",
        cache_root=tmp_path / "cache",
    )

    with project_code_build_context(context.paths) as build_context:
        assert (build_context / "images/project-code/pyproject.toml").is_file()
        assert (build_context / "packages/domain-model/pyproject.toml").is_file()
        assert (build_context / "libs/platform.py").is_file()
        assert (build_context / "lakehouse_code/custom.py").is_file()
        assert not (build_context / "lakehouse_code/demo.py").exists()
