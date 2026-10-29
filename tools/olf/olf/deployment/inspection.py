"""Read-only deployment inspection helpers used by ``olf doctor``.

The deployment engine owns preflight checks rather than leaving them in Make
or shell wrappers. Managed-toolchain awareness (#127) reports which tools
are provisioned from `release/component-catalog.yaml` versus resolved from
`PATH`, and provisions on first use - `olf doctor` is the documented way to
pre-warm a clean machine's toolchain before `olf deploy`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from openlakeforge_domain import LakehouseDescriptorError, load_lakehouse_inventory

from olf.deployment.errors import DeploymentError, ExecutableNotFoundError, ToolchainError
from olf.tooling.resolver import ManagedExecutableResolver

if TYPE_CHECKING:
    from olf.tooling.toolkit import Toolkit


@dataclass(frozen=True)
class DoctorItem:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    items: tuple[DoctorItem, ...]

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.items)

    def render(self) -> str:
        lines = ["OpenLakeForge deployment preflight"]
        for item in self.items:
            status = "PASS" if item.ok else "FAIL"
            lines.append(f"{status:4} {item.name}: {item.detail}")
        return "\n".join(lines)


def _toolchain_mode_item(tools: Toolkit) -> DoctorItem:
    resolver = tools.resolver
    if not isinstance(resolver, ManagedExecutableResolver):
        return DoctorItem("toolchain mode", True, "host (OLF_TOOLCHAIN_MODE=host; PATH resolution only)")
    manager = resolver.manager
    detail = f"managed; distribution {manager.distribution_version}; platform {manager.platform}; home {manager.home}"
    return DoctorItem("toolchain mode", True, detail)


def project_not_runnable_reason(project_root: Path) -> str | None:
    """Return why `project_root` cannot run a dynamic-artifact deploy, or None.

    A project is runnable once its descriptors load strictly: an
    `olf init --empty` project only becomes runnable after `olf source new`
    and `olf product new`. Unreadable or malformed descriptors are reported
    the same way rather than raised, because both callers -- `olf doctor` and
    the deploy precondition -- exist to explain the problem."""
    try:
        load_lakehouse_inventory(project_root)
    except (LakehouseDescriptorError, yaml.YAMLError, OSError) as exc:
        return str(exc)
    return None


def base_report(
    *,
    project_root: Path,
    distribution_root: Path,
    tools: Toolkit,
    required_tools: Iterable[str],
) -> list[DoctorItem]:
    reason = project_not_runnable_reason(project_root)
    project_item = (
        DoctorItem("project", True, str(project_root))
        if reason is None
        else DoctorItem("project", False, f"{project_root}: not runnable: {reason}")
    )
    items = [
        project_item,
        DoctorItem(
            "platform distribution",
            (distribution_root / "release/component-catalog.yaml").is_file(),
            str(distribution_root),
        ),
        DoctorItem(
            "terraform roots",
            (distribution_root / "infra/terraform").is_dir(),
            str(distribution_root / "infra/terraform"),
        ),
        _toolchain_mode_item(tools),
    ]
    resolver = tools.resolver
    managed_names: frozenset[str] = frozenset()
    if isinstance(resolver, ManagedExecutableResolver):
        from olf.toolchain.spec import MANAGED_TOOLS

        managed_names = frozenset(MANAGED_TOOLS)
    for name in required_tools:
        try:
            path = resolver.resolve(name)
        except ExecutableNotFoundError:
            items.append(DoctorItem(name, False, "not found on PATH"))
        except ToolchainError as exc:
            items.append(DoctorItem(name, False, str(exc)))
        else:
            source = "managed" if name in managed_names else "PATH"
            items.append(DoctorItem(name, True, f"{path} ({source})"))
    return items


def docker_health(tools: Toolkit, *, env: Mapping[str, str]) -> DoctorItem:
    try:
        version = tools.docker.version(env=env).stdout.strip().splitlines()[0]
    except DeploymentError as exc:
        return DoctorItem("docker engine", False, str(exc))
    return DoctorItem("docker engine", True, version or "reachable")
