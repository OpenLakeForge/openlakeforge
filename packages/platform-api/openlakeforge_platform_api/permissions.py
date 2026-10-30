"""The permission matrix: operation, lowest role allowed, and the stages it applies to."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

ANONYMOUS = "anonymous"
EVERY_STAGE = "*"


class PermissionMatrixError(ValueError):
    """The matrix names a role outside precedence or is malformed; the API refuses to start."""


@dataclass(frozen=True)
class Permission:
    role: str
    stages: tuple[str, ...]


@dataclass(frozen=True)
class PermissionMatrix:
    operations: Mapping[str, Permission]
    precedence: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], precedence: tuple[str, ...]) -> PermissionMatrix:
        operations: dict[str, Permission] = {}
        for name, row in (data.get("operations") or {}).items():
            if not isinstance(row, Mapping) or set(row) != {"role", "stages"}:
                raise PermissionMatrixError(f"operation {name!r} must set exactly role and stages")
            role, stages = row["role"], row["stages"]
            if role != ANONYMOUS and role not in precedence:
                raise PermissionMatrixError(f"operation {name!r} names role {role!r}, not in {list(precedence)}")
            if not isinstance(stages, list) or not all(isinstance(stage, str) and stage for stage in stages):
                raise PermissionMatrixError(f"operation {name!r}: stages must be a list of stage names")
            if role == ANONYMOUS and stages:
                raise PermissionMatrixError(f"operation {name!r}: an anonymous operation cannot reach a stage")
            operations[str(name)] = Permission(role=role, stages=tuple(stages))
        return cls(operations=operations, precedence=precedence)

    @classmethod
    def packaged(cls, precedence: tuple[str, ...]) -> PermissionMatrix:
        text = resources.files(__package__).joinpath("permissions.toml").read_text(encoding="utf-8")
        return cls.from_mapping(tomllib.loads(text), precedence)

    def allows(self, operation: str, role: str | None, stage: str | None = None) -> bool:
        """Whether `role` may perform `operation`, on `stage` when the request names one.

        An operation with no row is refused: the matrix fails closed.
        """
        permission = self.operations.get(operation)
        if permission is None:
            return False
        if stage is None:
            if permission.stages:
                return False
        elif EVERY_STAGE not in permission.stages and stage not in permission.stages:
            return False
        if permission.role == ANONYMOUS:
            return True
        if role is None or role not in self.precedence:
            return False
        return self.precedence.index(role) <= self.precedence.index(permission.role)
