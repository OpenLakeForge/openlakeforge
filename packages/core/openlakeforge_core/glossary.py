"""The ``Glossary`` descriptor at ``lakehouse_code/glossaries/<name>.yml`` and the term links to it.

A glossary holds business terms in Git, independent of dbt code. Terms are
referenced as ``<glossary>.<Term>`` from ``governance.yml`` (domains and
products) and from dbt column ``meta`` in the Gold ``schema.yml`` files. Nothing
here talks to OpenMetadata; publishing is a separate step that consumes these
types.

A term's reference is flat (``<glossary>.<Term>``) even when it has a
``parent``: the reference stays stable if a term moves in the hierarchy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from .descriptors import LakehouseDescriptorError, _identifier, _non_empty_string
from .governance import EntityRef, _entity_ref, parse_term_refs

GLOSSARY_API_VERSION = "openlakeforge.io/v1alpha1"
GLOSSARY_KIND = "Glossary"
# OpenMetadata 1.13.6 `type/status.json` (EntityStatus), so a status publishes unchanged.
TERM_STATUSES = ("Draft", "In Review", "Approved", "Archived", "Deprecated", "Rejected", "Unprocessed")
# No `.` (the FQN separator) and no whitespace or quotes, so a term never needs FQN escaping.
TERM_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
# https only, and no `http://` anywhere in it: the revision build rejects that text as a stage-bound endpoint
# (project_revision.py), even inside a query string.
REFERENCE_ENDPOINT_PATTERN = r"^https://(?!\S*http://)\S+$"

_GLOSSARY_FIELDS = {"apiVersion", "kind", "name", "displayName", "description", "owner", "terms"}
_TERM_FIELDS = {
    "name",
    "displayName",
    "description",
    "synonyms",
    "status",
    "relatedTerms",
    "references",
    "parent",
}
_TERM_NAME = re.compile(TERM_NAME_PATTERN)
_ENDPOINT = re.compile(REFERENCE_ENDPOINT_PATTERN)


@dataclass(frozen=True)
class TermReference:
    """An external page that defines or supports a term."""

    name: str
    endpoint: str


@dataclass(frozen=True)
class GlossaryTerm:
    glossary: str
    name: str
    display_name: str
    description: str
    synonyms: tuple[str, ...] = ()
    status: str | None = None
    related_terms: tuple[str, ...] = ()
    references: tuple[TermReference, ...] = ()
    parent: str | None = None

    @property
    def fqn(self) -> str:
        return f"{self.glossary}.{self.name}"


@dataclass(frozen=True)
class Glossary:
    path: Path
    name: str
    display_name: str
    description: str
    terms: tuple[GlossaryTerm, ...]
    owner: EntityRef | None = None


@dataclass(frozen=True)
class ColumnTermLink:
    """Term references on one dbt column, read from ``meta.openlakeforge.glossaryTerms``."""

    path: Path
    product: str
    model: str
    column: str
    terms: tuple[str, ...]

    @property
    def is_mart(self) -> bool:
        return self.model.startswith("mart_")


def _string_list(value: object, *, field: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise LakehouseDescriptorError(f"{where}: {field} must be a non-empty array of non-empty strings")
    return tuple(value)


def _term(glossary: str, entry: object, index: int, *, source: str) -> GlossaryTerm:
    where = f"{source}: terms[{index}]"
    if not isinstance(entry, Mapping):
        raise LakehouseDescriptorError(f"{where}: must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or not _TERM_NAME.fullmatch(name):
        raise LakehouseDescriptorError(f"{where}: name must match {TERM_NAME_PATTERN!r}, got {name!r}")
    where = f"{source}: terms[{name}]"
    unexpected = sorted(set(entry) - _TERM_FIELDS)
    if unexpected:
        raise LakehouseDescriptorError(f"{where}: must not contain {unexpected!r}")
    display_name = _non_empty_string(entry.get("displayName"), field="displayName", source=where)
    description = _non_empty_string(entry.get("description"), field="description", source=where)
    if not description.strip():
        raise LakehouseDescriptorError(f"{where}: description must be a non-empty string")
    status = entry.get("status")
    if status is not None and status not in TERM_STATUSES:
        raise LakehouseDescriptorError(f"{where}: status must be one of {list(TERM_STATUSES)!r}, got {status!r}")
    parent = entry.get("parent")
    if parent is not None and (not isinstance(parent, str) or not _TERM_NAME.fullmatch(parent)):
        raise LakehouseDescriptorError(f"{where}: parent must be the name of a term in this glossary, got {parent!r}")
    references = []
    raw_references = entry.get("references", [])
    if not isinstance(raw_references, list):
        raise LakehouseDescriptorError(f"{where}: references must be an array of {{name, endpoint}} objects")
    for ref_index, ref in enumerate(raw_references):
        ref_where = f"{where}: references[{ref_index}]"
        if not isinstance(ref, Mapping) or set(ref) != {"name", "endpoint"}:
            raise LakehouseDescriptorError(f"{ref_where} must be an object {{name, endpoint}}")
        _non_empty_string(ref["name"], field="name", source=ref_where)
        if not isinstance(ref["endpoint"], str) or not _ENDPOINT.fullmatch(ref["endpoint"]):
            raise LakehouseDescriptorError(f"{ref_where}: endpoint must be an https URL, got {ref['endpoint']!r}")
        references.append(TermReference(ref["name"], ref["endpoint"]))
    return GlossaryTerm(
        glossary=glossary,
        name=name,
        display_name=display_name,
        description=description,
        synonyms=_string_list(entry["synonyms"], field="synonyms", where=where) if "synonyms" in entry else (),
        status=status,
        related_terms=parse_term_refs(entry["relatedTerms"], field="relatedTerms", where=where)
        if "relatedTerms" in entry
        else (),
        references=tuple(references),
        parent=parent,
    )


def parse_glossary(document: object, *, path: str | Path = "glossary.yml") -> Glossary:
    """Validate one document's shape. Cross-term and cross-file rules are in ``glossary_errors``."""
    source = str(path)
    if not isinstance(document, Mapping):
        raise LakehouseDescriptorError(f"{source}: descriptor must contain a YAML object")
    if document.get("apiVersion") != GLOSSARY_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: unsupported apiVersion {document.get('apiVersion')!r}; expected {GLOSSARY_API_VERSION!r}"
        )
    if document.get("kind") != GLOSSARY_KIND:
        raise LakehouseDescriptorError(f"{source}: kind must be {GLOSSARY_KIND!r}")
    unexpected = sorted(set(document) - _GLOSSARY_FIELDS)
    if unexpected:
        raise LakehouseDescriptorError(f"{source}: must not contain {unexpected!r}")
    name = _identifier(document.get("name"), field="name", source=source)
    display_name = _non_empty_string(document.get("displayName"), field="displayName", source=source)
    description = _non_empty_string(document.get("description"), field="description", source=source)
    terms = document.get("terms")
    if not isinstance(terms, list) or not terms:
        raise LakehouseDescriptorError(f"{source}: terms must be a non-empty array")
    return Glossary(
        path=Path(path),
        name=name,
        display_name=display_name,
        description=description,
        terms=tuple(_term(name, term, index, source=source) for index, term in enumerate(terms)),
        owner=_entity_ref(document["owner"], field="owner", where=source) if "owner" in document else None,
    )


