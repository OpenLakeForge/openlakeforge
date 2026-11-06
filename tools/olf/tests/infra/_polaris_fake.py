"""A Polaris management API and a Kubernetes namespace store, shared by olf's bootstrap and the Terraform Job's script.

`FakePolaris` answers as Polaris does: 409 on a repeated create, 500 on a repeated assignment or grant, one-time
credentials on a principal create or rotate, compact JSON (the script parses it with sed). `Cluster` keeps Secrets
and ConfigMaps in a JSON file, so olf's kubectl calls (in process) and the script's (through `kubectl_shim`) see
the same objects.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = ("root", "root-secret-value")
STAGES = ("dev", "prod")
# What person_access.tf renders for release/identity-roles.yaml with DEV and PROD enabled.
ASSIGNED = {
    "olf-signed-in": {},
    "olf-platform-admin": {"lakehouse_dev": "olf-write", "lakehouse_prod": "olf-write"},
    "olf-data-engineer": {"lakehouse_dev": "olf-write", "lakehouse_prod": "olf-read"},
    "olf-analyst": {"lakehouse_dev": "olf-gold-read", "lakehouse_prod": "olf-gold-read"},
    "olf-viewer": {"lakehouse_prod": "olf-gold-read"},
}
STS = {
    "role_arn": "arn:aws:iam::000000000000:role/polaris-vending",
    "region": "us-east-1",
    "endpoint": "http://seaweedfs-s3.olf-system:8333",
    "sts_endpoint": "http://seaweedfs-s3.olf-system:8333",
}


def conformance_plan(*, person_access: bool = True, sts: bool = True) -> Any:
    """The plan the local root's DEV and PROD stages give the bootstrap, with GitHub sign-in."""
    from olf.infra import polaris_bootstrap as pb

    def credential(principal: str, secret: str, keys: str, namespace: str, *, recreatable: bool = False) -> Any:
        return pb.Credential(principal, secret, f"{keys}_CLIENT_ID", f"{keys}_CLIENT_SECRET", namespace, recreatable)

    stages = tuple(
        pb.StageCatalog(
            stage,
            f"lakehouse_{stage}",
            f"lakehouse-{stage}-bronze",
            f"lakehouse-{stage}-silver",
            f"lakehouse-{stage}-gold",
            trino=credential(f"trino-{stage}", f"polaris-{stage}-trino-creds", "POLARIS_TRINO", "olf-system"),
            floe=credential(
                f"floe-{stage}", f"polaris-{stage}-floe-creds", "POLARIS_FLOE", f"olf-{stage}", recreatable=True
            ),
            deployer=credential(
                f"deployer-{stage}",
                f"polaris-{stage}-deployer-creds",
                "POLARIS_DEPLOYER",
                f"olf-{stage}",
                recreatable=True,
            ),
        )
        for stage in STAGES
    )
    people = tuple(
        credential(f"trino-people-{stage}", f"polaris-{stage}-trino-people-creds", "POLARIS", "olf-system")
        for stage in STAGES
    )
    return pb.BootstrapPlan(
        shared_namespace="olf-system",
        stages=stages,
        openmetadata=credential("openmetadata", "polaris-om-creds", "POLARIS_OM", "olf-system"),
        sts=pb.CatalogSts(**STS) if sts else None,
        person_access=pb.PersonAccess("github", ASSIGNED, people) if person_access else None,
    )


