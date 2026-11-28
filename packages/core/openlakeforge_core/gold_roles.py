"""The role of each Gold model: fact, dimension or mart.

A model's role comes from `meta.openlakeforge.role` in its `schema.yml` entry (on the model or under its `config`),
otherwise from its name prefix. Like `product_dependencies` it reads files as text, so `olf check contracts` and the
metadata deploy need no dbt run and no manifest. A role set in a model's own `config(meta=...)` is not read.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import yaml

__all__ = ["GOLD_ROLES", "GOLD_ROLE_CLASSIFICATION", "gold_role_tag", "scan_gold_roles"]

GOLD_ROLE_CLASSIFICATION = "GoldRole"
GOLD_ROLES = {"fact": "Fact", "dimension": "Dimension", "mart": "Mart"}
_PREFIXES = {"fct_": "Fact", "dim_": "Dimension", "mart_": "Mart"}


def gold_role_tag(role: str) -> str:
    return f"{GOLD_ROLE_CLASSIFICATION}.{role}"


def _declared_roles(path: Path) -> dict[str, Any]:
    """`model name -> declared role value` for each model of a properties file that sets one."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    found: dict[str, Any] = {}
    for model in document.get("models") or () if isinstance(document, Mapping) else ():
        if not isinstance(model, Mapping) or "name" not in model:
            continue
        config = model.get("config")
        for meta in (model.get("meta"), config.get("meta") if isinstance(config, Mapping) else None):
            olf = meta.get("openlakeforge") if isinstance(meta, Mapping) else None
            if isinstance(olf, Mapping) and "role" in olf:
                found[str(model["name"])] = olf["role"]
    return found


def scan_gold_roles(gold_root: Path, products: Collection[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Each product's models with their role, and the models whose role cannot be resolved.

    Returns `({product: {model: role}}, problems)`. A model with no declared role and no known prefix is left out of
    the mapping and named in `problems`, as is one whose declared role is not Fact, Dimension or Mart.
    """
    roles: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for product in sorted(products):
        folder = gold_root / "models" / product
        if not folder.is_dir():
            continue
        declared: dict[str, Any] = {}
        for path in sorted([*folder.rglob("*.yml"), *folder.rglob("*.yaml")]):
            declared.update(_declared_roles(path))
        resolved: dict[str, str] = {}
        for name in sorted({path.stem for path in folder.rglob("*.sql")}):
            if name in declared:
                role = GOLD_ROLES.get(str(declared[name]).lower())
                if role is None:
                    problems.append(
                        f"{product}.{name}: meta.openlakeforge.role {declared[name]!r} is not one of "
                        f"{', '.join(GOLD_ROLES.values())}"
                    )
                    continue
            else:
                role = next((r for prefix, r in _PREFIXES.items() if name.startswith(prefix)), None)
                if role is None:
                    problems.append(
                        f"{product}.{name}: no role; name it fct_, dim_ or mart_, or set "
                        "meta.openlakeforge.role in its schema.yml entry"
                    )
                    continue
            resolved[name] = role
        roles[product] = resolved
    return roles, problems
