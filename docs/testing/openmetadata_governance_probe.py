#!/usr/bin/env python3
"""OpenMetadata 1.13.6 governance probe for v0.7.5 (#918, #920, #1464, #1465).

Runs against a deployed local stack, on one stage, and answers the questions those issues mark "verify before
coding": which governance fields the OLF automation bot may write, what the Trino crawl keeps or overwrites, whether
Iceberg comments reach OpenMetadata, and what `metadata profile` accepts and stores. It creates entities named
`olfprobe_*` and removes them (and restores the Gold tables it patched) on exit.

    uv run --project tools/olf --locked python docs/testing/openmetadata_governance_probe.py \\
        -f openlakeforge.yaml --stage dev --json probe.json --summary probe.md

Needs a stack whose Gold tables exist and are crawled (`olf infra e2e run --checks core` leaves one). It uses the
identities `olf infra e2e run` uses: the automation bot token for OpenMetadata, the stage processing principal for
Trino, and the crawl Job's own bot and Polaris Secrets. No credential is printed; JWTs are masked in everything it
writes. A refused or ignored write is a verdict, not a failure: the exit status is non-zero only when the harness
could not run (no cluster, no Gold table).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from olf.common import config
from olf.infra import e2e, k8s, trino
from olf.infra.commands._shared import applied_runtime_environment, deployment_context_for_profile
from olf.infra.e2e._assertions import openmetadata_client
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl
from olf.infra.e2e._trino import product_gold_tables, trino_query
from olf.infra.openmetadata import _ingestion
from openlakeforge_services.openmetadata import OpenMetadataClient

P = "olfprobe"
PATCH_TYPE = "application/json-patch+json"
GLOSSARY, TERM = f"{P}_glossary", f"{P}_term"
TERM_FQN = f"{GLOSSARY}.{TERM}"
TRINO_TABLE = f"{P}_comment"
JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
PROFILE_METRICS = ["rowCount", "valuesCount", "nullCount", "nullProportion", "distinctCount", "min", "max", "mean"]


@dataclass
class Finding:
    id: str
    question: str
    verdict: str = "NOT RUN"
    detail: str = ""
    payload: Any = None
    response: Any = None

    def note(self, text: str) -> None:
        self.detail = f"{self.detail}; {text}" if self.detail else text


@dataclass
class Resp:
    status: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def message(self) -> str:
        text = self.data.get("message") if isinstance(self.data, dict) else self.data
        return " ".join(str(text).split())[:400]

    def brief(self) -> str:
        return f"HTTP {self.status}" + ("" if self.ok else f": {self.message}")


@dataclass
class Ctx:
    cfg: E2EConfig
    client: OpenMetadataClient
    stage: str
    database_fqn: str
    catalog: str
    service: str
    findings: dict[str, Finding] = field(default_factory=dict)
    fix: dict[str, Any] = field(default_factory=dict)
    cleanups: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    restore_errors: list[str] = field(default_factory=list)

    def finding(self, id: str, question: str) -> Finding:
        return self.findings.setdefault(id, Finding(id, question))


def scrub(value: Any) -> Any:
    return json.loads(JWT.sub("***", json.dumps(value, default=str)))


def om(ctx: Ctx, method: str, path: str, body: Any = None, *, params: dict[str, Any] | None = None) -> Resp:
    """One OpenMetadata call that never raises on an HTTP status: the status and body are the probe's data."""
    headers = {"Content-Type": PATCH_TYPE} if method == "PATCH" else None
    response = ctx.client.send(
        method, path, ok_statuses=tuple(range(200, 600)), json=body, params=params, headers=headers
    )
    try:
        data: Any = response.json()
    except ValueError:
        data = response.text
    return Resp(response.status_code, data)


def patch(ctx: Ctx, kind: str, entity_id: str, *ops: dict[str, Any]) -> Resp:
    return om(ctx, "PATCH", f"/api/v1/{kind}/{entity_id}", list(ops))


def add(path: str, value: Any) -> dict[str, Any]:
    return {"op": "add", "path": path, "value": value}


def quote(fqn: str) -> str:
    return urllib.parse.quote(fqn, safe="")


def read(ctx: Ctx, kind: str, entity_id: str, fields: str) -> Resp:
    """GET with the wanted `fields`, then without when the server refuses one of them."""
    first = om(ctx, "GET", f"/api/v1/{kind}/{entity_id}", params={"fields": fields} if fields else None)
    return first if first.ok or not fields else om(ctx, "GET", f"/api/v1/{kind}/{entity_id}")


def ref(entity_id: str, kind: str) -> dict[str, str]:
    return {"id": entity_id, "type": kind}


def table_fqn(ctx: Ctx, schema: str, table: str) -> str:
    return f"{ctx.database_fqn}.{schema}.{table}"


def get_table(ctx: Ctx, fqn: str, fields: str = "columns,tableConstraints,tags") -> Resp:
    return om(ctx, "GET", f"/api/v1/tables/name/{quote(fqn)}", params={"fields": fields})


def run_crawl(ctx: Ctx, label: str) -> tuple[bool, str]:
    """The OLF-owned Trino crawl (ADR 0007 D15), exactly as `olf infra openmetadata deploy-metadata` runs it."""
    started = time.monotonic()
    try:
        _ingestion.run_trino_ingestion(cfg_environ(ctx.cfg), namespace=ctx.cfg.platform_namespace, service=ctx.service)
    except k8s.KubectlError as exc:
        return False, f"crawl {label} FAILED: {JWT.sub('***', str(exc))[-600:]}"
    return True, f"crawl {label} ok in {time.monotonic() - started:.0f}s"


def job_namespace(ctx: Ctx) -> str:
    return trino.service_namespace(cfg_environ(ctx.cfg), ctx.cfg.platform_namespace)


def ingestion_image(ctx: Ctx) -> str:
    root = config.distribution_root(environ=cfg_environ(ctx.cfg))
    return yaml.safe_load((root / "release/component-catalog.yaml").read_text())["components"]["images"][
        "openmetadata_ingestion"
    ]


