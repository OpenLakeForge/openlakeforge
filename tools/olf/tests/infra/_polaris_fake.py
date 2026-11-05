"""A Polaris management API and a Kubernetes namespace store for olf's Polaris bootstrap.

`FakePolaris` answers as Polaris does: 409 on a repeated create, one-time
credentials on a principal create or rotate. `Cluster` keeps Secrets and ConfigMaps in a JSON file and answers
olf's kubectl calls from it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = ("root", "root-secret-value")
STS = {
    "role_arn": "arn:aws:iam::000000000000:role/polaris-vending",
    "region": "us-east-1",
    "endpoint": "http://seaweedfs-s3.olf-system:8333",
    "sts_endpoint": "http://seaweedfs-s3.olf-system:8333",
}


class FakePolaris:
    def __init__(self) -> None:
        self.catalogs: dict[str, dict[str, Any]] = {}
        self.principals: dict[str, dict[str, Any]] = {}
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
                self.principals[name] = {}
                return 201, self._issue(name)
            case "GET", ["principals", name]:
                return (200, {"name": name}) if name in self.principals else (404, {})
            case "DELETE", ["principals", name]:
                return (204, {}) if self.principals.pop(name, None) is not None else (404, {})
            case "POST", ["principals", name, "rotate"]:
                return 200, self._issue(name)
        return 400, {"error": f"unexpected {method} {path}"}

    def state(self) -> dict[str, Any]:
        """Everything a client can observe, less entity versions and the credentials themselves."""
        return {
            "catalogs": {
                name: {k: v for k, v in c.items() if k != "entityVersion"} for name, c in self.catalogs.items()
            },
            "principals": sorted(self.principals),
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
        if argv[0] == "get":
            stdout = "found\n" if f"{argv[1]}/{argv[4]}/{argv[2]}" in objects else ""
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
        else:
            raise AssertionError(f"unexpected kubectl call: {argv}")
        self.save(objects)
        if check and returncode:
            raise CommandExecutionError(command, returncode, stderr="AlreadyExists")
        return CommandResult(tuple(argv), returncode, stdout, "", 0.0)
