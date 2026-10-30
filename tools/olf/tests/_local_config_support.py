"""Catalog and published-image fixtures shared by the common and infra tests of the local configuration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from olf.common.deployment.context import DeploymentContext
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.profile import Preset, ProjectSpec, legacy_single_stage_topology
from olf.common.profile import Provider as ProfileProvider


def _write_full_catalog(repo_root: Path, *, overrides: dict[str, str] | None = None) -> Path:
    """A catalog covering every chart the local provider might resolve - a
    real `component-catalog.yaml` always declares all of them, so a partial
    fixture would make `CatalogChart.load` raise for any chart beyond the
    one(s) a given test focuses on."""
    from olf.common.chart_pins import CHART_DEFAULTS

    overrides = overrides or {}
    lines = ["components:", "  helm:", "    charts:"]
    for index, (name, default) in enumerate(CHART_DEFAULTS.items()):
        sha256 = overrides.get(name, f"{index:0>2}" * 32)
        lines.extend(
            [
                f"      {name}:",
                f"        repository: {default.repository}",
                f"        reference: {default.chart_ref}",
                f"        version: {default.version}",
                f'        sha256: "{sha256}"',
            ]
        )
    catalog_path = repo_root / "release/component-catalog.yaml"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text("\n".join(lines) + "\n")
    return catalog_path


_PIN = "ghcr.io/openlakeforge/openlakeforge/project-code:0.5.0-alpha.1@sha256:" + "a" * 64


def _published_config(
    tmp_path: Path,
    environ: dict[str, str] | None = None,
    *,
    project: ProjectSpec | None = None,
    project_root: Path | None = None,
):
    catalog = _write_full_catalog(tmp_path)
    catalog.write_text(catalog.read_text() + f"  images:\n    project_code: {_PIN}\n")
    topology = replace(
        legacy_single_stage_topology(provider=ProfileProvider.LOCAL, preset=Preset.FULL), project=project
    )
    context = DeploymentContext.local(repo_root=project_root or tmp_path, distribution_root=tmp_path, topology=topology)
    return LocalDeploymentConfig.from_environment(environ or {}, context=context)
