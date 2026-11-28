"""Live check of glossary publication (#919): terms and links exist, and a second deploy writes nothing."""

from __future__ import annotations

import urllib.parse
from typing import Any

from openlakeforge_core import LakehouseInventory
from openlakeforge_services.glossary_publish import om_fqns
from openlakeforge_services.openmetadata import OpenMetadataClient

from olf.common import config, log
from olf.infra.e2e._assertions import openmetadata_client
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ


def _get(client: OpenMetadataClient, collection: str, fqn: str, fields: str) -> dict[str, Any]:
    path = f"/api/v1/{collection}/name/{urllib.parse.quote(fqn, safe='')}"
    entity = dict(client.request("GET", path, params={"fields": fields}, ok_statuses=(200, 404)))
    return entity if entity.get("id") else {}


def _tag_fqns(entity: dict[str, Any]) -> set[str]:
    return {tag["tagFQN"] for tag in entity.get("tags") or [] if tag.get("source") == "Glossary"}


def _targets(inventory: LakehouseInventory) -> list[tuple[str, str, str, set[str]]]:
    """(collection, fqn, fields, wanted term FQNs) for every entity a deploy tags."""
    om = om_fqns(inventory.glossaries)
    targets: list[tuple[str, str, str, set[str]]] = []
    for domain in inventory.domains:
        if domain.glossary_terms:
            targets.append(("domains", domain.name, "tags", {om[ref] for ref in domain.glossary_terms}))
        for product in domain.products:
            if product.glossary_terms:
                targets.append(("dataProducts", product.id, "tags", {om[ref] for ref in product.glossary_terms}))
    return targets


def _column_links(inventory: LakehouseInventory, database_fqn: str) -> dict[str, dict[str, set[str]]]:
    """table FQN -> column -> wanted term FQNs, on this stage's own Gold schemas."""
    om = om_fqns(inventory.glossaries)
    links: dict[str, dict[str, set[str]]] = {}
    for product in inventory.products:
        for (model, column), terms in inventory.column_terms(product.id).items():
            table = f"{database_fqn}.{product.gold_namespace}.{model}"
            links.setdefault(table, {})[column] = {om[term.fqn] for term in terms}
    return links


def _snapshot(client: OpenMetadataClient, inventory: LakehouseInventory, database_fqn: str) -> dict[str, Any]:
    """Entity versions: a write that changes anything bumps one, so equal snapshots mean nothing was written."""
    versions: dict[str, Any] = {}
    for glossary in inventory.glossaries:
        versions[f"glossary:{glossary.name}"] = _get(client, "glossaries", glossary.name, "").get("version")
    for fqn in om_fqns(inventory.glossaries).values():
        versions[f"term:{fqn}"] = _get(client, "glossaryTerms", fqn, "").get("version")
    for collection, fqn, fields, _ in _targets(inventory):
        versions[f"{collection}:{fqn}"] = _get(client, collection, fqn, fields).get("version")
    for table in _column_links(inventory, database_fqn):
        versions[f"table:{table}"] = _get(client, "tables", table, "columns").get("version")
    return versions


def check_glossary_publication(cfg: E2EConfig) -> None:
    from olf.infra.commands.openmetadata import deploy_openmetadata_metadata

    log.step("Checking glossary publication to OpenMetadata...")
    environ = cfg_environ(cfg)
    database_fqn = config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=environ)
    if not database_fqn:
        raise E2EError("OPENLAKEFORGE_CATALOG_DATABASE_FQN is unset; cannot locate this stage's database root.")
    inventory = cfg.inventory
    if not inventory.glossaries:
        log.info("The project declares no glossary; nothing to check.")
        return
    # The Gold tables reach OpenMetadata through a crawl after the pipelines; the deploy crawls first, then
    # publishes and attaches, so the column links resolve.
    deploy_openmetadata_metadata(environ=environ)
    with openmetadata_client(cfg) as (client, _token):
        before = _snapshot(client, inventory, database_fqn)
        problems: list[str] = []
        for glossary in inventory.glossaries:
            found = _get(client, "glossaries", glossary.name, "owners,extension")
            if not found:
                problems.append(f"glossary {glossary.name} is missing")
        om = om_fqns(inventory.glossaries)
        for ref, term in inventory.terms.items():
            fqn = om[ref]
            found = _get(client, "glossaryTerms", fqn, "relatedTerms,extension")
            if not found:
                problems.append(f"term {fqn} is missing")
                continue
            if sorted(found.get("synonyms") or []) != sorted(term.synonyms):
                problems.append(f"term {fqn} synonyms are {found.get('synonyms')}, want {list(term.synonyms)}")
            if len(found.get("relatedTerms") or []) != len(term.related_terms):
                problems.append(f"term {fqn} has {len(found.get('relatedTerms') or [])} related terms")
            log.info(f"term {fqn}: status {found.get('entityStatus')}, synonyms {found.get('synonyms') or []}")
        for collection, fqn, fields, wanted in _targets(inventory):
            have = _tag_fqns(_get(client, collection, fqn, fields))
            if wanted - have:
                problems.append(f"{collection} {fqn} lacks glossary tags {sorted(wanted - have)}")
            else:
                log.info(f"{collection} {fqn} tagged {sorted(wanted)}")
        pending = []
        for table_fqn, columns in _column_links(inventory, database_fqn).items():
            table = _get(client, "tables", table_fqn, "columns,tags")
            held = {column["name"]: column for column in table.get("columns") or []}
            for column, wanted in columns.items():
                if column not in held:
                    pending.append(f"{table_fqn}.{column}")
                    continue
                have = _tag_fqns(held[column])
                if wanted - have:
                    problems.append(f"column {table_fqn}.{column} lacks glossary tags {sorted(wanted - have)}")
                else:
                    log.info(f"column {table_fqn}.{column} tagged {sorted(wanted)}")
        if pending:
            log.warn(f"Column links pending, table or column not crawled: {pending}")
        if problems:
            raise E2EError("glossary publication is incomplete:\n" + "\n".join(problems))
        deploy_openmetadata_metadata(environ=environ)
        after = _snapshot(client, inventory, database_fqn)
        changed = sorted(key for key in before if before[key] != after[key])
        if changed:
            raise E2EError(f"a repeat deploy rewrote glossary-managed entities: {changed}")
        log.info("A repeat deploy left every glossary, term, tagged product and tagged table at its version.")
