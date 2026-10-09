"""Typed contract for a writable OpenLakeForge data project."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from openlakeforge_domain import LakehouseDescriptorError, LakehouseInventory, load_lakehouse_inventory

if TYPE_CHECKING:
    from olf.distribution import RuntimeLayout


@dataclass(frozen=True)
class ProjectSpec:
    """The writable project and immutable distribution selected for one run."""

    root: Path
    distribution_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(self, "distribution_root", self.distribution_root.resolve())

    @classmethod
    def from_layout(cls, layout: RuntimeLayout) -> ProjectSpec:
        return cls(root=layout.project_root, distribution_root=layout.distribution_root)

    @property
    def profile_path(self) -> Path:
        return self.root / "openlakeforge.yaml"

    @property
    def code_root(self) -> Path:
        return self.root / "lakehouse_code"

    @property
    def lakehouse_path(self) -> Path:
        return self.code_root / "lakehouse.yaml"

    @property
    def bronze_root(self) -> Path:
        return self.code_root / "bronze"

    @property
    def silver_root(self) -> Path:
        return self.code_root / "silver"

    @property
    def gold_root(self) -> Path:
        return self.code_root / "gold"

    @property
    def dashboards_root(self) -> Path:
        return self.code_root / "dashboards" / "superset"

    @property
    def pipelines_root(self) -> Path:
        return self.code_root / "pipelines" / "dagster"

    @property
    def schema_root(self) -> Path:
        return self.distribution_root / "docs" / "schema"


@dataclass(frozen=True)
class ProjectCheck:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class ProjectValidationReport:
    project: ProjectSpec
    checks: tuple[ProjectCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def render_json(self) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "valid": self.ok,
                "project_root": str(self.project.root),
                "distribution_root": str(self.project.distribution_root),
                "checks": [check.as_dict() for check in self.checks],
            },
            sort_keys=True,
        )


def validate_project(project: ProjectSpec) -> ProjectValidationReport:
    """Validate the provider-neutral project files. Parses the Deployment
    Profile's structure but does not resolve it into a topology -- that is
    `olf profile resolve`."""
    from olf.contracts_check import descriptor_schema_errors

    checks = [_canonical_layout_check(project), _profile_check(project)]
    descriptor_errors = descriptor_schema_errors(project.root, schema_root=project.schema_root)
    checks.append(
        ProjectCheck(
            "descriptors",
            not descriptor_errors,
            "descriptors validated" if not descriptor_errors else "; ".join(descriptor_errors),
        )
    )

    inventory: LakehouseInventory | None = None
    try:
        inventory = load_lakehouse_inventory(project.root)
    except (LakehouseDescriptorError, OSError) as exc:
        checks.append(ProjectCheck("inventory", False, str(exc)))
    else:
        checks.append(ProjectCheck("inventory", True, f"{len(inventory.products)} product(s) discovered"))

    checks.append(_project_assets_check(project, inventory))
    return ProjectValidationReport(project=project, checks=tuple(checks))


def _canonical_layout_check(project: ProjectSpec) -> ProjectCheck:
    required_files = {
        "openlakeforge.yaml": project.profile_path,
        "lakehouse_code/lakehouse.yaml": project.lakehouse_path,
    }
    required_directories = {
        "lakehouse_code/": project.code_root,
        "lakehouse_code/bronze/": project.bronze_root,
        "lakehouse_code/silver/": project.silver_root,
        "lakehouse_code/gold/": project.gold_root,
        "lakehouse_code/dashboards/superset/": project.dashboards_root,
        "lakehouse_code/pipelines/dagster/": project.pipelines_root,
    }
    missing = [name for name, path in required_files.items() if not path.is_file()]
    missing.extend(name for name, path in required_directories.items() if not path.is_dir())
    if missing:
        return ProjectCheck("canonical_layout", False, f"missing: {', '.join(missing)}")
    return ProjectCheck("canonical_layout", True, "canonical project paths are present")


def _profile_check(project: ProjectSpec) -> ProjectCheck:
    from olf.profile import DeploymentProfileError, load_deployment_profile

    try:
        load_deployment_profile(project.profile_path, distribution_root=project.distribution_root)
    except (DeploymentProfileError, OSError) as exc:
        return ProjectCheck("profile", False, str(exc))
    return ProjectCheck("profile", True, "deployment profile validated")


def _project_assets_check(project: ProjectSpec, inventory: LakehouseInventory | None) -> ProjectCheck:
    if inventory is None:
        return ProjectCheck("project_assets", False, "not evaluated because inventory validation failed")

    missing: list[str] = []
    for source in inventory.sources:
        path = project.bronze_root / source.name / "dlt" / f"{source.name}.py"
        if not path.is_file():
            missing.append(path.relative_to(project.root).as_posix())
    for domain in inventory.domains:
        path = project.silver_root / domain.name / "contracts" / "floe" / f"{domain.name}.yml"
        if not path.is_file():
            missing.append(path.relative_to(project.root).as_posix())
    for product in inventory.products:
        dbt_path = project.gold_root / product.id / "dbt" / "dbt_project.yml"
        pipeline_path = project.pipelines_root / f"{product.id}.py"
        for path in (dbt_path, pipeline_path):
            if not path.is_file():
                missing.append(path.relative_to(project.root).as_posix())
    for dashboard in inventory.dashboards:
        path = project.root / dashboard.report_source_dir / "metadata.yaml"
        if not path.is_file():
            missing.append(path.relative_to(project.root).as_posix())

    if missing:
        return ProjectCheck("project_assets", False, f"missing: {', '.join(missing)}")
    return ProjectCheck("project_assets", True, "declared project assets are present")