def run_in_ingestion_image(ctx: Ctx, name: str, code: str, *, timeout: int = 300) -> tuple[bool, str]:
    """`python -c code` in the pinned ingestion-base image; (succeeded, its output)."""
    namespace = job_namespace(ctx)
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": {"openlakeforge.io/job": P}},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "python",
                    "image": ingestion_image(ctx),
                    "command": ["python", "-W", "ignore", "-c", code],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                }
            ],
        },
    }
    delete = ["delete", "pod", name, "-n", namespace, "--ignore-not-found"]
    kubectl(ctx.cfg, delete, capture=True)
    phase = ""
    try:
        kubectl(ctx.cfg, ["apply", "-f", "-"], capture=True, input_text=json.dumps(pod))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and phase not in ("Succeeded", "Failed"):
            time.sleep(4)
            try:
                phase = kubectl(
                    ctx.cfg, ["get", "pod", name, "-n", namespace, "-o", "jsonpath={.status.phase}"], capture=True
                ).strip()
            except E2EError:
                continue
        logs = kubectl(ctx.cfg, ["logs", name, "-n", namespace], capture=True)
    finally:
        kubectl(ctx.cfg, [*delete, "--wait=false"], capture=True)
    return phase == "Succeeded", JWT.sub("***", logs)


# Prints, for each model, its property names and the definition of the ones asked for, with $refs resolved.
SCHEMA_CODE = r"""
import json, importlib
def resolve(node, defs, depth=0):
    if isinstance(node, dict):
        if "$ref" in node and depth < 4:
            return resolve(defs.get(node["$ref"].split("/")[-1], node), defs, depth + 1)
        return {k: resolve(v, defs, depth) for k, v in node.items() if k not in ("title", "additionalProperties")}
    if isinstance(node, list):
        return [resolve(v, defs, depth) for v in node]
    return node
for module, cls, wanted in json.loads(%r):
    try:
        model = getattr(importlib.import_module(module), cls)
        schema = model.model_json_schema()
        props, defs = schema.get("properties", {}), schema.get("$defs", {})
        print("MODEL", cls, "properties:", sorted(props))
        for name in wanted or sorted(props):
            print("  ", name, "=", json.dumps(resolve(props.get(name, "ABSENT"), defs), sort_keys=True))
    except Exception as exc:
        print("MODEL", cls, "ERROR", type(exc).__name__, str(exc)[:200])
"""
GEN = "metadata.generated.schema"
SCHEMAS = [
    (
        f"{GEN}.entity.domains.dataProduct",
        "DataProduct",
        ["sla", "lifecycleStage", "dataProductType", "consumesFrom", "providesTo", "experts", "reviewers", "owners"],
    ),
    (f"{GEN}.api.domains.createDataProduct", "CreateDataProductRequest", []),
    (f"{GEN}.entity.domains.domain", "Domain", ["domainType"]),
    (f"{GEN}.entity.data.metric", "Metric", ["metricExpression", "metricType", "unitOfMeasurement", "granularity"]),
    (f"{GEN}.api.data.createMetric", "CreateMetricRequest", []),
    (
        f"{GEN}.entity.data.table",
        "TableConstraint",
        ["constraintType", "columns", "referredColumns", "relationshipType"],
    ),
    (f"{GEN}.metadataIngestion.databaseServiceProfilerPipeline", "DatabaseServiceProfilerPipeline", []),
    (
        f"{GEN}.metadataIngestion.databaseServiceMetadataPipeline",
        "DatabaseServiceMetadataPipeline",
        ["overrideMetadata"],
    ),
]


def probe_schemas(ctx: Ctx) -> None:
    f = ctx.finding("S", "Generated 1.13.6 models for the governance fields (from the pinned ingestion image)")
    ok, output = run_in_ingestion_image(ctx, f"{P}-schemas", SCHEMA_CODE % json.dumps(SCHEMAS))
    f.verdict, f.response = ("RECORDED" if ok else "ERROR"), output
    ctx.fix["profiler_properties"] = _profiler_properties(output)


def _profiler_properties(output: str) -> set[str]:
    match = re.search(r"MODEL DatabaseServiceProfilerPipeline properties: (\[.*\])", output)
    return set(names_in(match.group(1))) if match else set()


def names_in(text: str) -> list[str]:
    return re.findall(r"'([^']+)'", text)


