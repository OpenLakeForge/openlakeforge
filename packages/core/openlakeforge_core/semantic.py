"""Apache Ossie semantic models, parsed into a model that does not depend on the spec version.

The adapter targets Ossie 0.1.1 in the subset that survives the 0.3 changes unchanged (one model per document,
field names equal to column names, `dataset.field` metric references, no `custom_extensions`), so the readers that
follow (the OpenMetadata publisher, the agent read API) see `SemanticModel` and never the document shape. A document
outside the subset is refused with the file, the line and the rule it breaks.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "SUPPORTED_OSSIE_VERSION",
    "AiContext",
    "SemanticDataset",
    "SemanticDiagnostic",
    "SemanticField",
    "SemanticInventory",
    "SemanticMetric",
    "SemanticModel",
    "SemanticRelationship",
    "discover_semantic_files",
    "load_ossie_document",
    "parse_ossie_model",
]

SUPPORTED_OSSIE_VERSION = "0.1.1"
SEMANTIC_SUFFIX = ".ossie.yaml"
_ANSI = "ANSI_SQL"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_QUALIFIED = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\b(?!\s*\()")
_BARE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\b(?!\s*[.(])(?!\s*\.)")
# Bare words a metric may use without qualifying: SQL keywords, type names and interval units. Any other bare word is
# read as an unqualified field, so a misspelled column cannot pass as a keyword.
_SQL_WORDS = frozenset(
    (
        "and or not in is null case when then else end distinct as between like true false cast asc desc filter where "
        "over partition by order rows range unbounded preceding following current row interval from for date time "
        "timestamp year quarter month week day hour minute second integer int bigint smallint double float real "
        "decimal numeric varchar char string boolean"
    ).split()
)


@dataclass(frozen=True)
class SemanticDiagnostic:
    """One rule break, located. `rule` names the subset or validation rule so the fix is findable."""

    file: str
    line: int
    rule: str
    message: str
    # A warning is reported but does not stop a model from being published.
    severity: str = "error"

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: [{self.rule}] {self.message}"


@dataclass(frozen=True)
class AiContext:
    instructions: str = ""
    synonyms: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticField:
    name: str
    is_time: bool = False
    label: str = ""
    description: str = ""
    ai_context: AiContext | None = None


@dataclass(frozen=True)
class SemanticDataset:
    name: str
    source: str
    schema: str
    table: str
    primary_key: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()
    description: str = ""
    ai_context: AiContext | None = None
    fields: tuple[SemanticField, ...] = ()


@dataclass(frozen=True)
class SemanticRelationship:
    """`from_dataset` holds the foreign key; it points at the key of `to_dataset`."""

    name: str
    from_dataset: str
    to_dataset: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    ai_context: AiContext | None = None


@dataclass(frozen=True)
class SemanticMetric:
    name: str
    expression: str
    # `(dataset, field)` pairs the expression reads, in first-use order.
    references: tuple[tuple[str, str], ...] = ()
    description: str = ""
    ai_context: AiContext | None = None


@dataclass(frozen=True)
class SemanticModel:
    product: str
    name: str
    datasets: tuple[SemanticDataset, ...]
    relationships: tuple[SemanticRelationship, ...] = ()
    metrics: tuple[SemanticMetric, ...] = ()
    description: str = ""
    ai_context: AiContext | None = None
    # Provenance: the Ossie version, the file relative to `lakehouse_code/`, and a hash of its bytes.
    version: str = SUPPORTED_OSSIE_VERSION
    file: str = ""
    content_hash: str = ""
    # Where each element sits in the file, keyed by its path in the document: ("datasets", 0, "fields", 2).
    locations: Mapping[tuple[str | int, ...], int] = field(default_factory=dict, compare=False, repr=False)

    def line(self, *path: str | int) -> int:
        """The line of `path`, or of its nearest enclosing element that has one."""
        for end in range(len(path), -1, -1):
            if path[:end] in self.locations:
                return self.locations[path[:end]]
        return 1


@dataclass(frozen=True)
class SemanticInventory:
    models: tuple[SemanticModel, ...]
    diagnostics: tuple[SemanticDiagnostic, ...]


def discover_semantic_files(code_root: Path, products: Collection[str]) -> tuple[list[tuple[str, Path]], list[str]]:
    """`(product, file)` for every `semantic/<product>/*.ossie.yaml`, and the folders that name no product."""
    root = code_root / "semantic"
    found: list[tuple[str, Path]] = []
    unknown: list[str] = []
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        if not folder.is_dir() or folder.name.startswith(("_", ".")):
            continue
        if folder.name not in products:
            unknown.append(folder.name)
            continue
        found.extend((folder.name, path) for path in sorted(folder.glob(f"*{SEMANTIC_SUFFIX}")))
    return found, unknown


def load_ossie_document(path: Path) -> tuple[Any, dict[tuple[str | int, ...], int], list[tuple[int, str]]]:
    """The document, the line of every element by path, and `(line, key)` for each mapping key written twice."""
    loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))
    try:
        root = loader.get_single_node()
    finally:
        loader.dispose()
    lines: dict[tuple[str | int, ...], int] = {}
    duplicates: list[tuple[int, str]] = []
    constructor = yaml.SafeLoader("")

    flattened: set[int] = set()

    def build(node: yaml.Node, where: tuple[str | int, ...]) -> Any:
        lines[where] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            # `<<: *anchor` is flattened into the node, after which an explicit key that overrides a merged one is
            # indistinguishable from a repeat. Repeats are therefore counted on the keys as written, once per node.
            if id(node) not in flattened:
                flattened.add(id(node))
                written: set[Any] = set()
                for key_node, _ in node.value:
                    if key_node.tag == "tag:yaml.org,2002:merge":
                        continue
                    key = constructor.construct_object(key_node, deep=True)
                    if key in written:
                        duplicates.append((key_node.start_mark.line + 1, str(key)))
                    written.add(key)
                constructor.flatten_mapping(node)
            built: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = constructor.construct_object(key_node, deep=True)
                built[key] = build(value_node, (*where, key if isinstance(key, str) else str(key)))
            return built
        if isinstance(node, yaml.SequenceNode):
            return [build(item, (*where, index)) for index, item in enumerate(node.value)]
        return constructor.construct_object(node, deep=True)

    if root is None:
        return None, lines, duplicates
    return build(root, ()), lines, duplicates


def parse_ossie_model(
    path: Path, product: str, code_root: Path
) -> tuple[SemanticModel | None, list[SemanticDiagnostic]]:
    """The model in `path`, or the subset violations that stop it from being one.

    Shape errors the JSON Schema also reports (a field of the wrong type) are skipped here: `olf check contracts`
    runs the schema first, and this module is lenient about what it cannot read so one fault is reported once.
    """
    shown = _relative(path, code_root)
    try:
        document, lines, duplicates = load_ossie_document(path)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        return None, [SemanticDiagnostic(shown, mark.line + 1 if mark else 1, "yaml", f"not valid YAML: {error}")]
    problems: list[SemanticDiagnostic] = []

    def fail(where: tuple[str | int, ...], rule: str, message: str) -> None:
        line = next((lines[where[:end]] for end in range(len(where), -1, -1) if where[:end] in lines), 1)
        problems.append(SemanticDiagnostic(shown, line, rule, message))

    for line, key in duplicates:
        problems.append(SemanticDiagnostic(shown, line, "duplicate-key", f"mapping key {key!r} is written twice"))
    if not isinstance(document, Mapping):
        fail((), "schema", "the document must be a mapping with `version` and `semantic_model`")
        return None, problems
    if document.get("version") != SUPPORTED_OSSIE_VERSION:
        fail(
            ("version",),
            "ossie-version",
            f"version is {document.get('version')!r}; only Ossie {SUPPORTED_OSSIE_VERSION} is supported "
            "(development versions such as *.dev0 are never accepted)",
        )
    models = document.get("semantic_model")
    if not isinstance(models, list) or not models:
        fail(("semantic_model",), "schema", "`semantic_model` must be a list holding one model")
        return None, problems
    if len(models) != 1:
        fail(("semantic_model", 1), "one-model-per-file", f"a file holds one model, found {len(models)}")
    raw = models[0]
    base: tuple[str | int, ...] = ("semantic_model", 0)
    if not isinstance(raw, Mapping):
        fail(base, "schema", "the model must be a mapping")
        return None, problems
    _forbid_extensions(raw, base, fail)
    _require(raw, base, ("name", "datasets"), fail)
    for key in ("datasets", "relationships", "metrics"):
        if not isinstance(raw.get(key) or [], list):
            fail((*base, key), "schema", f"`{key}` must be a list")
    if problems:
        return None, problems

    datasets = tuple(
        _dataset(item, (*base, "datasets", index), fail)
        for index, item in enumerate(raw.get("datasets") or ())
        if isinstance(item, Mapping)
    )
    relationships = tuple(
        _relationship(item, (*base, "relationships", index), fail)
        for index, item in enumerate(raw.get("relationships") or ())
        if isinstance(item, Mapping)
    )
    metrics = tuple(
        _metric(item, (*base, "metrics", index), fail)
        for index, item in enumerate(raw.get("metrics") or ())
        if isinstance(item, Mapping)
    )
    if problems:
        return None, problems
    content = path.read_bytes()
    model = SemanticModel(
        product=product,
        name=str(raw.get("name", "")),
        datasets=datasets,
        relationships=relationships,
        metrics=metrics,
        description=str(raw.get("description", "")),
        ai_context=_ai_context(raw.get("ai_context")),
        version=SUPPORTED_OSSIE_VERSION,
        file=shown,
        content_hash=f"sha256:{hashlib.sha256(content).hexdigest()}",
        locations={_shift(key, base): line for key, line in lines.items() if key[: len(base)] == base},
    )
    return model, []


def _shift(key: tuple[str | int, ...], base: tuple[str | int, ...]) -> tuple[str | int, ...]:
    return key[len(base) :]


def _relative(path: Path, code_root: Path) -> str:
    try:
        return path.relative_to(code_root.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _forbid_extensions(node: Any, where: tuple[str | int, ...], fail: Any) -> None:
    """`custom_extensions` is renamed in Ossie 0.3 and its payload is vendor-specific, so the subset has none."""
    if isinstance(node, Mapping):
        if "custom_extensions" in node:
            fail((*where, "custom_extensions"), "no-custom-extensions", "`custom_extensions` is outside the subset")
        for key, value in node.items():
            if key != "custom_extensions":
                _forbid_extensions(value, (*where, str(key)), fail)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _forbid_extensions(item, (*where, index), fail)


def _require(item: Mapping[str, Any], where: tuple[str | int, ...], keys: tuple[str, ...], fail: Any) -> None:
    """The properties the Ossie schema requires, for callers that reach the model without the JSON Schema pass."""
    for key in keys:
        value = item.get(key)
        if not value or not isinstance(value, str | list):
            fail((*where, key) if key in item else where, "schema", f"`{key}` is required and must not be empty")


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _ai_context(value: Any) -> AiContext | None:
    if isinstance(value, str):
        return AiContext(instructions=value)
    if isinstance(value, Mapping):
        return AiContext(
            instructions=str(value.get("instructions", "")),
            synonyms=_strings(value.get("synonyms")),
            examples=_strings(value.get("examples")),
        )
    return None


def _ansi_expression(item: Mapping[str, Any]) -> str | None:
    expression = item.get("expression")
    dialects = expression.get("dialects") if isinstance(expression, Mapping) else None
    for entry in dialects if isinstance(dialects, list) else ():
        if isinstance(entry, Mapping) and entry.get("dialect") == _ANSI and isinstance(entry.get("expression"), str):
            return str(entry["expression"]).strip()
    return None


def _dataset(item: Mapping[str, Any], where: tuple[str | int, ...], fail: Any) -> SemanticDataset:
    _require(item, where, ("name", "source"), fail)
    name = str(item.get("name", ""))
    source = str(item.get("source", ""))
    parts = source.split(".")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        fail(
            (*where, "source"),
            "logical-source",
            f"source {source!r} must be `<schema>.<table>` (for example sales_core_gold.fct_order_lines): "
            "no catalog, stage or query; the deploy adapter resolves the stage",
        )
        parts = [source, ""]
    fields: list[SemanticField] = []
    declared_fields = item.get("fields") or []
    if not isinstance(declared_fields, list):
        fail((*where, "fields"), "schema", "`fields` must be a list")
        declared_fields = []
    for index, raw in enumerate(declared_fields):
        if not isinstance(raw, Mapping):
            continue
        at = (*where, "fields", index)
        _require(raw, at, ("name",), fail)
        written = _ansi_expression(raw)
        if written is None:
            fail((*at, "expression"), "ansi-expression", f"field {raw.get('name')!r} needs an {_ANSI} expression")
        elif written != raw.get("name"):
            fail(
                (*at, "expression"),
                "field-equals-column",
                f"field {raw.get('name')!r} must be written as the column itself (`{raw.get('name')}`), "
                f"not `{written}`; Ossie 0.3 resolves keys and metrics to fields",
            )
        dimension = raw.get("dimension")
        fields.append(
            SemanticField(
                name=str(raw.get("name", "")),
                is_time=bool(dimension.get("is_time")) if isinstance(dimension, Mapping) else False,
                label=str(raw.get("label", "")),
                description=str(raw.get("description", "")),
                ai_context=_ai_context(raw.get("ai_context")),
            )
        )
    keys = item.get("unique_keys")
    return SemanticDataset(
        name=name,
        source=source,
        schema=parts[0],
        table=parts[1],
        primary_key=_strings(item.get("primary_key")),
        unique_keys=tuple(_strings(key) for key in keys if isinstance(key, list)) if isinstance(keys, list) else (),
        description=str(item.get("description", "")),
        ai_context=_ai_context(item.get("ai_context")),
        fields=tuple(fields),
    )


def _relationship(item: Mapping[str, Any], where: tuple[str | int, ...], fail: Any) -> SemanticRelationship:
    _require(item, where, ("name", "from", "to", "from_columns", "to_columns"), fail)
    return SemanticRelationship(
        name=str(item.get("name", "")),
        from_dataset=str(item.get("from", "")),
        to_dataset=str(item.get("to", "")),
        from_columns=_strings(item.get("from_columns")),
        to_columns=_strings(item.get("to_columns")),
        ai_context=_ai_context(item.get("ai_context")),
    )


def _metric(item: Mapping[str, Any], where: tuple[str | int, ...], fail: Any) -> SemanticMetric:
    _require(item, where, ("name",), fail)
    name = str(item.get("name", ""))
    expression = _ansi_expression(item)
    if expression is None:
        fail((*where, "expression"), "ansi-expression", f"metric {name!r} needs an {_ANSI} expression")
        expression = ""
    stripped = _STRING_LITERAL.sub("''", expression)
    references: list[tuple[str, str]] = []
    for dataset, column in _QUALIFIED.findall(stripped):
        if (dataset, column) not in references:
            references.append((dataset, column))
    # A qualified reference is masked first so its column is not read again as a bare word.
    bare = {word for word in _BARE.findall(_QUALIFIED.sub(" ", stripped)) if word.lower() not in _SQL_WORDS}
    for word in sorted(bare):
        fail(
            (*where, "expression"),
            "metric-qualified-references",
            f"metric {name!r} reads `{word}` unqualified; write fields as `<dataset>.<field>` (Ossie 0.1.1 metrics "
            "are model-scoped)",
        )
    return SemanticMetric(
        name=name,
        expression=expression,
        references=tuple(references),
        description=str(item.get("description", "")),
        ai_context=_ai_context(item.get("ai_context")),
    )
