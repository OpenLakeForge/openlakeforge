"""Provider-neutral, ownership-safe Phase 2 catalog reconciliation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from openlakeforge_domain import CatalogNamespace, inventory_for

MANAGED_BY_KEY = "openlakeforge.io/managed-by"
MANAGED_BY_VALUE = "openlakeforge"
CATALOG_KEY = "openlakeforge.io/catalog"
# ADR 0019, section 5. The role itself is created by the Polaris bootstrap, and only when people sign in.
GOLD_READ_ROLE = "olf-gold-read"
GOLD_READ_PRIVILEGES = ("TABLE_READ_DATA", "NAMESPACE_READ_PROPERTIES", "VIEW_READ_PROPERTIES")


class NamespaceSyncError(RuntimeError):
    """Raised when a descriptor conflicts with a foreign catalog namespace."""


@dataclass(frozen=True)
class NamespaceState:
    """Live namespace state, including enough ownership evidence to mutate it."""

    location: str
    properties: Mapping[str, str] = field(default_factory=dict)

    @property
    def managed(self) -> bool:
        return self.properties.get(MANAGED_BY_KEY) == MANAGED_BY_VALUE


class NamespaceClient(Protocol):
    def create_namespace(self, namespace: CatalogNamespace) -> None: ...

    def update_namespace_location(self, namespace: CatalogNamespace) -> None: ...

    def adopt_namespace(self, namespace: CatalogNamespace) -> None: ...

    def drop_namespace(self, name: str) -> None: ...


@dataclass(frozen=True)
class NamespaceSyncPlan:
    create: tuple[CatalogNamespace, ...] = ()
    adopt: tuple[CatalogNamespace, ...] = ()
    update: tuple[CatalogNamespace, ...] = ()
    delete: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    orphans: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.create or self.adopt or self.update or self.delete)


def _state(value: str | NamespaceState) -> NamespaceState:
    return value if isinstance(value, NamespaceState) else NamespaceState(location=value)


def plan_namespace_sync(
    existing: Mapping[str, str | NamespaceState],
    desired: Sequence[CatalogNamespace],
    *,
    prune: bool = False,
    namespace_prefix: str = "",
) -> NamespaceSyncPlan:
    """Plan reconciliation without ever taking ownership of a foreign object.

    A legacy namespace is adopted only when its descriptor-derived location
    already matches. A same-name namespace at another location is a collision,
    not drift: changing it could redirect someone else's data.

    `existing` reflects everything visible in the catalog, which on AWS Glue's
    shared default catalog includes every enabled stage's own namespaces too.
    A stage prefix scopes reconciliation so siblings cannot be reported or
    pruned. Before that scope is applied, matching unprefixed v0.2 namespace
    names stop the upgrade: creating their prefixed replacements would hide
    existing Iceberg table registrations behind an empty catalog boundary.
    """
    legacy_namespace_names = sorted(
        namespace.name.removeprefix(namespace_prefix)
        for namespace in desired
        if namespace_prefix and namespace.name.startswith(namespace_prefix)
        and namespace.name.removeprefix(namespace_prefix) in existing
    )
    if legacy_namespace_names:
        raise NamespaceSyncError(
            "Catalog contains unprefixed v0.2 Glue namespace(s) "
            f"{legacy_namespace_names!r}. Refusing to create parallel {namespace_prefix!r} namespaces because "
            "their Iceberg table registrations would be lost. Migrate the Glue metadata to the prefixed names "
            "before running artifacts, or create a fresh environment."
        )
    live = {
        name: _state(value)
        for name, value in existing.items()
        if not namespace_prefix or name.startswith(namespace_prefix)
    }
    canonical_silver = {namespace.name for namespace in desired if namespace.name.endswith("_silver")}
    noncanonical_silver = sorted(
        name
        for name in live
        if name.endswith("_silver") and name not in canonical_silver
    )
    if noncanonical_silver:
        raise NamespaceSyncError(
            "Catalog contains noncanonical legacy Silver namespace(s) "
            f"{noncanonical_silver!r}. This alpha ownership change is reset-only; "
            "destroy and recreate the environment instead of migrating namespaces in place."
        )
    create: list[CatalogNamespace] = []
    adopt: list[CatalogNamespace] = []
    update: list[CatalogNamespace] = []
    unchanged: list[str] = []
    for namespace in sorted(desired, key=lambda item: item.name):
        current = live.get(namespace.name)
        if current is None:
            create.append(namespace)
        elif not current.managed and current.location != namespace.location:
            raise NamespaceSyncError(
                f"Namespace {namespace.name!r} exists at {current.location!r} but is not managed by OpenLakeForge; "
                "refusing to change it."
            )
        elif not current.managed:
            adopt.append(namespace)
        elif current.location != namespace.location:
            update.append(namespace)
        else:
            unchanged.append(namespace.name)

    declared = {namespace.name for namespace in desired}
    managed_orphans = tuple(sorted(name for name, state in live.items() if state.managed and name not in declared))
    foreign = tuple(sorted(name for name, state in live.items() if not state.managed and name not in declared))
    return NamespaceSyncPlan(
        create=tuple(create), adopt=tuple(adopt), update=tuple(update),
        delete=managed_orphans if prune else (), unchanged=tuple(unchanged),
        orphans=managed_orphans, foreign=foreign,
    )


def desired_namespaces(
    repo_root: Path, *, bronze_bucket: str, silver_bucket: str, gold_bucket: str, namespace_prefix: str = ""
) -> tuple[CatalogNamespace, ...]:
    physical = inventory_for(repo_root).resolve_physical_names(
        catalog_database_fqn="",
        bronze_bucket=bronze_bucket,
        silver_bucket=silver_bucket,
        gold_bucket=gold_bucket,
        namespace_prefix=namespace_prefix,
    )
    return physical.catalog_namespaces


def desired_gold_namespaces(repo_root: Path, *, namespace_prefix: str = "") -> tuple[str, ...]:
    """Physical Gold namespace names, from the same inventory as `desired_namespaces`."""
    physical = inventory_for(repo_root).resolve_physical_names(
        catalog_database_fqn="", bronze_bucket="", silver_bucket="", gold_bucket="", namespace_prefix=namespace_prefix
    )
    return tuple(sorted(product.gold_namespace for product in physical.products))


def plan_gold_grants(held: frozenset[tuple[str, str]], gold_namespaces: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """The (namespace, privilege) grants `GOLD_READ_ROLE` lacks.

    Polaris answers a repeated grant with HTTP 500, not 409, so only what is
    missing may be written.
    """
    return tuple(
        (namespace, privilege)
        for namespace in sorted(gold_namespaces)
        for privilege in GOLD_READ_PRIVILEGES
        if (namespace, privilege) not in held
    )


def apply_namespace_sync(client: NamespaceClient, plan: NamespaceSyncPlan) -> None:
    for namespace in plan.create:
        client.create_namespace(namespace)
    for namespace in plan.adopt:
        client.adopt_namespace(namespace)
    for namespace in plan.update:
        client.update_namespace_location(namespace)
    for name in plan.delete:
        client.drop_namespace(name)


def render_plan(plan: NamespaceSyncPlan, *, prune: bool) -> str:
    lines: list[str] = []
    for namespace in plan.create:
        lines.append(f"  + create {namespace.name} -> {namespace.location}")
    for namespace in plan.adopt:
        lines.append(f"  ~ adopt legacy {namespace.name}")
    for namespace in plan.update:
        lines.append(f"  ~ update default location {namespace.name} -> {namespace.location}")
    for name in plan.delete:
        lines.append(f"  - remove metadata {name} (object-store files retained)")
    for name in plan.unchanged:
        lines.append(f"    unchanged {name}")
    for name in plan.orphans:
        if not prune:
            lines.append(f"    undeclared managed {name} (kept; rerun with --prune to remove metadata)")
    for name in plan.foreign:
        lines.append(f"    foreign {name} (never managed or pruned)")
    return "\n".join(lines)
