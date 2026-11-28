"""A semantic model checked against the Gold dbt properties it describes, offline.

dbt owns column names, descriptions, key tests and relationship tests (`schema.yml`); Ossie owns metrics, keys
and relationships as business statements. This module is where the two are held to each other, so an Ossie key is
only ever published when dbt proves it. It reads `schema.yml` as text, like `product_dependencies`, so it needs no
dbt run and no manifest.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .product_dependencies import ProductDependencies
from .semantic import (
    SemanticDiagnostic,
    SemanticInventory,
    SemanticModel,
    discover_semantic_files,
    parse_ossie_model,
)

__all__ = ["DbtModel", "scan_dbt_models", "validate_semantic_model", "load_semantic_inventory"]

_REF = re.compile(r"""ref\s*\(\s*(?:['"][^'"]+['"]\s*,\s*)?['"]([^'"]+)['"]""")
_COMPOSITE_UNIQUE = "unique_combination_of_columns"


@dataclass
class DbtModel:
    product: str
    name: str
    # column -> description ("" when undocumented); a column exists for Ossie when `schema.yml` declares it.
    columns: dict[str, str] = field(default_factory=dict)
    unique: set[tuple[str, ...]] = field(default_factory=set)
    not_null: set[str] = field(default_factory=set)
    # (column, referenced model, referenced column)
    relationships: set[tuple[str, str, str]] = field(default_factory=set)


def scan_dbt_models(gold_root: Path, products: Collection[str]) -> dict[tuple[str, str], DbtModel]:
    """Every model a product's `schema.yml` files declare, keyed `(product, model)`."""
    found: dict[tuple[str, str], DbtModel] = {}
    for product in sorted(products):
        folder = gold_root / "models" / product
        paths = sorted([*folder.rglob("*.yml"), *folder.rglob("*.yaml")]) if folder.is_dir() else ()
        for path in paths:
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            for entry in document.get("models") or () if isinstance(document, Mapping) else ():
                if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                    found[(product, entry["name"])] = _dbt_model(product, entry)
    return found


def _tests(node: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for key in ("tests", "data_tests"):
        for test in node.get(key) or ():
            if isinstance(test, str):
                yield test, {}
            elif isinstance(test, Mapping):
                for name, config in test.items():
                    config = config if isinstance(config, Mapping) else {}
                    arguments = config.get("arguments")
                    yield str(name).rsplit(".", 1)[-1], arguments if isinstance(arguments, Mapping) else config


def _dbt_model(product: str, entry: Mapping[str, Any]) -> DbtModel:
    model = DbtModel(product, str(entry["name"]))
    for test, arguments in _tests(entry):
        combination = arguments.get("combination_of_columns")
        if test == _COMPOSITE_UNIQUE and isinstance(combination, list):
            model.unique.add(tuple(sorted(str(column) for column in combination)))
    for column in entry.get("columns") or ():
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            continue
        name = column["name"]
        model.columns[name] = str(column.get("description") or "")
        for test, arguments in _tests(column):
            if test == "unique":
                model.unique.add((name,))
            elif test == "not_null":
                model.not_null.add(name)
            elif test == "relationships":
                target = _REF.search(str(arguments.get("to", "")))
                if target and isinstance(arguments.get("field"), str):
                    model.relationships.add((name, target.group(1), str(arguments["field"])))
    return model


def validate_semantic_model(
    model: SemanticModel,
    dbt: Mapping[tuple[str, str], DbtModel],
    dependencies: ProductDependencies,
) -> list[SemanticDiagnostic]:
    """Rules 2 to 6 of issue #920 for one parsed model."""
    problems: list[SemanticDiagnostic] = []

    def fail(path: tuple[str | int, ...], rule: str, message: str, severity: str = "error") -> None:
        problems.append(SemanticDiagnostic(model.file, model.line(*path), rule, message, severity))

    for label, names in (
        ("dataset", [d.name for d in model.datasets]),
        ("relationship", [r.name for r in model.relationships]),
        ("metric", [m.name for m in model.metrics]),
    ):
        for name, count in Counter(names).items():
            if count > 1:
                fail((f"{label}s",), "duplicate-name", f"{label} name {name!r} is declared {count} times")
    allowed = {
        d.producer: {m for m in d.models if (d.producer, m) not in dependencies.private_refs}
        for d in dependencies.dependencies
        if d.consumer == model.product
    }
    resolved: dict[str, DbtModel] = {}
    for index, dataset in enumerate(model.datasets):
        at = ("datasets", index)
        for name, count in Counter(f.name for f in dataset.fields).items():
            if count > 1:
                fail(
                    (*at, "fields"), "duplicate-name", f"dataset {dataset.name!r} declares field {name!r} {count} times"
                )
        owner = _owner(dataset.schema, model.product, allowed, dataset.table)
        if owner is None or (owner, dataset.table) not in dbt:
            fail(
                (*at, "source"),
                "source-exists",
                f"{dataset.source!r} is not a model of product {model.product!r} or a public model of a product "
                "it depends on",
            )
            continue
        source = dbt[(owner, dataset.table)]
        resolved[dataset.name] = source
        fields = {f.name for f in dataset.fields}
        for position, declared in enumerate(dataset.fields):
            if declared.name not in source.columns:
                fail(
                    (*at, "fields", position),
                    "field-is-column",
                    f"field {declared.name!r} is not a column of {dataset.source} in its schema.yml",
                )
            elif declared.description and source.columns[declared.name] != declared.description:
                fail(
                    (*at, "fields", position),
                    "description-differs",
                    f"field {declared.name!r} differs from its dbt column description; dbt owns that text",
                    "warning",
                )
        keys: list[tuple[tuple[str | int, ...], tuple[str, ...], bool]] = (
            [(("primary_key",), dataset.primary_key, True)] if dataset.primary_key else []
        )
        keys += [(("unique_keys", i), key, False) for i, key in enumerate(dataset.unique_keys)]
        for key_path, key, primary in keys:
            _check_key(fail, at, key_path, key, primary, dataset.name, source, fields)
    for index, relationship in enumerate(model.relationships):
        _check_relationship(fail, ("relationships", index), relationship, model, resolved)
    declared_fields = {d.name: {f.name for f in d.fields} for d in model.datasets}
    for index, metric in enumerate(model.metrics):
        if not metric.expression:
            continue
        for owner_dataset, column in metric.references:
            if column not in declared_fields.get(owner_dataset, ()):
                fail(
                    ("metrics", index, "expression"),
                    "metric-reference",
                    f"metric {metric.name!r} reads {owner_dataset}.{column}, which is not a declared field",
                )
    # Glossary references (rule 6) are validated here once the glossary inventory exists: the 0.1.1 schema has no
    # term field, so the reference syntax is decided with the glossary descriptor, not guessed here.
    return problems


def _owner(schema: str, product: str, allowed: Mapping[str, set[str]], table: str) -> str | None:
    if schema == f"{product}_gold":
        return product
    for producer, models in allowed.items():
        if schema == f"{producer}_gold" and table in models:
            return producer
    return None


def _check_key(
    fail: Any,
    at: tuple[str | int, ...],
    key_path: tuple[str | int, ...],
    key: tuple[str, ...],
    primary: bool,
    dataset: str,
    source: DbtModel,
    fields: set[str],
) -> None:
    where = (*at, *key_path)
    label = "primary_key" if primary else "unique_keys"
    for column in key:
        if column not in fields:
            fail(where, "key-field", f"{label} of {dataset!r} names {column!r}, which is not a declared field")
    if tuple(sorted(key)) not in source.unique:
        fail(where, "key-test", f"{label} ({', '.join(key)}) of {dataset!r} has no dbt `unique` test on {source.name}")
    elif primary and not set(key) <= source.not_null:
        fail(
            where,
            "key-test",
            f"primary_key ({', '.join(key)}) of {dataset!r} needs a dbt `not_null` test on each column",
        )


def _check_relationship(
    fail: Any, at: tuple[str | int, ...], relationship: Any, model: SemanticModel, resolved: Mapping[str, DbtModel]
) -> None:
    known = {d.name for d in model.datasets}
    for side in (relationship.from_dataset, relationship.to_dataset):
        if side not in known:
            fail(at, "relationship-dataset", f"relationship {relationship.name!r} names unknown dataset {side!r}")
            return
    if len(relationship.from_columns) != 1 or len(relationship.to_columns) != 1:
        fail(
            at, "single-column-relationships", f"relationship {relationship.name!r} must join one column to one column"
        )
        return
    source, target = resolved.get(relationship.from_dataset), resolved.get(relationship.to_dataset)
    if source is None or target is None:
        return
    wanted = (relationship.from_columns[0], target.name, relationship.to_columns[0])
    if wanted not in source.relationships:
        fail(
            at,
            "relationship-test",
            f"relationship {relationship.name!r} has no dbt `relationships` test: {source.name}.{wanted[0]} "
            f"to ref('{target.name}').{wanted[2]}",
        )
    if (wanted[2],) not in target.unique:
        fail(
            at,
            "relationship-target-key",
            f"relationship {relationship.name!r} targets {target.name}.{wanted[2]}, which has no dbt `unique` test",
        )


def load_semantic_inventory(
    code_root: Path,
    products: Collection[str],
    dependencies: ProductDependencies,
) -> SemanticInventory:
    """Every `semantic/<product>/*.ossie.yaml`, parsed and checked; the models that passed and every rule break."""
    files, unknown = discover_semantic_files(code_root, products)
    problems = [
        SemanticDiagnostic(
            f"lakehouse_code/semantic/{name}", 1, "product-exists", f"{name!r} is not a product in governance.yml"
        )
        for name in unknown
    ]
    dbt = scan_dbt_models(code_root / "gold", products)
    models: list[SemanticModel] = []
    for product, path in files:
        parsed, errors = parse_ossie_model(path, product, code_root)
        problems.extend(errors)
        if parsed is None:
            continue
        found = validate_semantic_model(parsed, dbt, dependencies)
        problems.extend(found)
        if not any(d.severity == "error" for d in found):
            models.append(parsed)
    names = Counter(m.name for m in models)
    problems.extend(
        SemanticDiagnostic(
            m.file, m.line("name"), "duplicate-name", f"model name {m.name!r} is used by {names[m.name]} files"
        )
        for m in models
        if names[m.name] > 1
    )
    return SemanticInventory(tuple(m for m in models if names[m.name] == 1), tuple(problems))
