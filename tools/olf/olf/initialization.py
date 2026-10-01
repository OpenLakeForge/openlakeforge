"""Create a writable OpenLakeForge project from a verified distribution."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentError, ExecutableNotFoundError
from olf.deployment.inspection import docker_health
from olf.distribution import DistributionError, RuntimeLayout, runtime_layout
from olf.profile import (
    PROFILE_API_VERSION,
    PROFILE_KIND,
    DeploymentProfileError,
    Preset,
    StageName,
    validate_deployment_profile,
)
from olf.toolchain.manager import ToolchainManager
from olf.toolchain.spec import MANAGED_TOOLS

_EMPTY_LAKEHOUSE_YAML = """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: openlakeforge
displayName: OpenLakeForge
description: Empty OpenLakeForge project. Add a source and product before deployment.
status: planned
sources: []
domains: []
dashboards: []
"""

_EMPTY_FILES = (
    "__init__.py",
    "bronze/__init__.py",
    "silver/__init__.py",
    "gold/__init__.py",
    "dashboards/__init__.py",
    "dashboards/superset/__init__.py",
    "pipelines/__init__.py",
    "pipelines/dagster/__init__.py",
)


_PROFILE_HEADER = """# Deployment Profile (ADR 0011): the stages this project deploys and their
# capabilities. For AWS, set provider.type: aws and provider.region; for Azure,
# set provider.type: azure (its region comes from the foundation tfvars,
# AZURE_TFVARS_FILE).
# Check it with: olf profile validate --project .
"""


class InitializationError(RuntimeError):
    """`olf init` could not safely create a project."""


def default_profile_name(project_root: Path) -> str:
    """The project directory's name as a profile name, which must be a valid
    Kubernetes label value (it labels every namespace the deployment owns)."""
    name = re.sub(r"[^a-z0-9-]+", "-", project_root.name.lower())
    name = re.sub(r"-{2,}", "-", name).lstrip("-0123456789")[:63].rstrip("-")
    return name or "openlakeforge"


def render_profile(*, name: str, stages: Sequence[StageName], preset: Preset) -> str:
    """A local Deployment Profile enabling `stages`, validated by the same
    parser `olf profile validate` uses before anything is written."""
    document = {
        "apiVersion": PROFILE_API_VERSION,
        "kind": PROFILE_KIND,
        "metadata": {"name": name},
        "spec": {
            "provider": {"type": "local"},
            "preset": preset.value,
            "stages": {stage.value: {"enabled": True} for stage in StageName if stage in stages},
        },
    }
    try:
        validate_deployment_profile(document)
    except DeploymentProfileError as exc:
        raise InitializationError(str(exc)) from exc
    return _PROFILE_HEADER + yaml.safe_dump(document, sort_keys=False)


@dataclass(frozen=True)
class InitializationResult:
    project_root: Path
    lakehouse_root: Path
    empty: bool
    profile_only: bool = False

    @property
    def next_command(self) -> str:
        if not self.empty:
            return "olf platform apply -f openlakeforge.yaml"
        return (
            "olf source new <source> --resource <resource>, then olf product new "
            "<domain>/<product> --input <source>/<resource> --gold-table <table>"
        )


@dataclass
class ProjectInitializer:
    """Orchestrate the small, testable set of side effects behind `olf init`."""

    layout_resolver: Callable[[dict[str, str]], RuntimeLayout] = runtime_layout
    manager_factory: Callable[[Path], ToolchainManager] = ToolchainManager.from_catalog_path
    toolkit_factory: Callable[..., Toolkit] = Toolkit.default

    def initialize(
        self,
        *,
        empty: bool = False,
        stages: Sequence[StageName] = (StageName.DEV,),
        preset: Preset = Preset.SLIM,
        name: str = "",
        profile_only: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> InitializationResult:
        env = dict(environ if environ is not None else os.environ)
        try:
            layout = self.layout_resolver(env)
        except DistributionError as exc:
            raise InitializationError(str(exc)) from exc

        target = layout.project_root / "lakehouse_code"
        profile_target = layout.project_root / "openlakeforge.yaml"
        profile = render_profile(name=name or default_profile_name(layout.project_root), stages=stages, preset=preset)
        for path in (profile_target,) if profile_only else (profile_target, target):
            if path.exists():
                raise InitializationError(f"refusing to overwrite existing project path: {path}")
        if not layout.project_root.is_dir():
            raise InitializationError(f"project root is not a directory: {layout.project_root}")

        if profile_only:
            # An existing project (a 0.2 one, say) gains only its profile: no
            # code is copied, so neither the toolchain nor Docker is needed.
            if not (target / "lakehouse.yaml").is_file():
                raise InitializationError(
                    f"--profile-only needs an existing project: {target / 'lakehouse.yaml'} not found"
                )
            _write_profile(profile_target, profile)
            return InitializationResult(
                project_root=layout.project_root, lakehouse_root=target, empty=False, profile_only=True
            )
        tools = self._prepare_toolchain(layout, env)
        self._verify_docker(tools, env)
        self._create_project(layout, target, profile_target, profile, empty=empty)
        return InitializationResult(project_root=layout.project_root, lakehouse_root=target, empty=empty)

    def _prepare_toolchain(self, layout: RuntimeLayout, env: Mapping[str, str]) -> Toolkit:
        mode = env.get("OLF_TOOLCHAIN_MODE", "managed")
        if mode not in {"managed", "host"}:
            raise InitializationError("OLF_TOOLCHAIN_MODE must be 'managed' or 'host'")
        try:
            if mode == "managed":
                self.manager_factory(layout.catalog_path).ensure_all()
            tools = self.toolkit_factory(environ=env)
            if mode == "host":
                for name in MANAGED_TOOLS:
                    tools.resolver.resolve(name)
            return tools
        except (DeploymentError, OSError, ValueError) as exc:
            raise InitializationError(str(exc)) from exc

    def _verify_docker(self, tools: Toolkit, env: Mapping[str, str]) -> None:
        try:
            docker_env = dict(env)
            if not docker_env.get("DOCKER_HOST"):
                endpoint = tools.docker.resolve_current_engine_endpoint(env=docker_env)
                if endpoint:
                    docker_env["DOCKER_HOST"] = endpoint
            health = docker_health(tools, env=docker_env)
        except (DeploymentError, ExecutableNotFoundError) as exc:
            raise InitializationError(str(exc)) from exc
        if not health.ok:
            raise InitializationError(f"Docker engine is not reachable: {health.detail}")

    def _create_project(
        self, layout: RuntimeLayout, target: Path, profile_target: Path, profile: str, *, empty: bool
    ) -> None:
        staging_parent = Path(tempfile.mkdtemp(prefix=".olf-init-", dir=layout.project_root))
        staging = staging_parent / "lakehouse_code"
        profile_staging = staging_parent / "openlakeforge.yaml"
        try:
            if empty:
                self._write_empty_project(layout.distribution_root, staging)
            else:
                template = layout.distribution_root / "lakehouse_code"
                if not template.is_dir():
                    raise InitializationError(f"distribution is missing demo template: {template}")
                shutil.copytree(template, staging, ignore=shutil.ignore_patterns("__pycache__"))
            profile_staging.write_text(profile, encoding="utf-8")
            self._make_user_writable(staging_parent)
            if target.exists() or profile_target.exists():
                raise InitializationError(f"refusing to overwrite an existing project path under {layout.project_root}")
            os.replace(profile_staging, profile_target)
            try:
                os.replace(staging, target)
            except OSError:
                profile_target.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise InitializationError(f"could not create project at {target}: {exc}") from exc
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    def _write_empty_project(self, distribution_root: Path, target: Path) -> None:
        definitions = distribution_root / "lakehouse_code" / "definitions.py"
        if not definitions.is_file():
            raise InitializationError(f"distribution is missing base definitions module: {definitions}")
        target.mkdir(parents=True)
        for relative in _EMPTY_FILES:
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        (target / "definitions.py").write_text(definitions.read_text(encoding="utf-8"), encoding="utf-8")
        (target / "lakehouse.yaml").write_text(_EMPTY_LAKEHOUSE_YAML, encoding="utf-8")

    def _make_user_writable(self, root: Path) -> None:
        """Hand the copied tree to the user.

        The packaged payload is deliberately read-only, so a plain copy would
        hand the user files they cannot edit. Walking top-down and widening
        each directory before descending keeps the walk itself possible on a
        payload whose directories are not user-traversable."""
        _widen(root, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                _widen(Path(dirpath, name), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            for name in filenames:
                _widen(Path(dirpath, name), stat.S_IRUSR | stat.S_IWUSR)


def _write_profile(profile_target: Path, profile: str) -> None:
    """Create the profile, never overwriting one; a failed write leaves nothing."""
    try:
        with profile_target.open("x", encoding="utf-8") as handle:
            handle.write(profile)
    except FileExistsError as exc:
        raise InitializationError(f"refusing to overwrite existing project path: {profile_target}") from exc
    except OSError as exc:
        profile_target.unlink(missing_ok=True)
        raise InitializationError(f"could not write {profile_target}: {exc}") from exc


def _widen(path: Path, bits: int) -> None:
    path.chmod(path.stat().st_mode | bits)


def initialize_project(
    *,
    empty: bool = False,
    stages: Sequence[StageName] = (StageName.DEV,),
    preset: Preset = Preset.SLIM,
    name: str = "",
    profile_only: bool = False,
) -> InitializationResult:
    """Create the current directory's project, or only its profile."""
    return ProjectInitializer().initialize(
        empty=empty, stages=stages, preset=preset, name=name, profile_only=profile_only
    )
