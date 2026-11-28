"""`lakehouse_code/glossaries/*.yml` and every term reference to them, offline."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml
from openlakeforge_core import LakehouseDescriptorError, load_governance_descriptor
from openlakeforge_core.glossary import (
    column_link_locations,
    glossary_errors,
    glossary_paths,
    parse_glossary,
    read_column_term_links,
    rel_path,
    unresolved_term_refs,
)

from olf.common.contracts_check._report import CheckResult


def _check_glossary_conformance(repo_root: Path, *, schema_root: Path | None = None) -> CheckResult:
    """Glossaries must match their JSON Schema and the canonical model, have unique and acyclic terms, and every
    `glossaryTerms` reference (governance.yml, dbt column `meta`) must resolve. A reference on a `mart_` column is a
    warning: the term belongs on the fact or dimension column that holds it. Offline; nothing reads a cluster."""
    name = "glossary_conformance"
    code = repo_root / "lakehouse_code"
    paths = glossary_paths(code)
    errors: list[str] = []
    glossaries = []
    if paths:
        schema_path = (schema_root or repo_root / "docs/schema") / "glossary.schema.json"
        validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    for path in paths:
        label = rel_path(path, repo_root)
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{label}: invalid YAML: {exc}")
            continue
        errors += [
            f"{label}: schema violation at {'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
            f"{error.message}"
            for error in validator.iter_errors(document)
        ]
        try:
            glossaries.append(parse_glossary(document, path=path))
        except LakehouseDescriptorError as exc:
            errors.append(str(exc).replace(str(path), label, 1))
    errors += glossary_errors(tuple(glossaries), root=repo_root)

    refs: dict[str, tuple[str, ...]] = {}
    governance = code / "governance.yml"
    if governance.is_file():
        try:
            document = load_governance_descriptor(governance)
        except LakehouseDescriptorError:
            document = {"domains": []}  # governance_conformance reports it
        for domain in document["domains"]:
            refs[f"lakehouse_code/governance.yml: domains[{domain['name']}].glossaryTerms"] = tuple(
                domain.get("glossaryTerms", ())
            )
            for product in domain.get("products", []):
                where = f"lakehouse_code/governance.yml: domains[{domain['name']}].products[{product['id']}]"
                refs[f"{where}.glossaryTerms"] = tuple(product.get("glossaryTerms", ()))
    warnings: list[str] = []
    try:
        links = read_column_term_links(code / "gold" / "models")
    except LakehouseDescriptorError as exc:
        links = ()
        errors.append(str(exc))
    refs |= column_link_locations(links, root=repo_root)
    warnings = [
        f"{rel_path(link.path, repo_root)}: models[{link.model}].columns[{link.column}]: glossaryTerms on a mart "
        "column; link the fact or dimension column that holds the term instead"
        for link in links
        if link.is_mart
    ]
    errors += unresolved_term_refs({location: terms for location, terms in refs.items() if terms}, tuple(glossaries))

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors), warnings=warnings)
    detail = f"{len(glossaries)} glossaries, {sum(len(g.terms) for g in glossaries)} terms" if glossaries else ""
    return CheckResult(name, ok=True, detail=detail or "no glossaries", warnings=warnings)
