"""Publishes a stage's Ossie semantic models to OpenMetadata 1.13.6 as Metrics, lineage and table constraints.

What lands where is the table in `docs/reference/semantic-models.md`. Every write goes through the reconciler (create
with the ownership marker, then PATCH only what differs), so a repeat deploy of the same revision writes nothing, and
nothing is ever deleted: a metric removed from Git is reported as `stale` by `SemanticPublisher.drift`.

OpenMetadata keeps one Metric namespace for every stage, so the stage is part of the metric's name and is recorded in
its `olfStage` property.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openlakeforge_core import SemanticDataset, SemanticMetric, SemanticModel

from .drift import CHANGED, MISSING, STALE, UNRESOLVED, DriftItem, stage_of
from .openmetadata import OpenMetadataError, fqn_part
from .ownership import MARKER_DESCRIPTION, MARKER_PROPERTY, marker_of, patch_operations
from .progress import SILENT, Progress

if TYPE_CHECKING:
    from .governance import MetadataConfig, MetadataReconciler

__all__ = ["SemanticPublisher", "metric_type", "metric_name", "metric_terms"]

METRIC_FIELDS = "owners,tags,domains,dataProducts,extension"
STAGE_PROPERTY = "olfStage"
# The custom properties a Metric carries besides the ownership marker: where the definition came from, and the
# `ai_context` OpenMetadata has no native field for (the agent read API serves it whole).
METRIC_PROPERTIES = {
    MARKER_PROPERTY: MARKER_DESCRIPTION,
    STAGE_PROPERTY: "The stage whose deploy published this metric.",
    "olfOssieVersion": "Apache Ossie version of the semantic model the metric is defined in.",
    "olfSourceFile": "The semantic model file in the project, relative to lakehouse_code/'s parent.",
    "olfContentHash": "`sha256:` and the hash of that file's bytes.",
    "olfAiInstructions": "The metric's Ossie ai_context instructions.",
    "olfAiSynonyms": "The metric's Ossie ai_context synonyms, comma separated.",
}
# Outer aggregates a Metric type exists for; any other expression (a ratio, a CASE, two aggregates) is OTHER.
_AGGREGATES = {
    "SUM": "SUM",
    "COUNT": "COUNT",
    "AVG": "AVERAGE",
    "MIN": "MIN",
    "MAX": "MAX",
    "MEDIAN": "MEDIAN",
    "STDDEV": "STANDARD_DEVIATION",
    "STDDEV_SAMP": "STANDARD_DEVIATION",
    "STDDEV_POP": "STANDARD_DEVIATION",
    "VARIANCE": "VARIANCE",
    "VAR_SAMP": "VARIANCE",
    "VAR_POP": "VARIANCE",
}
_CALL = re.compile(r"\s*([A-Za-z_]+)\s*\(")
_AI_PROPERTIES = ("olfAiInstructions", "olfAiSynonyms")
_OLF_CONSTRAINTS = ("PRIMARY_KEY", "UNIQUE", "FOREIGN_KEY")


def metric_type(expression: str) -> str:
    """The OpenMetadata `metricType` of an expression: its outer aggregate, or OTHER when it is not one call."""
    call = _CALL.match(expression)
    if not call or call.group(1).upper() not in _AGGREGATES:
        return "OTHER"
    depth = 0
    quoted = False
    for index in range(call.end() - 1, len(expression)):
        char = expression[index]
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth == 0:
                return _AGGREGATES[call.group(1).upper()] if not expression[index + 1 :].strip() else "OTHER"
    return "OTHER"


def metric_name(metric: SemanticMetric, stage: str) -> str:
    return f"{metric.name}_{stage}"


def metric_terms(
    model: SemanticModel, metric: SemanticMetric, column_terms: Mapping[tuple[str, str], Sequence[str]]
) -> tuple[tuple[str, ...], str]:
    """The `<glossary>.<Term>` references a metric is tagged with, and why none when it has none.

    Ossie 0.1.1 has no term field, so the terms come from the dbt columns the expression reads: exactly one tagged
    column gives its terms, and several tagged columns with different terms are ambiguous.
    """
    tables = {dataset.name: dataset.table for dataset in model.datasets}
    tagged = {
        (dataset, column): tuple(column_terms[(tables[dataset], column)])
        for dataset, column in metric.references
        if dataset in tables and column_terms.get((tables[dataset], column))
    }
    if not tagged:
        return (), "no column it reads carries a glossary term"
    distinct = set(tagged.values())
    if len(distinct) > 1:
        return (), "the columns it reads carry different glossary terms: " + ", ".join(
            f"{d}.{c} -> {', '.join(t)}" for (d, c), t in tagged.items()
        )
    return next(iter(distinct)), ""


def _ref(entity: Mapping[str, Any], kind: str) -> dict[str, str]:
    return {"id": entity["id"], "type": kind}


def _tag(term_fqn: str) -> dict[str, str]:
    return {"tagFQN": term_fqn, "source": "Glossary", "labelType": "Manual", "state": "Confirmed"}


def _constraint_key(constraint: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        constraint.get("constraintType"),
        tuple(constraint.get("columns") or ()),
        tuple(constraint.get("referredColumns") or ()),
        constraint.get("relationshipType"),
    )


@dataclass(frozen=True)
class _Plan:
    """Everything one metric needs, resolved against what OpenMetadata holds."""

    model: SemanticModel
    metric: SemanticMetric
    fqn: str
    create: Mapping[str, Any]
    owned: Mapping[str, Any]
    extension: Mapping[str, str]
    expression: Mapping[str, str]
    tags: tuple[str, ...]
    sources: tuple[tuple[str, str], ...]


class SemanticPublisher:
    def __init__(
        self,
        reconciler: MetadataReconciler,
        config: MetadataConfig,
        models: Sequence[SemanticModel],
        column_terms: Mapping[tuple[str, str], Sequence[str]],
        domain_of: Mapping[str, str],
        gold_namespaces: Mapping[str, str],
        *,
        progress: Progress = SILENT,
    ):
        self._reconciler = reconciler
        self._client = reconciler.client
        self._config = config
        self._models = tuple(models)
        self._column_terms = column_terms
        self._domain_of = domain_of
        self._stage = stage_of(config)
        self._progress = progress
        # The logical namespace a dataset names (`sales_core_gold`) -> the schema the provider contract gives it, which
        # carries a stage prefix on some catalogs.
        self._gold_schemas = {
            namespace: config.catalog_gold_schema_fqns[product]
            for product, namespace in gold_namespaces.items()
            if product in config.catalog_gold_schema_fqns
        }

    # --- resolving ---------------------------------------------------------------------------------------------

    def _table_fqn(self, dataset: SemanticDataset) -> str | None:
        schema = self._gold_schemas.get(dataset.schema)
        return f"{schema}.{dataset.table}" if schema else None

    def _plans(self) -> tuple[list[_Plan], list[DriftItem]]:
        plans: list[_Plan] = []
        unresolved: list[DriftItem] = []
        for model in self._models:
            domain = self._reconciler.lookup("domains", self._domain_of.get(model.product, ""))
            product = self._reconciler.lookup("dataProducts", model.product, "owners,domains,extension")
            if domain is None or product is None:
                what = "domain" if domain is None else "data product"
                for metric in model.metrics:
                    detail = f"{what} of product {model.product!r} not found in OpenMetadata"
                    unresolved.append(
                        DriftItem(UNRESOLVED, "metric", metric_name(metric, self._stage), self._stage, detail)
                    )
                continue
            datasets = {dataset.name: dataset for dataset in model.datasets}
            for metric in model.metrics:
                plan = self._plan(model, metric, datasets, domain, product)
                twin = next((other for other in plans if other.fqn == plan.fqn), None)
                if twin:
                    # One OpenMetadata entity per stage and name: two definitions would overwrite each other.
                    detail = f"defined by both {twin.model.file} and {model.file}"
                    unresolved.append(DriftItem(UNRESOLVED, "metric", plan.fqn, self._stage, detail))
                else:
                    plans.append(plan)
        return plans, unresolved

    def _plan(
        self,
        model: SemanticModel,
        metric: SemanticMetric,
        datasets: Mapping[str, SemanticDataset],
        domain: Mapping[str, Any],
        product: Mapping[str, Any],
    ) -> _Plan:
        name = metric_name(metric, self._stage)
        kind = metric_type(metric.expression)
        owners = [_ref(owner, owner.get("type", "team")) for owner in product.get("owners") or []]
        terms, why_not = metric_terms(model, metric, self._column_terms)
        if why_not:
            self._progress.info(f"Metric {name}: no glossary tag, because {why_not}.")
        expression = {"language": "SQL", "code": metric.expression}
        ai = metric.ai_context
        extension = {
            STAGE_PROPERTY: self._stage,
            "olfOssieVersion": model.version,
            "olfSourceFile": model.file,
            "olfContentHash": model.content_hash,
            **({"olfAiInstructions": ai.instructions} if ai and ai.instructions else {}),
            **({"olfAiSynonyms": ", ".join(ai.synonyms)} if ai and ai.synonyms else {}),
        }
        display = metric.name
        owned = {
            "displayName": display,
            "description": metric.description,
            "metricType": kind,
            "owners": owners,
            "domains": [_ref(domain, "domain")],
            "dataProducts": [_ref(product, "dataProduct")],
        }
        create = {
            "name": name,
            "displayName": display,
            "description": metric.description,
            "metricExpression": expression,
            "metricType": kind,
            "owners": owners,
            "domains": [domain["fullyQualifiedName"]],
            "dataProducts": [product["fullyQualifiedName"]],
        }
        sources = []
        for dataset_name, _field in metric.references:
            table = datasets[dataset_name] if dataset_name in datasets else None
            fqn = self._table_fqn(table) if table else None
            if fqn and (dataset_name, fqn) not in sources:
                sources.append((dataset_name, fqn))
        return _Plan(model, metric, name, create, owned, extension, expression, terms, tuple(sources))

    def _present_terms(self, plan: _Plan) -> tuple[str, ...]:
        """The plan's glossary terms OpenMetadata holds; a tag naming a missing term would be refused."""
        present = tuple(t for t in plan.tags if self._reconciler.lookup("glossaryTerms", t, "") is not None)
        if len(present) < len(plan.tags):
            self._progress.warn(
                f"Metric {plan.fqn}: glossary term(s) {sorted(set(plan.tags) - set(present))} are not in "
                "OpenMetadata yet; the metric is tagged by the next deploy once the glossary is published."
            )
        return present

    @staticmethod
    def _extra_operations(current: Mapping[str, Any], plan: _Plan, tags: Sequence[str]) -> list[dict[str, Any]]:
        """The expression and glossary tags, which the generic patch cannot compare: the server escapes the code's
        quotes and adds display fields to each tag."""
        operations: list[dict[str, Any]] = []
        have = current.get("metricExpression") or {}
        if have.get("language") != "SQL" or html.unescape(have.get("code") or "") != plan.expression["code"]:
            operations.append(
                {"op": "replace" if have else "add", "path": "/metricExpression", "value": dict(plan.expression)}
            )
        held_extension = current.get("extension") or {}
        for key in _AI_PROPERTIES:
            if key in held_extension and key not in plan.extension:
                operations.append({"op": "remove", "path": f"/extension/{key}"})
        held = current.get("tags") or []
        present = {tag.get("tagFQN") for tag in held}
        missing = [tag for tag in tags if tag not in present]
        if missing and held:
            operations += [{"op": "add", "path": "/tags/-", "value": _tag(tag)} for tag in missing]
        elif missing:
            operations.append({"op": "add", "path": "/tags", "value": [_tag(tag) for tag in missing]})
        return operations

    # --- publishing --------------------------------------------------------------------------------------------

    def ensure_properties(self) -> None:
        """Define the custom properties a Metric carries; writes only the ones missing."""
        client = self._client
        definition = client.request("GET", "/api/v1/metadata/types/name/metric", params={"fields": "customProperties"})
        held = {prop.get("name") for prop in definition.get("customProperties") or []}
        string_type_id = ""
        for name, description in METRIC_PROPERTIES.items():
            if name in held:
                continue
            string_type_id = string_type_id or client.request("GET", "/api/v1/metadata/types/name/string")["id"]
            client.request(
                "PUT",
                f"/api/v1/metadata/types/{definition['id']}",
                json_body={
                    "name": name,
                    "description": description,
                    "propertyType": {"id": string_type_id, "type": "type"},
                },
                ok_statuses=(200, 201),
            )
            self._progress.info(f"Defined the {name} custom property on metric")

    def validate(self) -> None:
        """Refuse what needs no OpenMetadata read to see is wrong, so a deploy fails before its first write: two
        models defining one metric publish to the same entity."""
        seen: dict[str, str] = {}
        for model in self._models:
            for metric in model.metrics:
                name = metric_name(metric, self._stage)
                if name in seen:
                    raise OpenMetadataError(f"metric {name} is defined by both {seen[name]} and {model.file}")
                seen[name] = model.file

    def publish(self) -> None:
        for model in self._models:
            self._report_unsupported(model)
        plans, unresolved = self._plans()
        if unresolved:
            raise OpenMetadataError("\n".join(item.line() for item in unresolved))
        if not self._models:
            return
        self.ensure_properties()
        for plan in plans:
            self._publish_metric(plan)
        self._publish_constraints()

    def _publish_metric(self, plan: _Plan) -> None:
        tags = self._present_terms(plan)
        create = {**plan.create, **({"tags": [_tag(tag) for tag in tags]} if tags else {})}
        entity = self._reconciler.reconcile(
            "metrics", plan.fqn, create, plan.owned, fields=METRIC_FIELDS, extension=plan.extension
        )
        operations = self._extra_operations(entity, plan, tags)
        if operations:
            self._client.request(
                "PATCH",
                f"/api/v1/metrics/{entity['id']}",
                json_body=operations,
                content_type="application/json-patch+json",
            )
        self._progress.info(f"Reconciled OpenMetadata metric: {plan.fqn}")
        for _dataset, table_fqn in plan.sources:
            table = self._reconciler.lookup("tables", table_fqn, "")
            if table is None:
                self._progress.warn(f"Metric {plan.fqn}: table {table_fqn} is not in OpenMetadata yet; no lineage.")
            elif self._lineage_missing(entity["id"], table["id"]):
                self._client.request(
                    "PUT",
                    "/api/v1/lineage",
                    json_body={
                        "edge": {
                            "fromEntity": {"id": table["id"], "type": "table"},
                            "toEntity": {"id": entity["id"], "type": "metric"},
                            "lineageDetails": {"source": "Manual", "description": "computed from"},
                        }
                    },
                )

    def _lineage_missing(self, metric_id: str, table_id: str) -> bool:
        graph = self._client.request(
            "GET", f"/api/v1/lineage/metric/{metric_id}", params={"upstreamDepth": 1, "downstreamDepth": 0}
        )
        held = {edge.get("fromEntity") for edge in graph.get("upstreamEdges") or []}
        return table_id not in held

    # --- constraints -------------------------------------------------------------------------------------------

    def _wanted_constraints(self) -> dict[str, list[dict[str, Any]]]:
        """Table FQN -> the key constraints the models declare on it, together: two models may describe one table.
        A declared table with no key maps to an empty list, so a key removed from Git is removed there too.
        Ossie relationships become FOREIGN_KEY here and are never published as lineage."""
        wanted: dict[str, list[dict[str, Any]]] = {}

        def add(fqn: str, constraint: dict[str, Any]) -> None:
            if _constraint_key(constraint) not in {_constraint_key(held) for held in wanted[fqn]}:
                wanted[fqn].append(constraint)

        for model in self._models:
            tables = {dataset.name: self._table_fqn(dataset) for dataset in model.datasets}
            for dataset in model.datasets:
                fqn = tables[dataset.name]
                if fqn is None:
                    continue
                wanted.setdefault(fqn, [])
                if dataset.primary_key:
                    add(fqn, {"constraintType": "PRIMARY_KEY", "columns": list(dataset.primary_key)})
                for key in dataset.unique_keys:
                    add(fqn, {"constraintType": "UNIQUE", "columns": list(key)})
            for relationship in model.relationships:
                source, target = tables.get(relationship.from_dataset), tables.get(relationship.to_dataset)
                if source and target:
                    add(
                        source,
                        {
                            "constraintType": "FOREIGN_KEY",
                            "columns": list(relationship.from_columns),
                            "referredColumns": [f"{target}.{fqn_part(column)}" for column in relationship.to_columns],
                            "relationshipType": "MANY_TO_ONE",
                        },
                    )
        return wanted

    def _constraint_operations(self, table: Mapping[str, Any], wanted: Sequence[Mapping[str, Any]]) -> list[dict]:
        held = table.get("tableConstraints") or []
        ours = [c for c in held if c.get("constraintType") in _OLF_CONSTRAINTS]
        if {_constraint_key(c) for c in ours} == {_constraint_key(c) for c in wanted} and len(ours) == len(wanted):
            return []
        # Keys are OLF's on a Gold table; sort and cluster keys a person set stay.
        kept = [c for c in held if c.get("constraintType") not in _OLF_CONSTRAINTS]
        return [{"op": "replace" if held else "add", "path": "/tableConstraints", "value": [*kept, *wanted]}]

    def _constrained_tables(self) -> tuple[list[tuple[str, Mapping[str, Any], list[dict]]], list[tuple[str, str]]]:
        """Each declared table OpenMetadata holds with its crawled columns and the constraints to write on it, and
        the `(table, reason)` of those that cannot be reconciled yet."""
        found = []
        waiting = []
        for fqn, wanted in self._wanted_constraints().items():
            table = self._reconciler.lookup("tables", fqn, "tableConstraints,columns")
            columns = {str(column["name"]).lower() for column in (table or {}).get("columns") or []}
            used = {c.lower() for constraint in wanted for c in constraint["columns"]}
            if table is None or not used <= columns:
                reason = "is not in OpenMetadata yet" if table is None else f"lacks column(s) {sorted(used - columns)}"
                self._progress.warn(f"Table {fqn} {reason}; its Ossie key constraints are not published yet.")
                waiting.append((fqn, reason))
                continue
            # OpenMetadata refuses the whole PATCH when a foreign key's referred table or column is missing, which
            # would also block the table's other keys, so such a key waits for the referred table's crawl.
            ready = []
            for constraint in wanted:
                lacking = self._missing_referred(constraint)
                if lacking:
                    self._progress.warn(f"Table {fqn}: foreign key to {lacking} waits for its crawl.")
                    waiting.append((fqn, f"has a foreign key to {lacking}, which is not in OpenMetadata yet"))
                else:
                    ready.append(constraint)
            found.append((fqn, table, ready))
        return found, waiting

    def _missing_referred(self, constraint: Mapping[str, Any]) -> str | None:
        """The first `referredColumns` entry whose table or column OpenMetadata does not hold yet."""
        for referred in constraint.get("referredColumns") or ():
            # `fqn_part` quotes a column name that contains a dot, so a quoted last segment is one column.
            if referred.endswith('"') and '."' in referred:
                table_fqn, _, column = referred[:-1].rpartition('."')
            else:
                table_fqn, _, column = referred.rpartition(".")
            table = self._reconciler.lookup("tables", table_fqn, "columns")
            columns = {str(c["name"]).lower() for c in (table or {}).get("columns") or []}
            if column.strip('"').lower() not in columns:
                return str(referred)
        return None

    def _publish_constraints(self) -> None:
        for fqn, table, wanted in self._constrained_tables()[0]:
            operations = self._constraint_operations(table, wanted)
            if operations:
                self._client.request(
                    "PATCH",
                    f"/api/v1/tables/{table['id']}",
                    json_body=operations,
                    content_type="application/json-patch+json",
                )
                self._progress.info(f"Reconciled OpenMetadata table constraints: {fqn}")

    # --- reporting ---------------------------------------------------------------------------------------------

    def _report_unsupported(self, model: SemanticModel) -> None:
        """What the model declares that OpenMetadata has no field for, so it is not silently dropped."""
        with_context: list[Any] = [model, *model.datasets, *model.relationships]
        with_context += [f for d in model.datasets for f in d.fields]
        counts = {
            "ai_context on the model, datasets, fields and relationships": sum(
                bool(i.ai_context) for i in with_context
            ),
            "time dimensions (is_time)": sum(f.is_time for d in model.datasets for f in d.fields),
            "field labels": sum(bool(f.label) for d in model.datasets for f in d.fields),
        }
        left_out = [f"{count} {what}" for what, count in counts.items() if count]
        if left_out:
            self._progress.info(
                f"Semantic model {model.name} ({model.file}): not published to OpenMetadata, served by the agent API: "
                + "; ".join(left_out)
            )

    # --- drift -------------------------------------------------------------------------------------------------

    def drift(self) -> list[DriftItem]:
        """What differs between the semantic models in Git and this stage's metrics, lineage and constraints."""
        stage = self._stage
        plans, items = self._plans()
        # Every metric Git declares, resolved or not: one whose product is missing in OpenMetadata is unresolved.
        declared = {metric_name(metric, stage) for model in self._models for metric in model.metrics}
        for plan in plans:
            current = self._reconciler.lookup("metrics", plan.fqn, METRIC_FIELDS)
            if current is None:
                items.append(DriftItem(MISSING, "metric", plan.fqn, stage))
                continue
            fields = [
                op["path"].lstrip("/")
                for op in patch_operations(current, {**plan.owned, "extension": {**plan.extension}})
            ]
            if marker_of(current) is None:
                fields.append(MARKER_PROPERTY)
            tags = tuple(t for t in plan.tags if self._reconciler.lookup("glossaryTerms", t, "") is not None)
            fields += [op["path"].split("/")[1] for op in self._extra_operations(current, plan, tags)]
            for _dataset, table_fqn in plan.sources:
                table = self._reconciler.lookup("tables", table_fqn, "")
                if table and self._lineage_missing(current["id"], table["id"]):
                    fields.append(f"lineage from {table_fqn}")
            if fields:
                items.append(DriftItem(CHANGED, "metric", plan.fqn, stage, ", ".join(dict.fromkeys(fields))))
        constrained, waiting = self._constrained_tables()
        for fqn, table, wanted in constrained:
            if self._constraint_operations(table, wanted):
                items.append(DriftItem(CHANGED, "table", fqn, stage, "tableConstraints"))
        # Never read as matching Git what cannot be compared yet.
        items += [DriftItem(UNRESOLVED, "table", fqn, stage, f"tableConstraints: table {why}") for fqn, why in waiting]
        for found in self._client.list_entities("/api/v1/metrics", {"fields": "extension"}):
            fqn = found.get("fullyQualifiedName") or found["name"]
            held_extension = found.get("extension") or {}
            if marker_of(found) and held_extension.get(STAGE_PROPERTY) == stage and fqn not in declared:
                items.append(DriftItem(STALE, "metric", fqn, stage, "marked as OpenLakeForge-managed, absent from Git"))
        return items
