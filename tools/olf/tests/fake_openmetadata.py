"""An in-memory OpenMetadata for the reconciler tests."""

from __future__ import annotations

import urllib.parse


class FakeOpenMetadata:
    """An in-memory OpenMetadata 1.13.6 holding what its REST API returns for the endpoints the reconciler uses.

    Shapes follow what 1.13.6 answers: entities carry `id` and `fullyQualifiedName`, a missing name is a 404 with
    an error body, a create returns the entity, and JSON Patch is applied to the stored entity.
    """

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, dict]] = {}
        self.types = {
            name: {"id": f"type-{name}", "name": name, "customProperties": []}
            for name in ("domain", "dataProduct", "databaseSchema", "table", "container", "string")
        }
        self.calls: list[tuple[str, str]] = []
        self._ids = 0

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]

    def seed(self, collection: str, fqn: str, entity: dict) -> dict:
        self._ids += 1
        stored = {"id": f"id-{self._ids}", "name": fqn.rpartition(".")[2], "fullyQualifiedName": fqn, **entity}
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
                else body["service"]
            )
            return f"{parent_fqn}.{body['name']}"
        return body["name"]

    def request(
        self, method, path, *, json_body=None, params=None, ok_statuses=(200,), content_type="application/json"
    ):
        self.calls.append((method, path))
        route = path.partition("?")[0]
        if route == "/api/v1/system/config/jwks":
            return {}
        if route.startswith("/api/v1/metadata/types/name/"):
            return self.types[route.rsplit("/", 1)[1]]
        if route.startswith("/api/v1/metadata/types/"):
            entity_type = next(t for t in self.types.values() if t["id"] == route.rsplit("/", 1)[1])
            entity_type["customProperties"].append(json_body)
            return entity_type
        if route == "/api/v1/tables" and method == "GET":
            schema = urllib.parse.parse_qs(path.partition("?")[2])["databaseSchema"][0]
            tables = [t for fqn, t in self.entities.get("tables", {}).items() if fqn.rpartition(".")[0] == schema]
            return {"data": tables, "paging": {}}
        collection = route.removeprefix("/api/v1/")
        if "/name/" in collection:
            collection, _, fqn = collection.partition("/name/")
            found = self.entities.get(collection, {}).get(urllib.parse.unquote(fqn))
            return dict(found) if found else {"code": 404, "message": "not found"}
        if collection.endswith("/assets/add"):
            product = self.entities["dataProducts"][urllib.parse.unquote(collection.split("/")[1])]
            product.setdefault("assets", []).extend(json_body["assets"])
            return {}
        if method == "POST":
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
                *parents, leaf = op["path"].strip("/").split("/")
                node = entity
                for key in parents:
                    node = node.setdefault(key, {})
                node[leaf] = op["value"]
            return entity
        raise AssertionError(f"unhandled request {method} {path}")