def _load_yaml(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise LakehouseDescriptorError(f"{path}: invalid YAML: {exc}") from exc


def load_glossary_descriptor(path: str | Path) -> Glossary:
    return parse_glossary(_load_yaml(Path(path)), path=path)


def glossary_paths(lakehouse_root: str | Path) -> list[Path]:
    return sorted(Path(lakehouse_root).glob("glossaries/*.yml"))


def discover_glossaries(lakehouse_root: str | Path) -> tuple[Glossary, ...]:
    """Every ``glossaries/*.yml`` under the lakehouse root; none is listed in ``governance.yml``."""
    return tuple(load_glossary_descriptor(path) for path in glossary_paths(lakehouse_root))


def rel_path(path: Path, root: Path | None) -> str:
    return str(path.relative_to(root)) if root and path.is_relative_to(root) else str(path)


def glossary_errors(glossaries: tuple[Glossary, ...], *, root: Path | None = None) -> list[str]:
    """Cross-file and cross-term errors, each naming the file and the term path.

    Names collide case-insensitively: OpenMetadata compares FQNs that way for
    uniqueness, so `NetRevenue` and `netrevenue` cannot both be published.
    """

    def label(glossary: Glossary) -> str:
        return rel_path(glossary.path, root)

    errors: list[str] = []
    seen_glossaries: dict[str, Glossary] = {}
    for glossary in glossaries:
        first = seen_glossaries.setdefault(glossary.name.lower(), glossary)
        if first is not glossary:
            errors.append(f"{label(glossary)}: glossary {glossary.name!r} is already declared in {label(first)}")
    for glossary in glossaries:
        where = label(glossary)
        by_name: dict[str, GlossaryTerm] = {}
        for term in glossary.terms:
            if term.name.lower() in by_name:
                errors.append(
                    f"{where}: terms[{term.name}]: collides with term {by_name[term.name.lower()].name!r} "
                    "(term names are unique per glossary, ignoring case)"
                )
            else:
                by_name[term.name.lower()] = term
        names = {term.name for term in glossary.terms}
        for term in glossary.terms:
            if term.parent is not None and term.parent not in names:
                errors.append(f"{where}: terms[{term.name}].parent: {term.parent!r} is not a term of {glossary.name!r}")
            for index, ref in enumerate(term.related_terms):
                if ref == term.fqn:
                    errors.append(f"{where}: terms[{term.name}].relatedTerms[{index}]: a term cannot relate to itself")
        errors += _cycle_errors(glossary, where)
    known = {term.fqn for glossary in glossaries for term in glossary.terms}
    for glossary in glossaries:
        for term in glossary.terms:
            for index, ref in enumerate(term.related_terms):
                if ref != term.fqn and ref not in known:
                    errors.append(
                        f"{label(glossary)}: terms[{term.name}].relatedTerms[{index}]: {ref!r} is not a known term"
                    )
    return errors


def _cycle_errors(glossary: Glossary, where: str) -> list[str]:
    parents = {term.name: term.parent for term in glossary.terms}
    errors = []
    reported: set[str] = set()
    for start in parents:
        chain = [start]
        while (nxt := parents.get(chain[-1])) is not None and nxt in parents:
            if nxt in chain:
                loop = chain[chain.index(nxt) :]
                if not reported.intersection(loop):
                    errors.append(f"{where}: terms[{nxt}].parent: cycle {' -> '.join([*loop, nxt])}")
                    reported.update(loop)
                break
            chain.append(nxt)
    return errors


def unresolved_term_refs(refs: Mapping[str, tuple[str, ...]], glossaries: tuple[Glossary, ...]) -> list[str]:
    """One message per reference in ``refs`` (location -> term references) that names no known term."""
    known = {term.fqn for glossary in glossaries for term in glossary.terms}
    return [
        f"{location}: {ref!r} is not a known term"
        for location, terms in refs.items()
        for ref in terms
        if ref not in known
    ]


def read_column_term_links(gold_models: str | Path) -> tuple[ColumnTermLink, ...]:
    """Column links in the dbt ``<product>/**/*.yml`` files: ``columns[].meta.openlakeforge.glossaryTerms``."""
    root = Path(gold_models)
    links: list[ColumnTermLink] = []
    # Recursive, like `scan_product_dependencies`: a product may split its models into subfolders, and the
    # product is the top-level folder, not the file's immediate parent.
    files = [
        (product.name, path)
        for product in sorted(root.glob("[!_.]*"))
        if product.is_dir()
        for path in sorted([*product.rglob("*.yml"), *product.rglob("*.yaml")])
    ]
    for product_name, path in files:
        document = _load_yaml(path)
        models = document.get("models") if isinstance(document, Mapping) else None
        for model in models if isinstance(models, list) else ():
            if not isinstance(model, Mapping):
                continue
            columns = model.get("columns")
            if columns is not None and not isinstance(columns, list):
                raise LakehouseDescriptorError(f"{path}: models[{model.get('name')}].columns must be an array")
            for column in columns or ():
                meta = column.get("meta") if isinstance(column, Mapping) else None
                openlakeforge = meta.get("openlakeforge") if isinstance(meta, Mapping) else None
                if not isinstance(openlakeforge, Mapping) or "glossaryTerms" not in openlakeforge:
                    continue
                where = f"{path}: models[{model.get('name')}].columns[{column.get('name')}].meta.openlakeforge"
                links.append(
                    ColumnTermLink(
                        path=path,
                        product=product_name,
                        model=str(model.get("name")),
                        column=str(column.get("name")),
                        terms=parse_term_refs(openlakeforge["glossaryTerms"], field="glossaryTerms", where=where),
                    )
                )
    return tuple(links)


def term_index(glossaries: tuple[Glossary, ...]) -> dict[str, GlossaryTerm]:
    return {term.fqn: term for glossary in glossaries for term in glossary.terms}


def column_link_locations(links: tuple[ColumnTermLink, ...], *, root: Path | None = None) -> dict[str, tuple[str, ...]]:
    suffix = "meta.openlakeforge.glossaryTerms"
    return {
        f"{rel_path(link.path, root)}: models[{link.model}].columns[{link.column}].{suffix}": link.terms
        for link in links
    }
