"""An in-memory OpenMetadata for the reconciler tests."""

from __future__ import annotations

import copy
import urllib.parse
from collections.abc import Mapping
from typing import Any

from openlakeforge_services.openmetadata import fqn_part

_CREATE_FIELDS = {
    "glossaryTerms": {
        "glossary",
        "parent",
        "name",
        "displayName",
        "description",
        "style",
        "synonyms",
        "relatedTerms",
        "references",
        "conceptMappings",
        "reviewers",
        "owners",
        "tags",
        "provider",
        "mutuallyExclusive",
        "extension",
        "domains",
    },  # fmt: skip
    "glossaries": {
        "name",
        "displayName",
        "description",
        "reviewers",
        "owners",
        "tags",
        "provider",
        "mutuallyExclusive",
        "domains",
        "extension",
    },  # fmt: skip
}


_ENTITY_STATUSES = {"Draft", "In Review", "Approved", "Archived", "Deprecated", "Rejected", "Unprocessed"}


class FakeOpenMetadata:
    """An in-memory OpenMetadata 1.13.6 holding what its REST API returns for the endpoints the reconciler uses.

    Shapes follow what 1.13.6 answers: entities carry `id` and `fullyQualifiedName`, a missing name is a 404 with
    an error body, a create returns the entity, and JSON Patch is applied to the stored entity.
    """

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, dict]] = {}
        self.types = {
            name: {"id": f"type-{name}", "name": name, "customProperties": []}
            for name in (
                "domain",
                "dataProduct",
                "databaseSchema",
                "table",
                "container",
                "glossary",
                "glossaryTerm",
                "string",
            )
        }
        self.calls: list[tuple[str, str]] = []
        self._ids = 0

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]

    def seed(self, collection: str, fqn: str, entity: dict) -> dict:
        self._ids += 1
        entity_id = f"id-{self._ids}"
        stored = {
            "id": entity_id,
            "name": fqn.rpartition(".")[2],
            "fullyQualifiedName": fqn,
            "version": 0.1,
            "updatedBy": "admin",
            "href": f"http://localhost:8585/api/v1/{collection}/{entity_id}",
            **copy.deepcopy(entity),
        }
        self.entities.setdefault(collection, {})[fqn] = stored
        return stored

    def _fqn_of(self, collection: str, body: dict) -> str:
        if collection == "tables":
            return f"{body['databaseSchema']}.{body['name']}"
        if collection == "databaseSchemas":
            return f"{body['database']}.{body['name']}"
        if collection == "containers":
            parent = body.get("parent")
            parent_fqn = (
                next(c["fullyQualifiedName"] for c in self.entities["containers"].values() if c["id"] == parent["id"])
                if parent
                else fqn_part(body["service"])
            )
            # 1.13.6 quotes a name that contains a dot.
            return f"{parent_fqn}.{fqn_part(body['name'])}"
        if collection == "glossaryTerms":
            return f"{body.get('parent') or body['glossary']}.{body['name']}"
        return body["name"]

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (200,),
        content_type: str = "application/json",
    ) -> Mapping[str, Any]:
        self.calls.append((method, path))
        route = path.partition("?")[0]
        if route == "/api/v1/system/config/jwks":
            return {}
        if route.startswith("/api/v1/metadata/types/name/"):
            return self.types[route.rsplit("/", 1)[1]]
        if route.startswith("/api/v1/metadata/types/"):
            entity_type = next(t for t in self.types.values() if t["id"] == route.rsplit("/", 1)[1])
            string = self.types["string"]
            entity_type["customProperties"].append(
                {**json_body, "propertyType": {**json_body["propertyType"], "name": string["name"]}}
            )
            return entity_type
        if route == "/api/v1/tables" and method == "GET" and "databaseSchema=" in path:
            schema = urllib.parse.parse_qs(path.partition("?")[2])["databaseSchema"][0]
            tables = [t for fqn, t in self.entities.get("tables", {}).items() if fqn.rpartition(".")[0] == schema]
            return {"data": tables, "paging": {}}
        collection = route.removeprefix("/api/v1/")
        if route == "/api/v1/glossaryTerms" and method == "GET":
            query = {**{k: v[0] for k, v in urllib.parse.parse_qs(path.partition("?")[2]).items()}, **(params or {})}
            glossary_id = query["glossary"]  # an id, as on 1.13.6
            name = next(g["name"] for g in self.entities["glossaries"].values() if g["id"] == glossary_id)
            terms = [t for fqn, t in self.entities.get("glossaryTerms", {}).items() if fqn.startswith(f"{name}.")]
            return {"data": terms, "paging": {}}
        if method == "GET" and "/" not in collection and "limit" in (params or {}):
            parents = [v for k, v in params.items() if k in ("database", "databaseSchema")]
            found = [
                e
                for e in self.entities.get(collection, {}).values()
                if all(e["fullyQualifiedName"].rpartition(".")[0] == parent for parent in parents)
            ]
            return {"data": found, "paging": {}}
        if method == "GET" and collection.startswith("dataProducts/name/") and collection.endswith("/assets"):
            product = self.entities["dataProducts"][urllib.parse.unquote(collection.split("/")[2])]
            held = product.get("assets", [])
            offset, limit = params["offset"], min(params["limit"], 1000)
            # 1.13.6 lists every kind of asset (test cases too) with a name and an FQN, and pages with `total`.
            items = [{**ref, "name": ref["id"], "fullyQualifiedName": ref["id"], "deleted": False} for ref in held]
            return {"data": items[offset : offset + limit], "paging": {"total": len(held)}}
        if "/name/" in collection:
            collection, _, fqn = collection.partition("/name/")
            found = self.entities.get(collection, {}).get(urllib.parse.unquote(fqn))
            return dict(found) if found else {"code": 404, "message": "not found"}
        if collection.endswith("/assets/add"):
            product = self.entities["dataProducts"][urllib.parse.unquote(collection.split("/")[1])]
            held = product.setdefault("assets", [])
            held += [ref for ref in json_body["assets"] if ref["id"] not in {asset["id"] for asset in held}]
            return {}
        if method == "POST":
            unknown = set(json_body) - _CREATE_FIELDS.get(collection, set(json_body))
            if unknown:  # the 1.13.6 create requests set additionalProperties: false
                return {"code": 400, "message": "Invalid request format"}
            fqn = self._fqn_of(collection, json_body)
            if fqn in self.entities.get(collection, {}):
                return {"code": 409, "message": "already exists"}
            domains = [self.entities["domains"][name] for name in json_body.get("domains", [])]
            refs = [{"id": d["id"], "type": "domain", "fullyQualifiedName": d["fullyQualifiedName"]} for d in domains]
            return self.seed(collection, fqn, {**json_body, **({"domains": refs} if refs else {})})
        if method == "PATCH":
            entity = next(
                e
                for e in self.entities[collection.rpartition("/")[0]].values()
                if e["id"] == collection.rpartition("/")[2]
            )
            for op in json_body:
                if op["path"].startswith("/relatedTerms"):  # TermRelation: `id` is an unknown field there (HTTP 500)
                    relations = op["value"] if isinstance(op["value"], list) else [op["value"]]
                    assert all(set(r) == {"term", "relationType"} for r in relations), "bad TermRelation"
                if op["path"] == "/entityStatus":  # type/status.json, 1.13.6
                    assert op["value"] in _ENTITY_STATUSES, f"bad entityStatus {op['value']!r}"
                if "/tags" in op["path"]:
                    labels = op["value"] if isinstance(op["value"], list) else [op["value"]]
                    assert all(set(t) == {"tagFQN", "source", "labelType", "state"} for t in labels), "bad tag label"
                *parents, leaf = op["path"].strip("/").split("/")
                node = entity
                for key in parents:
                    # RFC 6902: only the last member of an `add` is created; a missing parent is an error.
                    node = node[int(key)] if isinstance(node, list) else node[key]
                if op["op"] == "remove":
                    node.pop(leaf)
                elif leaf == "-":
                    node.append(copy.deepcopy(op["value"]))
                else:
                    node[leaf] = copy.deepcopy(op["value"])
            return entity
        raise AssertionError(f"unhandled request {method} {path}")