class FakePolaris:
    def __init__(self) -> None:
        self.catalogs: dict[str, dict[str, Any]] = {}
        self.principals: dict[str, dict[str, Any]] = {}
        self.principal_roles: dict[str, dict[str, list[str]]] = {}
        self.catalog_roles: dict[str, dict[str, list[str]]] = {}
        self.writes: list[str] = []
        self.issued = 0

    def _issue(self, name: str) -> dict[str, Any]:
        self.issued += 1
        principal = self.principals[name]
        principal.update(clientId=f"cid-{name}-{self.issued}", clientSecret=f"S3CRET-{name}-{self.issued}")
        public = {"name": name, "clientId": principal["clientId"]}
        return {"principal": public, "credentials": {k: principal[k] for k in ("clientId", "clientSecret")}}

    def handle(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if path == "/api/catalog/v1/oauth/tokens":
            return 200, {"access_token": "token", "token_type": "bearer"}
        parts = [unquote(part) for part in path.removeprefix("/api/management/v1/").split("/")]
        if method != "GET":
            self.writes.append(f"{method} {path}")
        match method, parts:
            case "POST", ["catalogs"]:
                if body["name"] in self.catalogs:
                    return 409, {}
                self.catalogs[body["name"]] = {**body, "entityVersion": 1}
                self.catalog_roles[body["name"]] = {}
                return 201, self.catalogs[body["name"]]
            case "GET", ["catalogs", name]:
                return (200, self.catalogs[name]) if name in self.catalogs else (404, {})
            case "PUT", ["catalogs", name]:
                catalog = self.catalogs[name]
                if body.get("currentEntityVersion") != catalog["entityVersion"]:
                    return 409, {"error": "entity version mismatch"}
                catalog["properties"] = body["properties"]
                catalog["entityVersion"] += 1
                return 200, catalog
            case "POST", ["principals"]:
                name = body["principal"]["name"] if "principal" in body else body["name"]
                if name in self.principals:
                    return 409, {}
                self.principals[name] = {"roles": []}
                return 201, self._issue(name)
            case "GET", ["principals", name]:
                return (200, {"name": name}) if name in self.principals else (404, {})
            case "DELETE", ["principals", name]:
                return (204, {}) if self.principals.pop(name, None) is not None else (404, {})
            case "POST", ["principals", name, "rotate"]:
                return 200, self._issue(name)
            case "GET", ["principals", name, "principal-roles"]:
                if name not in self.principals:
                    return 404, {}
                return 200, {"roles": [{"name": role} for role in self.principals[name]["roles"]]}
            case "PUT", ["principals", name, "principal-roles"]:
                return self._add(self.principals[name]["roles"], body["principalRole"]["name"])
            case "POST", ["principal-roles"]:
                return self._create(self.principal_roles, body["principalRole"]["name"], {})
            case "POST", ["catalogs", catalog, "catalog-roles"]:
                return self._create(self.catalog_roles[catalog], body["catalogRole"]["name"], [])
            case "GET", ["catalogs", catalog, "catalog-roles", role, "grants"]:
                if role not in self.catalog_roles.get(catalog, {}):
                    return 404, {}
                grants = self.catalog_roles[catalog][role]
                return 200, {"grants": [{"type": "catalog", "privilege": privilege} for privilege in grants]}
            case "PUT", ["catalogs", catalog, "catalog-roles", role, "grants"]:
                return self._add(self.catalog_roles[catalog][role], body["grant"]["privilege"])
            case "GET", ["principal-roles", role, "catalog-roles", catalog]:
                if role not in self.principal_roles:
                    return 404, {}
                return 200, {"roles": [{"name": name} for name in self.principal_roles[role].get(catalog, [])]}
            case "PUT", ["principal-roles", role, "catalog-roles", catalog]:
                return self._add(self.principal_roles[role].setdefault(catalog, []), body["catalogRole"]["name"])
            case "DELETE", ["principal-roles", role, "catalog-roles", catalog, name]:
                self.principal_roles[role][catalog].remove(name)
                return 204, {}
        return 400, {"error": f"unexpected {method} {path}"}

    @staticmethod
    def _create(collection: dict[str, Any], name: str, empty: object) -> tuple[int, dict[str, Any]]:
        if name in collection:
            return 409, {}
        collection[name] = empty
        return 201, {}

    @staticmethod
    def _add(values: list[str], value: str) -> tuple[int, dict[str, Any]]:
        if value in values:
            return 500, {"error": "duplicate key value violates unique constraint grant_records_pkey"}
        values.append(value)
        return 201, {}

    def state(self) -> dict[str, Any]:
        """Everything a client can observe, less entity versions and the credentials themselves."""
        return {
            "catalogs": {
                name: {k: v for k, v in c.items() if k != "entityVersion"} for name, c in self.catalogs.items()
            },
            "principals": {name: sorted(p["roles"]) for name, p in self.principals.items()},
            "principal_roles": {
                role: {catalog: sorted(names) for catalog, names in catalogs.items() if names}
                for role, catalogs in self.principal_roles.items()
            },
            "catalog_roles": {
                catalog: {role: sorted(grants) for role, grants in roles.items()}
                for catalog, roles in self.catalog_roles.items()
            },
        }

    def owner(self, value: str) -> str:
        """Which principal's current credential `value` is, or `stale`."""
        for name, principal in self.principals.items():
            if value == principal.get("clientId"):
                return f"id of {name}"
            if value == principal.get("clientSecret"):
                return f"secret of {name}"
        return "stale"


@contextmanager
def serve(fake: FakePolaris) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) or b"{}"
            body = json.loads(raw) if self.path.startswith("/api/management") else {}
            status, payload = fake.handle(self.command, self.path, body)
            data = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _serve

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


