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
# dbt evaluates only what sits in `{{ }}` or `{% %}`: a `ref('x')` in a plain SQL comment is text, while one in
# `-- depends_on: {{ ref('x') }}` is a real dependency.
_JINJA_BLOCK = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_CONFIG_ACCESS = re.compile(r"""\bconfig\([^)]*?\baccess\s*=\s*['"](\w+)['"]""", re.DOTALL)
# `ref('model')`, `ref("model")` and `ref('package', 'model')`: the model is the last argument.
_REF = re.compile(
    r"""\bref\s*\(\s*(?:['"]([^'"]+)['"]\s*,\s*)?['"]([^'"]+)['"]\s*(?:,\s*v(?:ersion)?\s*=[^)]*)?\)"""
)
_CONFIG_DISABLED = re.compile(r"\bconfig\([^)]*?\benabled\s*=\s*(?:false|False)\b", re.DOTALL)


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
        try:
            sorter.prepare()
        except CycleError:
            # `olf check contracts` refuses the cycle; a launcher keeps going in the order it was given.
            return list(products)
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
    defaults = _project_access(gold_root)
    project_name = _project_name(gold_root)
    owner: dict[str, str] = {}
    sql: dict[str, Path] = {}
    declared: dict[str, str] = {}
    disabled: set[str] = set()
    schema_refs: dict[str, set[tuple[str, str]]] = {}
    for product in sorted(products):
        folder = models_root / product
        for path in sorted(folder.rglob("*.sql")) if folder.is_dir() else ():
            owner.setdefault(path.stem, product)
            sql[path.stem] = path
        for path in sorted(folder.rglob("*.yml")) if folder.is_dir() else ():
            access_levels, switched_off, tested = _declared_properties(path)
            declared.update(access_levels)
            disabled |= switched_off
            schema_refs.setdefault(product, set()).update(tested)

    def access_of(model: str, text: str) -> str:
        # dbt's precedence: the model's own config(), then its properties file, then dbt_project.yml.
        in_sql = _CONFIG_ACCESS.search(_executable(text))
        if in_sql:
            return in_sql.group(1)
        return declared.get(model) or defaults.get(owner[model]) or defaults.get("") or "protected"

    texts = {name: _JINJA_COMMENT.sub("", path.read_text(encoding="utf-8")) for name, path in sql.items()}
    edges: dict[tuple[str, str], set[str]] = {}
    violations: list[str] = []

    def consider(consumer: str, reader: str, package: str, referenced: str) -> None:
        if package and package != project_name:
            return
        producer = owner.get(referenced)
        if producer is None or producer == consumer:
            return
        edges.setdefault((consumer, producer), set()).add(referenced)
        if access_of(referenced, texts[referenced]) != "public":
            violations.append(
                f"{consumer}.{reader} refs {producer}.{referenced}, which is not `access: public` "
                f"(set it on that model in {producer}'s schema.yml)"
            )

    for name, text in sorted(texts.items()):
        executable = _executable(text)
        if name in disabled or _CONFIG_DISABLED.search(executable):
            continue
        for package, referenced in sorted(set(_REF.findall(executable))):
            consider(owner[name], name, package, referenced)
    # A relationships test declared in schema.yml reads the model its `to: ref(...)` names, with no ref() in any SQL.
    for consumer, tested in sorted(schema_refs.items()):
        for package, referenced in sorted(tested):
            consider(consumer, "schema.yml", package, referenced)
    dependencies = tuple(
        ProductDependency(consumer, producer, tuple(sorted(models)))
        for (consumer, producer), models in sorted(edges.items())
    )
    violations.extend(_cycles(dependencies))
    return ProductDependencies(dependencies, tuple(violations))


def _executable(text: str) -> str:
    """What dbt evaluates of a model file: its `{{ }}` and `{% %}` blocks, not plain SQL comments."""
    return " ".join(_JINJA_BLOCK.findall(text))


def _project_access(gold_root: Path) -> dict[str, str]:
    """`+access` from dbt_project.yml: "" is the project-wide default, a product name its folder's."""
    path = gold_root / "dbt_project.yml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(document, Mapping):
        return {}
    models = document.get("models")
    project = models.get(document.get("name")) if isinstance(models, Mapping) else None
    if not isinstance(project, Mapping):
        return {}
    found = {"": project["+access"]} if isinstance(project.get("+access"), str) else {}
    for folder, block in project.items():
        if isinstance(block, Mapping) and isinstance(block.get("+access"), str):
            found[str(folder)] = block["+access"]
    return found


def _project_name(gold_root: Path) -> str | None:
    try:
        document = yaml.safe_load((gold_root / "dbt_project.yml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    name = document.get("name") if isinstance(document, Mapping) else None
    return name if isinstance(name, str) else None


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [text for value in node.values() for text in _strings(value)]
    if isinstance(node, list):
        return [text for item in node for text in _strings(item)]
    return []


def _declared_properties(path: Path) -> tuple[dict[str, str], set[str], set[tuple[str, str]]]:
    """What a properties file declares: `access` per model, the models it disables, and the `(package, model)`
    refs its tests name (a relationships test's `to: ref('x')`)."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}, set(), set()
    if not isinstance(document, Mapping):
        return {}, set(), set()
    declared: dict[str, str] = {}
    disabled: set[str] = set()
    for model in document.get("models") or ():
        if not isinstance(model, Mapping) or "name" not in model:
            continue
        config: Any = model.get("config")
        config = config if isinstance(config, Mapping) else {}
        level = model.get("access") or config.get("access")
        if isinstance(level, str):
            declared[str(model["name"])] = level
        if config.get("enabled") is False:
            disabled.add(str(model["name"]))
    tested = {ref for text in _strings(document.get("models")) for ref in _REF.findall(text)}
    return declared, disabled, tested


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
