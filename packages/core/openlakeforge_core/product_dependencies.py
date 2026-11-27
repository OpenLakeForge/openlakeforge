"""Which products read which other products' Gold models through dbt `ref()`.

The scan is static (SQL text and `schema.yml`), so `olf check contracts` and the inventory need no dbt run and no
manifest. dbt itself still refuses a `ref()` to a private model of another group when it parses; this reports the
same rule earlier and names the product edge, which dbt does not.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

import yaml

__all__ = ["ProductDependencies", "ProductDependency", "scan_product_dependencies"]

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
# `ref('model')`, `ref("model")` and `ref('package', 'model')`: the model is the last argument.
_REF = re.compile(r"""\bref\(\s*(?:['"][^'"]+['"]\s*,\s*)?['"]([^'"]+)['"]\s*(?:,\s*v(?:ersion)?\s*=[^)]*)?\)""")


@dataclass(frozen=True)
class ProductDependency:
    """`consumer` reads `models` of `producer` through `ref()`."""

    consumer: str
    producer: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class ProductDependencies:
    dependencies: tuple[ProductDependency, ...]
    # Human-readable rule breaks: a ref() to a model that is not public, or a cycle between products.
    violations: tuple[str, ...]

    @property
    def producers_by_consumer(self) -> dict[str, tuple[str, ...]]:
        """Each product with the products it consumes; absent for a product that reads only Silver."""
        found: dict[str, list[str]] = {}
        for dependency in self.dependencies:
            found.setdefault(dependency.consumer, []).append(dependency.producer)
        return {consumer: tuple(sorted(producers)) for consumer, producers in sorted(found.items())}

    @property
    def consumers_by_producer(self) -> dict[str, tuple[str, ...]]:
        found: dict[str, list[str]] = {}
        for dependency in self.dependencies:
            found.setdefault(dependency.producer, []).append(dependency.consumer)
        return {producer: tuple(sorted(consumers)) for producer, consumers in sorted(found.items())}


    def launch_order(self, products: Sequence[str]) -> list[str]:
        """`products` with each one after the products it consumes (among `products`), otherwise in the given order.

        A product that reads another's tables fails its run if the other has not built, so anything that launches
        several products in one go launches them in this order.
        """
        wanted = set(products)
        graph = {
            product: {producer for producer in self.producers_by_consumer.get(product, ()) if producer in wanted}
            for product in products
        }
        sorter = TopologicalSorter(graph)
        sorter.prepare()
        ordered: list[str] = []
        while sorter.is_active():
            ready = sorted(sorter.get_ready(), key=products.index)
            ordered.extend(ready)
            sorter.done(*ready)
        return ordered


def scan_product_dependencies(gold_root: Path, products: Collection[str]) -> ProductDependencies:
    """The cross-product `ref()` edges of the Gold project at `gold_root`, for the declared `products`.

    A model belongs to the product whose folder under `models/` holds it, and is named by its file name, as dbt
    names it. A `ref()` to a name no product folder holds is left to dbt.
    """
    models_root = gold_root / "models"
    owner: dict[str, str] = {}
    sql: dict[str, Path] = {}
    access: dict[str, str] = {}
    for product in sorted(products):
        folder = models_root / product
        for path in sorted(folder.rglob("*.sql")) if folder.is_dir() else ():
            owner.setdefault(path.stem, product)
            sql[path.stem] = path
        for path in sorted(folder.rglob("*.yml")) if folder.is_dir() else ():
            access.update(_declared_access(path))

    edges: dict[tuple[str, str], set[str]] = {}
    violations: list[str] = []
    for name, path in sorted(sql.items()):
        consumer = owner[name]
        text = _JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))
        for referenced in sorted(set(_REF.findall(text))):
            producer = owner.get(referenced)
            if producer is None or producer == consumer:
                continue
            edges.setdefault((consumer, producer), set()).add(referenced)
            if access.get(referenced) != "public":
                violations.append(
                    f"{consumer}.{name} refs {producer}.{referenced}, which is not `access: public` "
                    f"(set it on that model in {producer}'s schema.yml)"
                )
    dependencies = tuple(
        ProductDependency(consumer, producer, tuple(sorted(models)))
        for (consumer, producer), models in sorted(edges.items())
    )
    violations.extend(_cycles(dependencies))
    return ProductDependencies(dependencies, tuple(violations))


def _declared_access(path: Path) -> dict[str, str]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(document, Mapping):
        return {}
    declared: dict[str, str] = {}
    for model in document.get("models") or ():
        if not isinstance(model, Mapping) or "name" not in model:
            continue
        config: Any = model.get("config")
        level = model.get("access") or (config.get("access") if isinstance(config, Mapping) else None)
        if isinstance(level, str):
            declared[str(model["name"])] = level
    return declared


def _cycles(dependencies: Collection[ProductDependency]) -> list[str]:
    graph: dict[str, set[str]] = {}
    for dependency in dependencies:
        graph.setdefault(dependency.consumer, set()).add(dependency.producer)
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        cycle = " -> ".join(error.args[1])
        return [f"products consume each other in a cycle ({cycle}); no run order satisfies it"]
    return []