class Cluster:
    """Secrets and ConfigMaps, `kind/namespace/name` -> plaintext data, persisted to `path`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.argv: list[list[str]] = []
        self.save({})

    def load(self) -> dict[str, dict[str, str]]:
        return json.loads(self.path.read_text())

    def save(self, objects: dict[str, dict[str, str]]) -> None:
        self.path.write_text(json.dumps(objects, sort_keys=True))

    def put(self, kind: str, namespace: str, name: str, data: dict[str, str]) -> None:
        self.save({**self.load(), f"{kind}/{namespace}/{name}": data})

    def state(self, polaris: FakePolaris) -> dict[str, dict[str, str]]:
        """Every object, with each Secret value named by the principal credential it is."""
        return {
            key: {k: polaris.owner(v) for k, v in data.items()} if key.startswith("secret/") else data
            for key, data in self.load().items()
        }

    def run(self, command: Any, *, check: bool = True, input_text: str | None = None, **_: Any) -> Any:
        """olf's kubectl calls, after `kubectl --context <context>`."""
        from olf.common.deployment.errors import CommandExecutionError
        from olf.common.tooling.process import CommandResult

        argv = [str(part) for part in command][3:]
        self.argv.append(argv)
        objects = self.load()
        stdout, returncode = "", 0
        if argv[:2] == ["get", "deployment"]:  # by label selector: every Deployment in the namespace
            prefix = f"deployment/{argv[5]}/"
            items = [
                {
                    "metadata": {"name": key.removeprefix(prefix)},
                    "spec": {"template": {"metadata": {"annotations": data}}},
                }
                for key, data in objects.items()
                if key.startswith(prefix)
            ]
            stdout = json.dumps({"items": items})
        elif argv[0] == "get":
            data = objects.get(f"{argv[1]}/{argv[4]}/{argv[2]}")
            stdout = "" if data is None else json.dumps({"data": data}) if "json" in argv else "found\n"
        elif argv[0] == "patch":  # a Deployment's pod-template annotations
            key = f"deployment/{argv[3]}/{argv[1].removeprefix('deployment/')}"
            objects[key] = {**objects[key], **json.loads(argv[-1])["spec"]["template"]["metadata"]["annotations"]}
        elif argv[0] == "delete":
            objects.pop(f"{argv[1]}/{argv[4]}/{argv[2]}", None)
        elif argv[0] in {"create", "apply"}:
            manifest = json.loads(input_text if argv[-1] == "-" else Path(argv[-1]).read_text())
            meta = manifest["metadata"]
            key = f"{manifest['kind'].lower()}/{meta['namespace']}/{meta['name']}"
            if argv[0] == "create" and key in objects:
                returncode = 1
            else:
                objects[key] = dict(manifest.get("stringData") or manifest.get("data") or {})
        elif argv[0] != "rollout":  # a rollout is only recorded in `argv`
            raise AssertionError(f"unexpected kubectl call: {argv}")
        self.save(objects)
        if check and returncode:
            raise CommandExecutionError(command, returncode, stderr="AlreadyExists")
        return CommandResult(tuple(argv), returncode, stdout, "", 0.0)


def kubectl_shim(directory: Path, cluster: Cluster) -> Path:
    """A `kubectl` for the Job's script, answering the calls it makes from `cluster`."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "kubectl"
    shim.write_text(
        f"#!{sys.executable}\n"
        + f"""
import json, sys
path = {str(cluster.path)!r}
args = sys.argv[1:]
objects = json.load(open(path))
verb, kind = args[0], args[1]
if verb == "create" and kind == "secret":
    kind, name, rest = "secret", args[3], args[4:]
else:
    name, rest = args[2], args[3:]
namespace = rest[rest.index("-n") + 1]
key = f"{{kind}}/{{namespace}}/{{name}}"
if verb == "get":
    sys.exit(0 if key in objects else 1)
if verb == "delete":
    objects.pop(key, None)
elif verb == "create":
    if key in objects:
        sys.exit("AlreadyExists")
    literals = [a.split("=", 1)[1] for a in rest if a.startswith("--from-literal=")]
    objects[key] = dict(literal.split("=", 1) for literal in literals)
json.dump(objects, open(path, "w"), sort_keys=True)
"""
    )
    shim.chmod(0o755)
    return directory