def setup(ctx: Ctx) -> None:
    users = om(ctx, "GET", "/api/v1/users/name/admin")
    team = om(ctx, "GET", "/api/v1/teams/name/Organization")
    if not users.ok or not team.ok:
        raise E2EError(f"cannot resolve the admin user ({users.brief()}) or the Organization team ({team.brief()})")
    ctx.fix["user"], ctx.fix["organization"] = ref(users.data["id"], "user"), ref(team.data["id"], "team")

    def create(kind: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        response = om(ctx, "POST", f"/api/v1/{kind}", {"name": name, "description": "OLF governance probe", **body})
        if not response.ok:
            raise E2EError(f"cannot create {kind} {name}: {response.brief()}")
        ctx.cleanups.append(
            (
                f"{kind}/{name}",
                lambda: (
                    om(
                        ctx,
                        "DELETE",
                        f"/api/v1/{kind}/{response.data['id']}",
                        params={"hardDelete": "true", "recursive": "true"},
                    )
                    and None
                ),
            )
        )
        return response.data

    group = create("teams", f"{P}_team", {"teamType": "Group"})
    ctx.fix["team"] = ref(group["id"], "team")
    ctx.fix["domain"] = create("domains", f"{P}_domain", {"domainType": "Aggregate"})
    for key, name in (("product", f"{P}_product"), ("upstream", f"{P}_upstream")):
        ctx.fix[key] = create("dataProducts", name, {"domains": [f"{P}_domain"]})
    create("glossaries", GLOSSARY, {})
    create("glossaryTerms", TERM, {"glossary": GLOSSARY})
    gold: list[tuple[str, str]] = []
    for product in ctx.cfg.inventory.products:
        for entry in product_gold_tables(ctx.cfg, product):
            schema, _, table = entry.partition(".")
            gold.append((schema, table))
    tables = []
    for schema, table in gold:
        response = get_table(ctx, table_fqn(ctx, schema, table))
        if response.ok and len(response.data.get("columns") or []) >= 2:
            tables.append(response.data)
    if len(tables) < 2:
        raise E2EError(f"need two crawled Gold tables with two columns in {ctx.database_fqn}; found {len(tables)}")
    ctx.fix["table_a"], ctx.fix["table_b"] = tables[0], tables[1]
    ctx.fix["originals"] = {
        t["id"]: {
            "constraints": t.get("tableConstraints"),
            "columns": t["columns"],
            "description": t.get("description"),
        }
        for t in tables[:2]
    }


# `fields` is refused whole when it names one the entity does not have, so each kind lists only its own.
READ_FIELDS = {
    "dataProducts": "domains,owners,experts,reviewers,consumesFrom,providesTo,tags,extension",
    "domains": "owners,experts,tags,extension",
    "metrics": "owners,domains,dataProducts,reviewers,tags,extension",
}


def stored(ctx: Ctx, kind: str, entity_id: str, name: str) -> Any:
    return (read(ctx, kind, entity_id, READ_FIELDS[kind]).data or {}).get(name)


def same(got: Any, want: Any) -> bool:
    """Whether the server's read-back carries `want`: entity references may come back expanded, so a wanted dict is
    matched as a subset and a wanted list element by element."""
    if isinstance(want, dict) and isinstance(got, dict):
        return all(same(got.get(k), v) for k, v in want.items())
    if isinstance(want, list) and isinstance(got, list):
        return len(want) == len(got) and all(any(same(g, w) for g in got) for w in want)
    return got == want


def try_candidates(
    ctx: Ctx, id: str, question: str, kind: str, entity_id: str, name: str, candidates: list[Any]
) -> Finding:
    """PATCH `name` with each candidate in turn until one is accepted and read back."""
    f = ctx.finding(id, question)
    attempts = []
    for value in candidates:
        r = patch(ctx, kind, entity_id, add(f"/{name}", value))
        got = stored(ctx, kind, entity_id, name) if r.ok else None
        attempts.append({"payload": value, "result": r.brief(), "ok": r.ok, "read_back": got})
        if r.ok and same(got, value):
            f.verdict, f.payload, f.response = "ACCEPTED", {"op": "add", "path": f"/{name}", "value": value}, attempts
            return f
    ignored = any(a["ok"] for a in attempts)
    f.verdict = "IGNORED" if ignored else "REFUSED"
    f.detail = "every candidate shape was tried; see response"
    f.payload, f.response = candidates, attempts
    return f


def probe_lifecycle(ctx: Ctx) -> None:
    f = ctx.finding("1", "PATCH DataProduct.lifecycleStage with the automation bot")
    product = ctx.fix["product"]
    first = patch(ctx, "dataProducts", product["id"], add("/lifecycleStage", "PRODUCTION"))
    attempts = [{"step": "PATCH add /lifecycleStage PRODUCTION", "result": first.brief()}]
    created = om(
        ctx,
        "POST",
        "/api/v1/dataProducts",
        {
            "name": f"{P}_lifecycle_create",
            "description": "probe",
            "domains": [f"{P}_domain"],
            "lifecycleStage": "DEVELOPMENT",
        },
    )
    attempts.append({"step": "POST create with lifecycleStage DEVELOPMENT", "result": created.brief()})
    if created.ok:
        ctx.cleanups.append(
            (
                "dataProducts/create",
                lambda: (
                    om(ctx, "DELETE", f"/api/v1/dataProducts/{created.data['id']}", params={"hardDelete": "true"})
                    and None
                ),
            )
        )
    try:
        workflows = om(ctx, "GET", "/api/v1/governance/workflowDefinitions", params={"limit": 50})
        attempts.append(
            {
                "step": "governance workflow definitions",
                "result": workflows.brief(),
                "names": [w.get("name") for w in (workflows.data or {}).get("data", [])]
                if isinstance(workflows.data, dict)
                else None,
            }
        )
    except Exception as exc:  # noqa: BLE001 - evidence only
        attempts.append({"step": "governance workflow definitions", "result": f"error {exc}"})
    after = (read(ctx, "dataProducts", product["id"], "").data or {}).get("lifecycleStage")
    time.sleep(20)  # a lifecycle workflow, if one is active, runs on the update event within seconds
    later = (read(ctx, "dataProducts", product["id"], "").data or {}).get("lifecycleStage")
    replaced = patch(
        ctx, "dataProducts", product["id"], {"op": "replace", "path": "/lifecycleStage", "value": "TESTING"}
    )
    attempts.append(
        {
            "step": "PATCH replace /lifecycleStage TESTING",
            "result": replaced.brief(),
            "read_back": (read(ctx, "dataProducts", product["id"], "").data or {}).get("lifecycleStage"),
        }
    )
    second = patch(ctx, "dataProducts", product["id"], add("/lifecycleStage", "DEPRECATED"))
    again = (read(ctx, "dataProducts", product["id"], "").data or {}).get("lifecycleStage")
    attempts.append({"step": "PATCH again to DEPRECATED", "result": second.brief(), "read_back": again})
    f.payload = {"op": "add", "path": "/lifecycleStage", "value": "PRODUCTION"}
    f.response = attempts
    f.detail = f"read back {after!r} at once, {later!r} after 20 s, {again!r} after a second PATCH"
    f.verdict = (
        "REFUSED"
        if not first.ok
        else "IGNORED"
        if after != "PRODUCTION"
        else "OVERWRITTEN"
        if later != "PRODUCTION"
        else "ACCEPTED"
    )


def probe_native_fields(ctx: Ctx) -> None:
    product, upstream, domain = ctx.fix["product"]["id"], ctx.fix["upstream"]["id"], ctx.fix["domain"]["id"]
    user, team = ctx.fix["user"], ctx.fix["team"]
    q = "PATCH DataProduct.%s"
    try_candidates(
        ctx,
        "2a",
        q % "dataProductType",
        "dataProducts",
        product,
        "dataProductType",
        ["DATASET", "REPORTS", "ANALYTIC_VIEW"],
    )
    try_candidates(
        ctx,
        "2b",
        q % "sla",
        "dataProducts",
        product,
        "sla",
        [
            {"tier": "GOLD", "availability": 99.9, "dataFreshness": 1440, "dataQuality": 95.0, "responseTime": 2000},
            {"availability": 99.9},
        ],
    )
    try_candidates(
        ctx,
        "2c",
        q % "consumesFrom",
        "dataProducts",
        product,
        "consumesFrom",
        [[ref(upstream, "dataProduct")], [{"fullyQualifiedName": f"{P}_upstream"}]],
    )
    reciprocal = (read(ctx, "dataProducts", upstream, "providesTo").data or {}).get("providesTo")
    ctx.finding("2c", "").detail = f"upstream.providesTo after the edit: {reciprocal}"
    try_candidates(ctx, "2d", q % "experts", "dataProducts", product, "experts", [[user]])
    try_candidates(ctx, "2e", q % "reviewers", "dataProducts", product, "reviewers", [[user]])
    try_candidates(ctx, "2f", q % "owners (team)", "dataProducts", product, "owners", [[team], [user]])
    try_candidates(
        ctx,
        "2g",
        "PATCH Domain.domainType",
        "domains",
        domain,
        "domainType",
        ["Consumer-aligned", "Source-aligned", "Aggregate"],
    )


def probe_metric(ctx: Ctx) -> None:
    f = ctx.finding("3", "Create a Metric (expression, type, owners, domain, data product, glossary tag, extension)")
    metric_type = om(ctx, "GET", "/api/v1/metadata/types/name/metric", params={"fields": "customProperties"})
    string_type = om(ctx, "GET", "/api/v1/metadata/types/name/string")
    prop = {"name": "olfProbeNote", "description": "probe", "propertyType": ref(string_type.data.get("id", ""), "type")}
    added = om(ctx, "PUT", f"/api/v1/metadata/types/{metric_type.data.get('id', '')}", prop)
    custom = ctx.finding("3a", "Provision an extension custom property on Metric")
    custom.verdict, custom.payload, custom.response = ("ACCEPTED" if added.ok else "REFUSED"), prop, added.brief()
    body = {
        "name": f"{P}_net_revenue",
        "displayName": "Probe net revenue",
        "description": "probe metric",
        "metricExpression": {
            "language": "SQL",
            "code": "SUM(CASE WHEN order_status = 'fulfilled' THEN net_amount END)",
        },
        "metricType": "SUM",
        "owners": [ctx.fix["team"]],
        "domains": [f"{P}_domain"],
        "dataProducts": [f"{P}_product"],
        "tags": [{"tagFQN": TERM_FQN, "source": "Glossary", "labelType": "Manual", "state": "Confirmed"}],
        "extension": {"olfProbeNote": "hello"},
    }
    r = om(ctx, "POST", "/api/v1/metrics", body)
    f.payload = body
    attempts: list[Any] = [{"full body": r.brief()}]
    metric = r.data if r.ok else None
    if not r.ok:
        base = {k: body[k] for k in ("name", "description", "metricExpression")}
        for key in ("metricType", "owners", "domains", "dataProducts", "tags", "extension"):
            extra = {"domains": body["domains"]} if key == "dataProducts" else {}
            part = om(
                ctx, "POST", "/api/v1/metrics", {**base, "name": f"{P}_metric_{key.lower()}", key: body[key], **extra}
            )
            attempts.append({f"minimal + {key}": part.brief()})
            if part.ok:
                ctx.cleanups.append(
                    (
                        f"metrics/{key}",
                        lambda i=part.data["id"]: (
                            om(ctx, "DELETE", f"/api/v1/metrics/{i}", params={"hardDelete": "true"}) and None
                        ),
                    )
                )
        minimal = om(ctx, "POST", "/api/v1/metrics", {**base, "name": f"{P}_metric_minimal"})
        if minimal.ok:
            metric = minimal.data
    f.response = attempts
    if metric is None:
        f.verdict = "REFUSED"
        return
    ctx.cleanups.append(
        (
            "metrics",
            lambda: om(ctx, "DELETE", f"/api/v1/metrics/{metric['id']}", params={"hardDelete": "true"}) and None,
        )
    )
    got = read(ctx, "metrics", metric["id"], READ_FIELDS["metrics"]).data or {}
    summary = {
        k: got.get(k)
        for k in ("metricType", "metricExpression", "owners", "domains", "dataProducts", "tags", "extension")
    }
    f.response = [*attempts, {"read_back": summary}]
    f.verdict = "ACCEPTED" if r.ok else "PARTIAL"
    edit = patch(
        ctx,
        "metrics",
        metric["id"],
        add("/description", "patched"),
        add("/metricExpression", {"language": "SQL", "code": "SUM(net_amount)"}),
    )
    f.detail = f"PATCH of description and metricExpression: {edit.brief()}"

    lineage = ctx.finding("3b", "Lineage edge table -> metric")
    table = ctx.fix["table_a"]
    edge = {
        "edge": {
            "fromEntity": ref(table["id"], "table"),
            "toEntity": ref(metric["id"], "metric"),
            "lineageDetails": {"source": "Manual", "description": "computed from"},
        }
    }
    put = om(ctx, "PUT", "/api/v1/lineage", edge)
    graph = om(ctx, "GET", f"/api/v1/lineage/metric/{metric['id']}", params={"upstreamDepth": 1, "downstreamDepth": 0})
    upstream = [e for e in (graph.data or {}).get("upstreamEdges", [])] if isinstance(graph.data, dict) else []
    lineage.payload, lineage.response = (
        edge,
        {"PUT": put.brief(), "GET lineage/metric": graph.brief(), "upstreamEdges": upstream},
    )
    lineage.verdict = "ACCEPTED" if put.ok and upstream else "REFUSED" if not put.ok else "IGNORED"
    if put.ok:
        ctx.cleanups.append(
            (
                "lineage",
                lambda: om(ctx, "DELETE", f"/api/v1/lineage/table/{table['id']}/metric/{metric['id']}") and None,
            )
        )


def trino_try(ctx: Ctx, sql: str) -> dict[str, Any]:
    try:
        return {"sql": sql, "ok": True, "output": trino_query(ctx.cfg, sql).strip()[:1500]}
    except E2EError as exc:
        return {"sql": sql, "ok": False, "output": JWT.sub("***", str(exc))[-500:]}


def probe_trino_comments(ctx: Ctx) -> None:
    f = ctx.finding("5", "Iceberg comments through Trino and Polaris, then OpenMetadata descriptions")
    schema = next(iter(ctx.fix["table_a"]["fullyQualifiedName"].split(".")[-2:-1]))
    ctx.fix["probe_schema"] = schema
    t = f"{ctx.catalog}.{schema}.{TRINO_TABLE}"
    ctx.cleanups.append(("trino table", lambda: trino_try(ctx, f"DROP TABLE IF EXISTS {t}") and None))
    ctx.cleanups.append(
        (
            "om probe table",
            lambda: (
                om(
                    ctx,
                    "DELETE",
                    f"/api/v1/tables/name/{quote(table_fqn(ctx, schema, TRINO_TABLE))}",
                    params={"hardDelete": "true"},
                )
                and None
            ),
        )
    )
    select = "SELECT CAST(1 AS bigint) AS id, CAST('a' AS varchar) AS name"
    steps = [
        trino_try(ctx, f"DROP TABLE IF EXISTS {t}"),
        trino_try(ctx, f"CREATE TABLE {t} AS {select}"),
        trino_try(ctx, f"COMMENT ON TABLE {t} IS 'probe table comment'"),
        trino_try(ctx, f"COMMENT ON COLUMN {t}.id IS 'probe id comment'"),
        trino_try(ctx, f"DESCRIBE {t}"),
        trino_try(
            ctx,
            f"SELECT comment FROM system.metadata.table_comments WHERE catalog_name = '{ctx.catalog}' "
            f"AND schema_name = '{schema}' AND table_name = '{TRINO_TABLE}'",
        ),
        # dbt-trino with on_table_exists=replace on Iceberg: this statement, then persist_docs' COMMENT statements.
        trino_try(ctx, f"CREATE OR REPLACE TABLE {t} AS {select}"),
        trino_try(ctx, f"DESCRIBE {t}"),
        trino_try(ctx, f"COMMENT ON TABLE {t} IS 'probe table comment'"),
        trino_try(ctx, f"COMMENT ON COLUMN {t}.id IS 'probe id comment'"),
        trino_try(ctx, f"DESCRIBE {t}"),
    ]
    f.response = {"trino": steps}
    failed = [s["sql"] for s in steps if not s["ok"]]
    f.verdict = "REFUSED" if failed else "ACCEPTED"
    f.detail = f"failed statements: {failed}" if failed else "COMMENT ON TABLE/COLUMN and CREATE OR REPLACE all ran"
    f.payload = {"statements": [s["sql"] for s in steps]}


def apply_constraints_and_tags(ctx: Ctx) -> None:
    a, b = ctx.fix["table_a"], ctx.fix["table_b"]
    pk_col, fk_col = a["columns"][0]["name"], a["columns"][1]["name"]
    referred = b["columns"][0]["fullyQualifiedName"]
    constraints = [
        {"constraintType": "PRIMARY_KEY", "columns": [pk_col]},
        {
            "constraintType": "FOREIGN_KEY",
            "columns": [fk_col],
            "referredColumns": [referred],
            "relationshipType": "MANY_TO_ONE",
        },
    ]
    f4 = ctx.finding("4", "PATCH Table.tableConstraints (PRIMARY_KEY, FOREIGN_KEY) and survive the re-crawl")
    op = add("/tableConstraints", constraints)
    r = patch(ctx, "tables", a["id"], op)
    unique = patch(
        ctx,
        "tables",
        b["id"],
        add("/tableConstraints", [{"constraintType": "UNIQUE", "columns": [b["columns"][0]["name"]]}]),
    )
    f4.payload = {"table": a["fullyQualifiedName"], "op": op, "UNIQUE on": b["fullyQualifiedName"]}
    f4.response = {"PATCH PK+FK": r.brief(), "PATCH UNIQUE": unique.brief()}
    f4.verdict = "ACCEPTED" if r.ok else "REFUSED"
    ctx.fix["constraints"] = constraints

    f6 = ctx.finding("6", "Glossary term tag PATCHed onto a column survives the re-crawl")
    tag = [{"tagFQN": TERM_FQN, "source": "Glossary", "labelType": "Manual", "state": "Confirmed"}]
    tagged = patch(
        ctx, "tables", a["id"], add("/columns/0/tags", tag), add("/columns/1/description", "human column description")
    )
    f6.payload = {"column": a["columns"][0]["fullyQualifiedName"], "op": add("/columns/0/tags", tag)}
    f6.response = {"PATCH": tagged.brief()}
    f6.verdict = "ACCEPTED" if tagged.ok else "REFUSED"


def verify_after_crawl(ctx: Ctx, label: str) -> None:
    a = get_table(ctx, ctx.fix["table_a"]["fullyQualifiedName"]).data
    b = get_table(ctx, ctx.fix["table_b"]["fullyQualifiedName"]).data
    f4, f6 = ctx.findings["4"], ctx.findings["6"]
    got = a.get("tableConstraints") or []
    kept = {c.get("constraintType") for c in got}
    unique = {c.get("constraintType") for c in b.get("tableConstraints") or []}
    f4.response[f"after {label}"] = {"A": got, "B": b.get("tableConstraints")}
    if f4.verdict in ("ACCEPTED", "SURVIVES", "PARTIAL"):
        f4.verdict = "SURVIVES" if {"PRIMARY_KEY", "FOREIGN_KEY"} <= kept else "PARTIAL" if kept else "WIPED"
        f4.detail = f"after {label}: A keeps {sorted(kept)}, B keeps {sorted(unique)}"
    cols = a.get("columns") or []
    tags = [t.get("tagFQN") for t in cols[0].get("tags") or []]
    f6.response[f"after {label}"] = {"column 0 tags": tags, "column 1 description": cols[1].get("description")}
    if f6.verdict in ("ACCEPTED", "SURVIVES", "WIPED"):
        f6.verdict = "SURVIVES" if TERM_FQN in tags else "WIPED"
        survived = cols[1].get("description") == "human column description"
        tag_state, text_state = ("kept" if x else "gone" for x in (TERM_FQN in tags, survived))
        f6.detail = f"after {label}: glossary tag {tag_state}; human column description {text_state}"


def verify_descriptions(ctx: Ctx, label: str, table_comment: str, column_comment: str) -> None:
    f = ctx.findings["5"]
    row = get_table(ctx, table_fqn(ctx, ctx.fix["probe_schema"], TRINO_TABLE), "columns")
    data = row.data if row.ok else {}
    columns = {c["name"]: c.get("description") for c in data.get("columns") or []}
    f.response[f"OpenMetadata after {label}"] = {
        "HTTP": row.status,
        "description": data.get("description"),
        "columns": columns,
    }
    f.note(f"{label}: table {data.get('description')!r}, column id {columns.get('id')!r}")
    if f.verdict in ("ACCEPTED", "REFUSED"):
        f.note(f"Trino comments were {table_comment!r} / {column_comment!r}")


def om_api(ctx: Ctx) -> str:
    environ = cfg_environ(ctx.cfg)
    host = f"{config.env('OPENMETADATA_SERVICE', 'openmetadata', environ=environ)}.{ctx.cfg.platform_namespace}"
    return f"http://{host}:{config.env('OPENMETADATA_SERVICE_PORT', '8585', environ=environ)}/api"


def run_workflow(ctx: Ctx, name: str, workflow: dict[str, Any], command: str) -> tuple[str, str]:
    """The crawl Job (identity, Secrets, resources) running `metadata <command>` on `workflow`: (outcome, log tail)."""
    environ = cfg_environ(ctx.cfg)
    job = _ingestion.ingestion_job(
        environ, image=ingestion_image(ctx), namespace=job_namespace(ctx), service=ctx.service, om_api=om_api(ctx)
    )
    job["metadata"]["name"] = name
    job["metadata"]["labels"] = {"openlakeforge.io/job": P}
    job["spec"]["template"]["metadata"]["labels"] = {"openlakeforge.io/job": P}
    job["spec"]["backoffLimit"] = 0
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["env"][0]["value"] = yaml.safe_dump(workflow)
    container["command"][2] = container["command"][2].replace("metadata ingest", f"metadata {command}")
    namespace = job_namespace(ctx)
    ctx.cleanups.append(
        (
            name,
            lambda: (
                kubectl(ctx.cfg, ["delete", "job", name, "-n", namespace, "--ignore-not-found"], capture=True) and None
            ),
        )
    )
    try:
        k8s.run_job(job, timeout_seconds=1500, environ=environ)
        outcome = "job completed"
    except k8s.KubectlError as exc:
        outcome = f"job failed: {JWT.sub('***', str(exc))[-700:]}"
    logs = kubectl(ctx.cfg, ["logs", f"job/{name}", "-n", namespace, "--tail=200"], capture=True, retry_transient=True)
    return outcome, JWT.sub("***", logs)


def summarise(data: Any) -> Any:
    """A response reduced to what the question needs: its keys, and any profile or sample data it carries."""
    if not isinstance(data, dict):
        return str(data)[:300]
    out: dict[str, Any] = {"keys": sorted(data)}
    for key in ("profile", "sampleData"):
        if key in data:
            out[key] = data[key]
    if isinstance(data.get("columns"), list):
        out["columns[].profile"] = {
            c.get("name"): sorted(k for k, v in (c.get("profile") or {}).items() if v is not None)
            for c in data["columns"]
        }
    if isinstance(data.get("data"), list):
        out["data"] = data["data"][-2:]
    return out


def stored_profile(ctx: Ctx, table: dict[str, Any]) -> dict[str, Any]:
    """What OpenMetadata returns for the table's profile and sample data, by every route that could hold them."""
    fqn, now = quote(table["fullyQualifiedName"]), int(time.time() * 1000)
    window = {"startTs": now - 6 * 3600 * 1000, "endTs": now + 3600 * 1000}
    routes = {
        "table fields=columns,profile,sampleData": (
            "/api/v1/tables/name/" + fqn,
            {"fields": "columns,profile,sampleData"},
        ),
        "tableProfile": (f"/api/v1/tables/{fqn}/tableProfile", window),
        "sampleData": (f"/api/v1/tables/{table['id']}/sampleData", None),
    }
    found: dict[str, Any] = {}
    for label, (path, params) in routes.items():
        r = om(ctx, "GET", path, params=params)
        found[label] = {"result": r.brief(), "body": summarise(r.data)}
    columns: dict[str, Any] = {}
    for column in table["columns"]:
        r = om(ctx, "GET", f"/api/v1/tables/{quote(column['fullyQualifiedName'])}/columnProfile", params=window)
        rows = r.data.get("data") if isinstance(r.data, dict) else None
        columns[column["name"]] = {
            "dataType": column.get("dataTypeDisplay"),
            "result": r.brief(),
            "metrics": sorted({k for row in rows or [] for k, v in row.items() if v is not None}),
            "latest": (rows or [None])[-1],
        }
    found["columnProfile per column"] = columns
    return found


def table_statistics(found: dict[str, Any]) -> list[str]:
    rows = (
        (found["tableProfile"]["body"] or {}).get("data") if isinstance(found["tableProfile"]["body"], dict) else None
    )
    return sorted({k for row in rows or [] for k in row if k != "timestamp"})


def column_statistics(found: dict[str, Any]) -> list[str]:
    seen = {m for c in found["columnProfile per column"].values() for m in c["metrics"] if m != "timestamp"}
    return sorted(seen)


def has_sample_data(found: dict[str, Any]) -> bool:
    bodies = [found["table fields=columns,profile,sampleData"]["body"], found["sampleData"]["body"]]
    return any(isinstance(b, dict) and b.get("sampleData") for b in bodies)


PROFILE_TABLE = f"{P}_profile"


def profile_target(ctx: Ctx) -> dict[str, Any]:
    """A throwaway table with a decimal, a date, a varchar and a bigint column, known values and one NULL each."""
    schema = ctx.fix["probe_schema"]
    t = f"{ctx.catalog}.{schema}.{PROFILE_TABLE}"
    ctx.cleanups.append(("trino profile table", lambda: trino_try(ctx, f"DROP TABLE IF EXISTS {t}") and None))
    fqn = table_fqn(ctx, schema, PROFILE_TABLE)
    ctx.cleanups.append(
        (
            "om profile table",
            lambda: om(ctx, "DELETE", f"/api/v1/tables/name/{quote(fqn)}", params={"hardDelete": "true"}) and None,
        )
    )
    created = (
        trino_try(ctx, f"DROP TABLE IF EXISTS {t}"),
        trino_try(
            ctx,
            f"CREATE TABLE {t} AS SELECT * FROM (VALUES "
            "(CAST(10.50 AS decimal(18,2)), DATE '2026-01-01', 'a', CAST(1 AS bigint)), "
            "(CAST(20.25 AS decimal(18,2)), DATE '2026-02-01', 'b', CAST(2 AS bigint)), "
            "(CAST(NULL AS decimal(18,2)), DATE '2026-03-01', 'b', CAST(3 AS bigint)), "
            "(CAST(40.00 AS decimal(18,2)), DATE '2026-04-01', CAST(NULL AS varchar), CAST(4 AS bigint))"
            ") AS v(amount, order_date, name, id)",
        ),
    )
    if not created[1]["ok"]:
        raise E2EError(f"cannot create the profile table: {created[1]['output']}")
    crawled, text = run_crawl(ctx, "P")
    if not crawled:
        raise E2EError(text)
    row = get_table(ctx, fqn, "columns")
    if not row.ok:
        raise E2EError(f"the crawl did not register {fqn}: {row.brief()}")
    return row.data


def probe_profile(ctx: Ctx) -> None:
    f = ctx.finding(
        "7",
        "`metadata profile` on a decimal, date, varchar and bigint probe table",
    )
    a = profile_target(ctx)
    schema, table = a["fullyQualifiedName"].split(".")[-2], a["name"]
    base = {
        "type": "Profiler",
        "computeTableMetrics": True,
        "computeColumnMetrics": True,
        "threadCount": 1,
        "timeoutSeconds": 900,
        "includeViews": False,
        "databaseFilterPattern": {"includes": [f"^{ctx.catalog}$"]},
        "schemaFilterPattern": {"includes": [f"^{schema}$"]},
        "tableFilterPattern": {"includes": [f"^{table}$"]},
    }
    variants = {"explicit metric list": {**base, "metrics": PROFILE_METRICS}, "default metrics": base}
    workflow = _ingestion.ingestion_config(cfg_environ(ctx.cfg), service=ctx.service, om_api=om_api(ctx))
    workflow["processor"] = {"type": "orm-profiler", "config": {}}
    runs: list[dict[str, Any]] = []
    f.payload = {"processor": workflow["processor"], "sourceConfig variants": variants}
    f.response = {"runs": runs}
    for label, source_config in variants.items():
        workflow["source"]["sourceConfig"]["config"] = source_config
        outcome, logs = run_workflow(ctx, f"{P}-profile", workflow, "profile")
        found = stored_profile(ctx, a)
        runs.append({"variant": label, "outcome": outcome, "log tail": logs[-3500:], "stored": found})
        sample, table_stats, column_stats = has_sample_data(found), table_statistics(found), column_statistics(found)
        f.detail = (
            f"{label}: {outcome}; table-level metrics {table_stats}; column-level metrics {column_stats}; "
            f"sample data present: {sample}"
        )
        if outcome != "job completed":
            f.verdict = "REFUSED"
            return
        if sample:
            f.verdict = "SAMPLE DATA STORED"
            return
        if column_stats:
            f.verdict = "ACCEPTED"
            return
        f.verdict = "TABLE LEVEL ONLY" if table_stats else "NOTHING STORED"


def probe_override_crawl(ctx: Ctx) -> None:
    f = ctx.finding("5b", "Crawl with overrideMetadata: refreshes descriptions, and what else it overwrites")
    workflow = _ingestion.ingestion_config(cfg_environ(ctx.cfg), service=ctx.service, om_api=om_api(ctx))
    # Only tables with a restoration snapshot (the two patched Gold tables) and the probe's own table: a catalog-wide
    # override crawl would rewrite the description of every table whose Trino comment differs.
    targets = [ctx.fix[k]["fullyQualifiedName"] for k in ("table_a", "table_b")]
    targets.append(table_fqn(ctx, ctx.fix["probe_schema"], TRINO_TABLE))
    config_ = workflow["source"]["sourceConfig"]["config"]
    config_["overrideMetadata"] = True
    config_["useFqnForFiltering"] = True
    config_["tableFilterPattern"] = {"includes": [f"^{re.escape(fqn)}$" for fqn in targets]}
    outcome, logs = run_workflow(ctx, f"{P}-override-crawl", workflow, "ingest")
    row = get_table(ctx, table_fqn(ctx, ctx.fix["probe_schema"], TRINO_TABLE), "columns").data or {}
    columns = {c["name"]: c.get("description") for c in row.get("columns") or []}
    a = get_table(ctx, ctx.fix["table_a"]["fullyQualifiedName"]).data or {}
    cols = a.get("columns") or []
    kept = {c.get("constraintType") for c in a.get("tableConstraints") or []}
    tags = [t.get("tagFQN") for t in cols[0].get("tags") or []]
    f.payload = {"sourceConfig.overrideMetadata": True}
    f.response = {
        "outcome": outcome,
        "log tail": logs[-1500:],
        "probe table": {"description": row.get("description"), "columns": columns},
        "Gold table A after": {
            "constraints": sorted(kept),
            "column 0 tags": tags,
            "column 1 description": cols[1].get("description"),
        },
    }
    f.detail = (
        f"{outcome}; table description {row.get('description')!r}; column id {columns.get('id')!r} "
        f"(a human had set 'human description'); A keeps constraints {sorted(kept)}, glossary tag "
        f"{'kept' if TERM_FQN in tags else 'gone'}, human column description "
        f"{'kept' if cols[1].get('description') == 'human column description' else 'gone'}"
    )
    f.verdict = (
        "REFUSED"
        if outcome != "job completed"
        else "ACCEPTED"
        if row.get("description") == "probe table comment v2"
        else "IGNORED"
    )


def restore(ctx: Ctx) -> None:
    """Put the two patched Gold tables back as they were crawled; every failure is recorded, none stops the rest."""
    for key in ("table_a", "table_b"):
        table = ctx.fix.get(key)
        if not table:
            continue
        try:
            original = ctx.fix["originals"][table["id"]]
            current = (
                om(ctx, "GET", f"/api/v1/tables/{table['id']}", params={"fields": "columns,tableConstraints,tags"}).data
                or {}
            )
            ops: list[dict[str, Any]] = []
            if original["constraints"]:
                ops.append(add("/tableConstraints", original["constraints"]))
            elif current.get("tableConstraints"):
                ops.append({"op": "remove", "path": "/tableConstraints"})
            if original["description"] and current.get("description") != original["description"]:
                ops.append(add("/description", original["description"]))
            now = current.get("columns") or []
            for index, column in enumerate(original["columns"]):
                if index >= len(now):
                    break
                if (now[index].get("tags") or []) != (column.get("tags") or []):
                    ops.append(add(f"/columns/{index}/tags", column.get("tags") or []))
                if column.get("description") and now[index].get("description") != column["description"]:
                    ops.append(add(f"/columns/{index}/description", column["description"]))
                elif not column.get("description") and now[index].get("description"):
                    ops.append({"op": "remove", "path": f"/columns/{index}/description"})
            if ops:
                done = patch(ctx, "tables", table["id"], *ops)
                if not done.ok:
                    ctx.restore_errors.append(f"{table['fullyQualifiedName']}: {done.brief()}")
        except Exception as exc:  # noqa: BLE001 - the other table and the cleanup still need to run
            ctx.restore_errors.append(f"{key}: {JWT.sub('***', str(exc))[:200]}")


def guarded(ctx: Ctx, ids: tuple[str, ...], step: Callable[[Ctx], None]) -> None:
    try:
        step(ctx)
    except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the others
        message = JWT.sub("***", f"{type(exc).__name__}: {exc}")[:600]
        print(f"probe step {step.__name__} failed: {message}", file=sys.stderr)
        traceback.print_exc()
        for id in ids:
            f = ctx.findings.get(id)
            if f is None:
                f = ctx.finding(id, QUESTIONS.get(id, id))
            f.verdict, f.detail = "ERROR", message


QUESTIONS = {
    "1": "PATCH DataProduct.lifecycleStage with the automation bot",
    "3": "Create a Metric (expression, type, owners, domain, data product, glossary tag, extension)",
    "3b": "Lineage edge table -> metric",
    "4": "PATCH Table.tableConstraints and survive the re-crawl",
    "5": "Iceberg comments through Trino and Polaris, then OpenMetadata descriptions",
    "6": "Glossary term tag on a column survives the re-crawl",
    "5b": "Crawl with overrideMetadata",
    "7": "`metadata profile` on one Gold table",
}


def run_probe(ctx: Ctx) -> None:
    guarded(ctx, ("S",), probe_schemas)
    # The suite builds the Gold tables after activation; OpenMetadata learns of them only at the next crawl, and the
    # hourly CronJob may not have run since.
    ok, text = run_crawl(ctx, "0")
    crawl = ctx.finding("C", "Gold tables reach OpenMetadata only through a crawl after the pipelines run")
    crawl.note(text)
    crawl.verdict = "RECORDED" if ok else "ERROR"
    setup(ctx)
    guarded(ctx, ("1",), probe_lifecycle)
    guarded(ctx, ("2a", "2b", "2c", "2d", "2e", "2f", "2g"), probe_native_fields)
    guarded(ctx, ("3", "3b"), probe_metric)
    guarded(ctx, ("5",), probe_trino_comments)
    guarded(ctx, ("4", "6"), apply_constraints_and_tags)
    crawled_a, first = run_crawl(ctx, "A")
    for id in ("4", "5", "6"):
        if id in ctx.findings:
            ctx.findings[id].note(first)
            if not crawled_a:
                ctx.findings[id].verdict = "INCONCLUSIVE"
    if crawled_a:
        guarded(ctx, ("4", "6"), lambda c: verify_after_crawl(c, "crawl A"))
        guarded(ctx, ("5",), lambda c: verify_descriptions(c, "crawl A", "probe table comment", "probe id comment"))
    if crawled_a and "5" in ctx.findings and ctx.fix.get("probe_schema"):
        t = f"{ctx.catalog}.{ctx.fix['probe_schema']}.{TRINO_TABLE}"
        fqn = table_fqn(ctx, ctx.fix["probe_schema"], TRINO_TABLE)
        human = patch(
            ctx, "tables", get_table(ctx, fqn).data.get("id", ""), add("/columns/0/description", "human description")
        )
        ctx.findings["5"].response["human edit of column id"] = human.brief()
        ctx.findings["5"].response["trino v2"] = [
            trino_try(ctx, f"COMMENT ON TABLE {t} IS 'probe table comment v2'"),
            trino_try(ctx, f"COMMENT ON COLUMN {t}.id IS 'probe id comment v2'"),
        ]
        crawled_b, second = run_crawl(ctx, "B")
        ctx.findings["5"].note(second)
        if crawled_b:
            guarded(
                ctx,
                ("5",),
                lambda c: verify_descriptions(
                    c,
                    "crawl B (Trino comments now v2, id column edited by a human)",
                    "probe table comment v2",
                    "probe id comment v2",
                ),
            )
            guarded(ctx, ("4", "6"), lambda c: verify_after_crawl(c, "crawl B"))
    guarded(ctx, ("5b",), probe_override_crawl)
    guarded(ctx, ("7",), probe_profile)


def render(ctx: Ctx) -> str:
    rows = ["| # | Question | Verdict | Detail |", "| --- | --- | --- | --- |"]
    for id in sorted(ctx.findings):
        f = ctx.findings[id]
        detail = (f.detail or "").replace("|", "/").replace("\n", " ")[:300]
        rows.append(f"| {id} | {f.question.replace('|', '/')} | **{f.verdict}** | {detail} |")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-f", "--file", required=True, help="Deployment Profile v1 the deployment was created from")
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--json", help="write every payload and response here (JWTs masked)")
    parser.add_argument("--summary", help="append the markdown verdict table here")
    args = parser.parse_args()
    context = deployment_context_for_profile(args.file, stage=args.stage)
    harness_error = ""
    with applied_runtime_environment(context, env="local") as contract:
        cfg = e2e.connect(
            "local",
            namespace=context.namespace,
            shared_namespace=context.shared_namespace,
            kube_context=contract.kube_context,
            repo_root=context.paths.repo_root,
            distribution_root=context.paths.distribution_root,
            deployment=context,
            environ=contract,
        )
        environ = cfg_environ(cfg)
        with openmetadata_client(cfg) as (client, token):
            if not token:
                raise SystemExit("no automation bot token: the probe answers for the bot identity only")
            ctx = Ctx(
                cfg=cfg,
                client=client,
                stage=args.stage,
                database_fqn=config.env("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "", environ=environ),
                catalog=trino.stage_catalog_name(environ),
                service=config.env("OPENMETADATA_CATALOG_SERVICE", environ=environ)
                or config.env("OPENLAKEFORGE_CATALOG_PROVIDER", environ=environ),
            )
            try:
                run_probe(ctx)
            except Exception as exc:  # noqa: BLE001 - the findings so far are still worth reporting
                harness_error = JWT.sub("***", f"{type(exc).__name__}: {exc}")
                print(f"probe could not finish: {harness_error}", file=sys.stderr)
                ctx.finding("H", "The probe ran to the end").verdict = "ERROR"
                ctx.findings["H"].detail = harness_error
            finally:
                restore(ctx)
                if ctx.restore_errors:
                    harness_error = harness_error or "the Gold tables were not restored"
                    ctx.finding("R", "The patched Gold tables were restored").verdict = "ERROR"
                    ctx.findings["R"].detail = "; ".join(ctx.restore_errors)
                for name, undo in reversed(ctx.cleanups):
                    try:
                        undo()
                    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                        print(f"cleanup {name}: {JWT.sub('***', str(exc))[:200]}", file=sys.stderr)
    table = render(ctx)
    print("\n" + table)
    if args.json:
        Path(args.json).write_text(json.dumps(scrub([asdict(ctx.findings[k]) for k in sorted(ctx.findings)]), indent=2))
    if args.summary:
        with open(args.summary, "a") as out:
            out.write("### OpenMetadata governance probe\n\n" + table + "\n")
    return 1 if harness_error else 0


if __name__ == "__main__":
    sys.exit(main())
