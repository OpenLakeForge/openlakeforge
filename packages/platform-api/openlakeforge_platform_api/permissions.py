"""The permission matrix: operation, lowest role allowed, and the stages it applies to."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from types import MappingProxyType
from typing import Any

ANONYMOUS = "anonymous"
EVERY_STAGE = "*"
REQUIRED_KEYS = frozenset({"role", "stages"})
ALLOWED_KEYS = REQUIRED_KEYS | {"changes_state", "role_stages", "services"}
# The service principals a route may name (ADR 0021, decision 3). They are not canonical roles and sit in no
# precedence: a row lists each one that may call it, and a service not listed is refused whatever its identity.
SERVICE_CALLERS = frozenset({"alert-evaluator"})


class PermissionMatrixError(ValueError):
    """The matrix names a role outside precedence or is malformed; the API refuses to start."""


@dataclass(frozen=True)
class Permission:
    role: str
    stages: tuple[str, ...]
    # An operation that changes state is audited (ADR 0021, decision 3): it runs only once a durable record of the
    # attempt is stored, and a refusal of an authenticated person is stored too.
    changes_state: bool = False
    # A role listed here is limited to these stages instead of `stages`, which every other role at or above the floor
    # keeps. Lets one row give a lower role less than a higher one, e.g. data-engineer on dev only.
    role_stages: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    # The service principals allowed in addition to the roles at or above the floor, on the row's `stages`.
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionMatrix:
    operations: Mapping[str, Permission]
    precedence: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], precedence: tuple[str, ...]) -> PermissionMatrix:
        operations: dict[str, Permission] = {}
        for name, row in (data.get("operations") or {}).items():
            if not isinstance(row, Mapping) or not REQUIRED_KEYS <= set(row) <= ALLOWED_KEYS:
                raise PermissionMatrixError(
                    f"operation {name!r} sets keys other than role, stages, changes_state and role_stages"
                )
            role, stages, changes_state = row["role"], row["stages"], row.get("changes_state", False)
            if role != ANONYMOUS and role not in precedence:
                raise PermissionMatrixError(f"operation {name!r} names role {role!r}, not in {list(precedence)}")
            if not isinstance(stages, list) or not all(isinstance(stage, str) and stage for stage in stages):
                raise PermissionMatrixError(f"operation {name!r}: stages must be a list of stage names")
            if role == ANONYMOUS and stages:
                raise PermissionMatrixError(f"operation {name!r}: an anonymous operation cannot reach a stage")
            if not isinstance(changes_state, bool):
                raise PermissionMatrixError(f"operation {name!r}: changes_state must be true or false")
            if role == ANONYMOUS and changes_state:
                raise PermissionMatrixError(f"operation {name!r}: an anonymous operation cannot change state")
            role_stages = cls._role_stages(str(name), row.get("role_stages", {}), role, stages, precedence)
            services = row.get("services", [])
            if not isinstance(services, list) or not set(services) <= SERVICE_CALLERS:
                raise PermissionMatrixError(
                    f"operation {name!r}: services must list names from {sorted(SERVICE_CALLERS)}"
                )
            if services and (role == ANONYMOUS or changes_state):
                raise PermissionMatrixError(f"operation {name!r}: a service may only call an authenticated read")
            operations[str(name)] = Permission(
                role=role,
                stages=tuple(stages),
                changes_state=changes_state,
                role_stages=role_stages,
                services=tuple(services),
            )
        return cls(operations=operations, precedence=precedence)

    @staticmethod
    def _role_stages(
        name: str, raw: Any, floor: str, stages: list[str], precedence: tuple[str, ...]
    ) -> Mapping[str, tuple[str, ...]]:
        if not isinstance(raw, Mapping):
            raise PermissionMatrixError(f"operation {name!r}: role_stages must be a table of role to stage list")
        if raw and not stages:
            raise PermissionMatrixError(f"operation {name!r}: role_stages needs an operation that names a stage")
        limits: dict[str, tuple[str, ...]] = {}
        for limited, allowed in raw.items():
            if limited not in precedence:
                raise PermissionMatrixError(
                    f"operation {name!r}: role_stages names {limited!r}, not in {list(precedence)}"
                )
            if precedence.index(limited) > precedence.index(floor):
                raise PermissionMatrixError(
                    f"operation {name!r}: role_stages names {limited!r}, below the {floor!r} floor"
                )
            if not isinstance(allowed, list) or not allowed or not all(isinstance(s, str) and s for s in allowed):
                raise PermissionMatrixError(
                    f"operation {name!r}: role_stages for {limited!r} must be a non-empty list of stage names"
                )
            limits[str(limited)] = tuple(allowed)
        return MappingProxyType(limits)

    @classmethod
    def packaged(cls, precedence: tuple[str, ...]) -> PermissionMatrix:
        text = resources.files(__package__).joinpath("permissions.toml").read_text(encoding="utf-8")
        return cls.from_mapping(tomllib.loads(text), precedence)

    def allows(self, operation: str, role: str | None, stage: str | None = None, *, service: str | None = None) -> bool:
        """Whether `role`, or the service principal `service`, may perform `operation`, on `stage` when it names one.

        An operation with no row is refused: the matrix fails closed. A service is allowed only where the row names
        it; it holds no role, so the role floor never applies to it.
        """
        permission = self.operations.get(operation)
        if permission is None:
            return False
        if stage is None:
            if permission.stages:
                return False
        elif EVERY_STAGE not in permission.stages and stage not in permission.stages:
            return False
        if service is not None:
            return service in permission.services
        if permission.role == ANONYMOUS:
            return True
        if role is None or role not in self.precedence:
            return False
        if self.precedence.index(role) > self.precedence.index(permission.role):
            return False
        limit = permission.role_stages.get(role)
        return limit is None or stage is None or EVERY_STAGE in limit or stage in limit
