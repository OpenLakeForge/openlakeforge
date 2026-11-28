"""The Full e2e's check of the Ossie models published to OpenMetadata (#920)."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from openlakeforge_core import SemanticMetric, SemanticModel
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError
from openlakeforge_services.semantic_publish import metric_terms, metric_type

from olf.common import config, log
from olf.infra.e2e._assertions import _admin_login, _project, openmetadata_client
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ
from olf.infra.e2e._trino import stage_catalog_name

_METRIC_FIELDS = "owners,tags,domains,dataProducts,extension"


def _get(client: OpenMetadataClient, collection: str, fqn: str, fields: str) -> Mapping[str, Any]:
    entity = client.request(
        "GET",
        f"/api/v1/{collection}/name/{urllib.parse.quote(fqn, safe='')}",
        params={"fields": fields},
        ok_statuses=(200, 404),
    )
    if not entity.get("id"):
        raise E2EError(f"OpenMetadata holds no {collection} {fqn!r}")
    return entity


def semantic_problems(
    client: OpenMetadataClient,
    models: Sequence[SemanticModel],
    *,
    stage: str,
    gold_schema_fqns: Mapping[str, str],
    column_terms: Mapping[str, Mapping[tuple[str, str], Sequence[str]]],
) -> list[str]:
    """What the published metrics, lineage and constraints of `stage` lack against `models`."""
    problems: list[str] = []
    # A dataset may read a public model of a product this one depends on, so each resolves by its own schema.
    schemas = {fqn.rpartition(".")[2]: fqn for fqn in gold_schema_fqns.values()}
    for model in models:
        tables = {
            dataset.name: _get(client, "tables", f"{schemas[dataset.schema]}.{dataset.table}", "tableConstraints")
            for dataset in model.datasets
        }
        for dataset in model.datasets:
            held = tables[dataset.name].get("tableConstraints") or []
            if dataset.primary_key and not any(
                c.get("constraintType") == "PRIMARY_KEY" and c.get("columns") == list(dataset.primary_key) for c in held
            ):
                problems.append(f"{dataset.table}: no PRIMARY_KEY on {list(dataset.primary_key)} (has {held})")
        for relationship in model.relationships:
            held = tables[relationship.from_dataset].get("tableConstraints") or []
            if not any(
                c.get("constraintType") == "FOREIGN_KEY"
                and c.get("columns") == list(relationship.from_columns)
                and c.get("relationshipType") == "MANY_TO_ONE"
                for c in held
            ):
                problems.append(f"{relationship.name}: no FOREIGN_KEY on {relationship.from_dataset} (has {held})")
        for declared in model.metrics:
            terms, _why = metric_terms(model, declared, column_terms.get(model.product, {}))
            problems += _metric_problems(client, declared, stage, tables, bool(terms))
    return problems


def _metric_problems(
    client: OpenMetadataClient,
    declared: SemanticMetric,
    stage: str,
    tables: Mapping[str, Mapping[str, Any]],
    tagged: bool,
) -> list[str]:
    fqn = f"{declared.name}_{stage}"
    metric = _get(client, "metrics", fqn, _METRIC_FIELDS)
    problems: list[str] = []
    expression = metric.get("metricExpression") or {}
    if expression.get("language") != "SQL" or not expression.get("code"):
        problems.append(f"{fqn}: no SQL expression")
    if metric.get("metricType") != metric_type(declared.expression):
        problems.append(f"{fqn}: metricType {metric.get('metricType')!r}")
    for field in ("domains", "dataProducts", "owners"):
        if not metric.get(field):
            problems.append(f"{fqn}: no {field}")
    if tagged and not any(tag.get("source") == "Glossary" for tag in metric.get("tags") or []):
        problems.append(f"{fqn}: no glossary tag")
    extension = metric.get("extension") or {}
    for key in ("olfManaged", "olfStage", "olfOssieVersion", "olfSourceFile", "olfContentHash"):
        if not extension.get(key):
            problems.append(f"{fqn}: no extension {key}")
    graph = client.request(
        "GET", f"/api/v1/lineage/metric/{metric['id']}", params={"upstreamDepth": 1, "downstreamDepth": 0}
    )
    upstream = {edge.get("fromEntity") for edge in graph.get("upstreamEdges") or []}
    for dataset_name, _field in declared.references:
        if tables[dataset_name]["id"] not in upstream:
            problems.append(f"{fqn}: no lineage edge from {dataset_name}")
    return problems


def check_openmetadata_semantic_publication(cfg: E2EConfig) -> None:
    """After the crawl has given the Gold tables their columns: a deploy publishes the Ossie models, the published
    state holds what Git declares, and a further deploy writes nothing and leaves an empty drift report."""
    from olf.infra.openmetadata._drift import drift_report, redeploy_writes

    log.step("Publishing the Ossie semantic models and checking them in OpenMetadata...")
    environ = cfg_environ(cfg)
    stage = stage_catalog_name(cfg).removeprefix("lakehouse_")
    gold_schemas = json.loads(config.env("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", "{}", environ=environ))
    column_terms = {
        product.id: {
            column: tuple(term.fqn for term in terms)
            for column, terms in cfg.inventory.column_terms(product.id).items()
        }
        for product in cfg.inventory.products
    }
    with openmetadata_client(cfg) as (client, token):
        login = _admin_login(cfg, token)
        try:
            # The crawl gave the tables their columns, so this deploy publishes the key constraints.
            first = redeploy_writes(environ, client, project=_project(cfg), **login)
            log.info(f"Semantic publication wrote {len(first)} request(s)")
            problems = semantic_problems(
                client,
                cfg.inventory.semantic_inventory.models,
                stage=stage,
                gold_schema_fqns=gold_schemas,
                column_terms=column_terms,
            )
            repeat = redeploy_writes(environ, client, project=_project(cfg), **login)
            drift = drift_report(environ, client, project=_project(cfg), **login)
        except (OpenMetadataError, E2EError) as exc:
            raise E2EError(str(exc)) from exc
    if repeat:
        problems.append("a repeat deploy wrote:\n" + "\n".join(repeat))
    if drift:
        problems.append("drift report after publication:\n" + "\n".join(i.line() for i in drift))
    if problems:
        raise E2EError("Published Ossie models differ from Git:\n" + "\n".join(problems))
