"""Publishes the Git-managed business glossaries to OpenMetadata and attaches their terms.

Order matters: every glossary and term is created (and carries the `olfManaged` marker) before any
product, domain or column is tagged with one. All writes go through `MetadataReconciler.reconcile`
or a JSON Patch that only appends the missing glossary tags, so a repeat deploy of the same revision
writes nothing. Nothing is deleted: a term or link removed from Git is reported in
`GlossaryReport`, which is the hook the drift report (#1472) reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openlakeforge_core import LakehouseDescriptorError, load_lakehouse_inventory_from_descriptors
from openlakeforge_core.glossary import Glossary, GlossaryTerm
from openlakeforge_core.governance import EntityRef
from openlakeforge_core.inventory import LakehouseInventory

from .openmetadata import OpenMetadataError, OpenMetadataTransientError
from .ownership import marker_of

if TYPE_CHECKING:
    from .governance import MetadataReconciler

_TERM_FIELDS = "owners,relatedTerms,extension"
_RELATION = "relatedTo"


def om_fqns(glossaries: tuple[Glossary, ...]) -> dict[str, str]:
    """`<glossary>.<Term>` (the reference Git uses, stable when a term moves) -> the FQN OpenMetadata gives it, which
    includes the parent terms: `sales.NetRevenue.Emea` for a term `Emea` under `NetRevenue`."""
    fqns: dict[str, str] = {}
    for glossary in glossaries:
        parents = {term.name: term.parent for term in glossary.terms}
        for term in glossary.terms:
            chain, current = [term.name], term.parent
            while current is not None:
                chain.append(current)
                current = parents.get(current)
            fqns[term.fqn] = ".".join([glossary.name, *reversed(chain)])
    return fqns


def term_tag(term_fqn: str) -> dict[str, str]:
    return {"tagFQN": term_fqn, "source": "Glossary", "labelType": "Manual", "state": "Confirmed"}


@dataclass
class GlossaryReport:
    """What a publish found but deliberately did not change.

    `pending_columns` are links whose table or column the Trino crawl has not produced yet. The
    `removed_terms` are OLF-marked terms no longer in Git; `removed_links` are Git terms tagged on an entity
    whose Git declaration no longer lists them (the tag cannot say who set it). OLF never deletes either.
    """

    pending_columns: list[str] = field(default_factory=list)
    removed_terms: list[str] = field(default_factory=list)
    removed_links: list[str] = field(default_factory=list)


class GlossaryPublisher:
    def __init__(self, reconciler: MetadataReconciler, inventory: LakehouseInventory):
        self._reconciler = reconciler
        self._client = reconciler.client
        self._progress = reconciler.progress
        self.inventory = inventory
        self.report = GlossaryReport()
        self._owner_ids: dict[tuple[str, str], str] = {}
        self._term_ids: dict[str, str] = {}
        self._om = om_fqns(inventory.glossaries)
        self._glossary_ids: dict[str, str] = {}

    @classmethod
    def from_descriptors(cls, reconciler: MetadataReconciler, descriptor_paths: list[Path]) -> GlossaryPublisher:
        try:
            inventory = load_lakehouse_inventory_from_descriptors(descriptor_paths[0], descriptor_paths[1:])
        except LakehouseDescriptorError as exc:
            raise OpenMetadataError(str(exc)) from exc
        return cls(reconciler, inventory)

    def _owner(self, ref: EntityRef) -> dict[str, str]:
        """The `{id, type}` of an existing user or team. Only a Group team can own entities on 1.13.6."""
        key = (ref.type, ref.ref)
        if key not in self._owner_ids:
            collection = "teams" if ref.type == "team" else "users"
            entity = self._reconciler.lookup(collection, ref.ref, "")
            if entity is None:
                raise OpenMetadataError(f"Glossary owner {ref.type} {ref.ref!r} does not exist in OpenMetadata.")
            if ref.type == "team" and entity.get("teamType") not in (None, "Group"):
                raise OpenMetadataError(
                    f"Glossary owner team {ref.ref!r} is of type {entity.get('teamType')}; only a Group team can own."
                )
            self._owner_ids[key] = entity["id"]
        return {"id": self._owner_ids[key], "type": ref.type}

    def publish(self) -> None:
        glossaries = self.inventory.glossaries
        for glossary in glossaries:
            self._publish_glossary(glossary)
        terms = [term for glossary in glossaries for term in glossary.terms]
        # Parents first, so a nested term's `parent` exists when it is created.
        ordered = sorted(terms, key=lambda term: self._depth(term, glossaries))
        for term in ordered:
            self._publish_term(term, first_pass=True)
        # `relatedTerms` can point forward or into another glossary, so it is written once every term exists.
        for term in ordered:
            self._publish_term(term, first_pass=False)
        self._collect_removed_terms(glossaries)

    @staticmethod
    def _depth(term: GlossaryTerm, glossaries: tuple[Glossary, ...]) -> int:
        parents = {t.name: t.parent for g in glossaries if g.name == term.glossary for t in g.terms}
        depth, current = 0, term.name
        while parents.get(current) is not None:
            current = parents[current]  # type: ignore[assignment]  # validated acyclic offline
            depth += 1
        return depth

    def _publish_glossary(self, glossary: Glossary) -> None:
        create: dict[str, Any] = {
            "name": glossary.name,
            "displayName": glossary.display_name,
            "description": glossary.description,
        }
        owned: dict[str, Any] = {"displayName": glossary.display_name, "description": glossary.description}
        if glossary.owner:
            owner = self._owner(glossary.owner)
            create["owners"] = [owner]
            owned["owners"] = [owner]
        entity = self._reconciler.reconcile("glossaries", glossary.name, create, owned, fields="owners,extension")
        self._glossary_ids[glossary.name] = entity["id"]
        self._progress.info(f"Reconciled OpenMetadata glossary: {glossary.name}")

    def _publish_term(self, term: GlossaryTerm, *, first_pass: bool) -> None:
        references = [{"name": ref.name, "endpoint": ref.endpoint} for ref in term.references]
        if first_pass:
            create: dict[str, Any] = {
                "glossary": term.glossary,
                "name": term.name,
                "displayName": term.display_name,
                "description": term.description,
                "synonyms": list(term.synonyms),
                "references": references,
            }
            owned: dict[str, Any] = {
                "displayName": term.display_name,
                "description": term.description,
                "synonyms": list(term.synonyms),
                "references": references,
            }
            if term.parent:
                create["parent"] = self._om[f"{term.glossary}.{term.parent}"]
        else:
            self._attach_related_terms(term)
            self._set_status(term)
            return
        entity = self._reconciler.reconcile("glossaryTerms", self._om[term.fqn], create, owned, fields=_TERM_FIELDS)
        self._term_ids[self._om[term.fqn]] = entity["id"]
        self._progress.info(f"Reconciled OpenMetadata glossary term: {term.fqn}")

    def _attach_related_terms(self, term: GlossaryTerm) -> None:
        """Add the related terms the term lacks. On 1.13.6 a relation is `{term: <reference>, relationType}`, not a
        bare reference, and a relation a person added is kept, so this only appends. Done once every term has an id,
        because a relation can point forward or into another glossary."""
        if not term.related_terms:
            return
        entity = self._reconciler.reconcile("glossaryTerms", self._om[term.fqn], {}, {}, fields=_TERM_FIELDS)
        have = entity.get("relatedTerms")
        held = {relation["term"]["id"] for relation in have or []}
        missing = [
            {"term": {"id": self._term_ids[self._om[ref]], "type": "glossaryTerm"}, "relationType": _RELATION}
            for ref in term.related_terms
            if self._term_ids[self._om[ref]] not in held
        ]
        if not missing:
            return
        operations: list[dict[str, Any]]
        if have:
            operations = [{"op": "add", "path": "/relatedTerms/-", "value": relation} for relation in missing]
        else:
            operations = [{"op": "add", "path": "/relatedTerms", "value": missing}]
        self._patch("glossaryTerms", entity["id"], operations)

    def _set_status(self, term: GlossaryTerm) -> None:
        """Patch the approval status. OpenMetadata applies transition rules (reviewers, its approval workflow), so a
        refusal is reported and the deploy goes on: the status is metadata, not something the links depend on."""
        if not term.status:
            return
        try:
            self._reconciler.reconcile(
                "glossaryTerms", self._om[term.fqn], {}, {"entityStatus": term.status}, fields=_TERM_FIELDS
            )
        except (OpenMetadataError, OpenMetadataTransientError) as exc:
            self._progress.warn(f"Glossary term {term.fqn}: status {term.status!r} was not applied: {exc}")

    def _collect_removed_terms(self, glossaries: tuple[Glossary, ...]) -> None:
        known = set(self._om.values())
        for glossary in glossaries:
            # The list endpoint filters by glossary id, not name.
            params = {"glossary": self._glossary_ids[glossary.name], "fields": "extension"}
            for entry in self._client.list_entities("/api/v1/glossaryTerms", params, 1000):
                fqn = entry.get("fullyQualifiedName", "")
                if marker_of(entry) and fqn not in known:
                    self.report.removed_terms.append(fqn)

    # --- attachment -----------------------------------------------------------

    def _append_tags(self, entity: Mapping[str, Any], path: str, wanted: list[str]) -> list[dict[str, Any]]:
        """JSON Patch adding the glossary tags `entity` (a tag-carrying object) lacks; never removes one."""
        have = entity.get("tags")
        present = {tag.get("tagFQN") for tag in have or []}
        missing = [term_tag(fqn) for fqn in wanted if fqn not in present]
        if not missing:
            return []
        if have is None:
            return [{"op": "add", "path": f"{path}/tags", "value": missing}]
        return [{"op": "add", "path": f"{path}/tags/-", "value": tag} for tag in missing]

    def _report_removed_links(self, where: str, entity: Mapping[str, Any], wanted: list[str]) -> None:
        for tag in entity.get("tags") or []:
            fqn = tag.get("tagFQN", "")
            if tag.get("source") == "Glossary" and fqn in self._term_ids and fqn not in wanted:
                self.report.removed_links.append(f"{where} -> {fqn}")

    def _patch(self, collection: str, entity_id: str, operations: list[dict[str, Any]]) -> None:
        if operations:
            self._client.request(
                "PATCH",
                f"/api/v1/{collection}/{entity_id}",
                json_body=operations,
                content_type="application/json-patch+json",
            )

    def tag_entity(self, collection: str, fqn: str, terms: tuple[str, ...]) -> None:
        """Attach a domain's or data product's declared terms as glossary-source tags."""
        entity = self._reconciler.lookup(collection, fqn, "tags")
        if entity is None:
            return
        self._report_removed_links(fqn, entity, [self._om[ref] for ref in terms])
        self._patch(collection, entity["id"], self._append_tags(entity, "", [self._om[ref] for ref in terms]))

    def tag_columns(self, product: str, schema_fqn: str) -> None:
        """Tag the columns of this stage's canonical tables with the terms the dbt `meta` declares.

        Only the declared column's tags are patched. A table or column the crawl has not produced is
        reported as pending, never created.
        """
        by_table: dict[str, dict[str, tuple[GlossaryTerm, ...]]] = {}
        for (model, column), terms in self.inventory.column_terms(product).items():
            by_table.setdefault(model, {})[column] = terms
        for model, columns in by_table.items():
            table_fqn = f"{schema_fqn}.{model}"
            table = self._reconciler.lookup("tables", table_fqn, "columns,tags")
            held = {col["name"].lower(): (index, col) for index, col in enumerate((table or {}).get("columns") or [])}
            operations: list[dict[str, Any]] = []
            for column, terms in columns.items():
                if column.lower() not in held:
                    self.report.pending_columns.append(f"{table_fqn}.{column}")
                    continue
                index, current = held[column.lower()]
                wanted = [self._om[term.fqn] for term in terms]
                operations += self._append_tags(current, f"/columns/{index}", wanted)
            # A column whose last declaration left Git keeps its tag; say so.
            for name, (_, current) in held.items():
                if name not in {column.lower() for column in columns}:
                    self._report_removed_links(f"{table_fqn}.{current['name']}", current, [])
            for column in columns:
                if column.lower() in held:
                    wanted = [self._om[term.fqn] for term in columns[column]]
                    self._report_removed_links(f"{table_fqn}.{column}", held[column.lower()][1], wanted)
            if table is not None:
                self._patch("tables", table["id"], operations)

    def domain_terms(self, domain: str) -> tuple[str, ...]:
        return next((d.glossary_terms for d in self.inventory.domains if d.name == domain), ())

    def product_terms(self, product: str) -> tuple[str, ...]:
        return next((p.glossary_terms for p in self.inventory.products if p.id == product), ())

    def log_report(self) -> None:
        for fqn in self.report.pending_columns:
            self._progress.warn(f"Glossary link pending, column not crawled yet: {fqn}")
        for fqn in self.report.removed_terms:
            self._progress.warn(f"Glossary term {fqn} is no longer in Git; left in place.")
        for link in self.report.removed_links:
            self._progress.warn(f"Glossary link {link} is no longer declared in Git; left in place.")
