"""The Polaris bootstrap creates each stage catalog with drop-with-purge allowed, and turns it on for a catalog that
already exists (#741: without it every dbt incremental model failed on its second run)."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

TEMPLATE = Path(__file__).resolve().parents[4] / "infra/terraform/modules/catalog/polaris/templates/bootstrap.sh.tftpl"
PURGE = "polaris.config.drop-with-purge.enabled"
STAGES = {
    stage: {
        "catalog_name": f"lakehouse_{stage}",
        **{f"{layer}_bucket_name": f"lakehouse-{stage}-{layer}" for layer in ("bronze", "silver", "gold")},
    }
    for stage in ("dev", "prod")
}
STS = {
    "role_arn": "arn:aws:iam::000000000000:role/polaris-vending",
    "region": "us-east-1",
    "endpoint": "http://seaweedfs-s3:8333",
    "sts_endpoint": "http://seaweedfs-s3:8333",
}


class FakePolaris:
    """The catalog endpoints with Polaris's semantics: 409 on a repeated create, an update that must name the current
    entity version and replaces the whole property map."""

    def __init__(self) -> None:
        self.catalogs: dict[str, dict[str, Any]] = {}
        self.writes: list[str] = []

    def handle(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = path.removeprefix("/api/management/v1/").split("/")
        if method != "GET":
            self.writes.append(f"{method} {path}")
        match method, parts:
            case "POST", ["catalogs"]:
                if body["name"] in self.catalogs:
                    return 409, {}
                self.catalogs[body["name"]] = {**body, "entityVersion": 1}
                return 201, self.catalogs[body["name"]]
            case "GET", ["catalogs", name]:
                return 200, self.catalogs[name]
            case "PUT", ["catalogs", name]:
                catalog = self.catalogs[name]
                if body.get("currentEntityVersion") != catalog["entityVersion"]:
                    return 409, {"error": "entity version mismatch"}
                if "properties" in body:
                    catalog["properties"] = body["properties"]
                catalog["entityVersion"] += 1
                return 200, catalog
        return 400, {"error": f"unexpected {method} {path}"}

    def existing(self, stage: str, properties: dict[str, str], *, sts: bool = False) -> None:
        self.catalogs[STAGES[stage]["catalog_name"]] = {
            "name": STAGES[stage]["catalog_name"],
            "type": "INTERNAL",
            "properties": properties,
            "storageConfigInfo": {"storageType": "S3", "stsUnavailable": not sts},
            "entityVersion": 3,
        }


@pytest.fixture
def polaris() -> Iterator[tuple[FakePolaris, str]]:
    fake = FakePolaris()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            status, payload = fake.handle(self.command, self.path, json.loads(self.rfile.read(length) or b"{}"))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = _serve

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _render_catalogs(sts: dict[str, str] | None) -> str:
    """The bootstrap's catalog part, rendered the way templatefile does for STAGES."""
    bootstrap = TEMPLATE.read_text()
    request = re.search(r"^  request\(\) \{\n.*?^  \}\n", bootstrap, re.S | re.M)
    catalogs = re.search(r"^  # Trino drops tables with purge.*?^%\{ endfor ~\}\n", bootstrap, re.S | re.M)
    assert request and catalogs
    preamble, loop = catalogs.group(0).split("%{ for stage_name, stage in stage_catalogs ~}\n")
    loop = loop.removesuffix("%{ endfor ~}\n")
    conditional = r"^%\{ if sts != null ~\}\n(.*?)(?:^%\{ else ~\}\n(.*?))?^%\{ endif ~\}\n"
    loop = re.sub(conditional, lambda m: m.group(1) if sts else (m.group(2) or ""), loop, flags=re.S | re.M)
    for name, value in (sts or {}).items():
        loop = loop.replace("${sts." + name + "}", value)
    rendered = "".join(
        re.sub(r"\$\{stage\.(\w+)\}", lambda m, stage=stage: stage[m.group(1)], loop) for stage in STAGES.values()
    )
    return (request.group(0) + preamble + rendered).replace("%%{", "%{").replace("$${", "${")


def _run(url: str, tmp_path: Path, *, sts: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script = (f'set -eu\npolaris_url="{url}"\nPOLARIS_TOKEN=t\n' + _render_catalogs(sts)).replace(
        "/tmp/polaris-body", str(tmp_path / "body")
    )
    return subprocess.run(["sh", "-ec", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})


@pytest.mark.parametrize("sts", [None, STS], ids=["static-keys", "sts"])
def test_a_new_stage_catalog_allows_drop_with_purge(
    polaris: tuple[FakePolaris, str], tmp_path: Path, sts: dict[str, str] | None
) -> None:
    fake, url = polaris

    result = _run(url, tmp_path, sts=sts)

    assert result.returncode == 0, result.stderr
    for stage in STAGES.values():
        assert fake.catalogs[stage["catalog_name"]]["properties"] == {
            "default-base-location": f"s3://{stage['silver_bucket_name']}/",
            PURGE: "true",
        }
    assert not any(write.startswith("PUT") for write in fake.writes)


@pytest.mark.parametrize("sts", [None, STS], ids=["static-keys", "sts"])
def test_an_existing_stage_catalog_gains_drop_with_purge_and_keeps_its_other_properties(
    polaris: tuple[FakePolaris, str], tmp_path: Path, sts: dict[str, str] | None
) -> None:
    fake, url = polaris
    before = {"default-base-location": "s3://lakehouse-dev-silver/", "operator-set": "kept"}
    fake.existing("dev", dict(before), sts=sts is not None)

    result = _run(url, tmp_path, sts=sts)

    assert result.returncode == 0, result.stderr
    assert fake.catalogs["lakehouse_dev"]["properties"] == {**before, PURGE: "true"}
    assert fake.catalogs["lakehouse_prod"]["properties"][PURGE] == "true"


def test_a_catalog_with_purge_turned_off_is_turned_back_on(polaris: tuple[FakePolaris, str], tmp_path: Path) -> None:
    fake, url = polaris
    fake.existing("dev", {"default-base-location": "s3://lakehouse-dev-silver/", PURGE: "false"})

    result = _run(url, tmp_path)

    assert result.returncode == 0, result.stderr
    assert fake.catalogs["lakehouse_dev"]["properties"][PURGE] == "true"


def test_a_rerun_updates_no_catalog(polaris: tuple[FakePolaris, str], tmp_path: Path) -> None:
    fake, url = polaris
    fake.existing("dev", {"default-base-location": "s3://lakehouse-dev-silver/"})
    _run(url, tmp_path)
    fake.writes.clear()

    result = _run(url, tmp_path)

    assert result.returncode == 0, result.stderr
    assert [write for write in fake.writes if not write.startswith("POST")] == []


def test_an_existing_catalog_without_sts_still_warns(polaris: tuple[FakePolaris, str], tmp_path: Path) -> None:
    fake, url = polaris
    fake.existing("dev", {"default-base-location": "s3://lakehouse-dev-silver/"}, sts=False)

    result = _run(url, tmp_path, sts=STS)

    assert result.returncode == 0, result.stderr
    assert "lakehouse_dev was created without STS" in result.stderr
    assert fake.catalogs["lakehouse_dev"]["properties"][PURGE] == "true"
