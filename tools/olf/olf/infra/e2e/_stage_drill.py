"""Assertions for the stage lifecycle drill (#1049, #1050).

The drill reconfigures the profile and redeploys (`.github/actions/stage-drill`), so these checks run between its
deploys instead of inside a suite. Each takes the stages that must have a thing and the stages that must not, and
reports every violation at once: `crawl` is the Trino crawl's Polaris credential copy and workloads, `namespaces`
the stage namespaces, `principals` the stage's Polaris principals. They read the cluster and Polaris, never a Secret's
value.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Collection, Mapping, Sequence

from olf.common import log
from olf.common.deployment.context import stage_namespace
from olf.infra import k8s
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise
from olf.infra.openmetadata._ingestion import JOB_NAME, POLARIS_SECRET
from olf.infra.polaris import PolarisClient, PolarisConfig
from olf.infra.polaris_bootstrap import ROOT_CREDENTIALS_SECRET, STAGE_PRINCIPALS

CRAWL_SELECTOR = f"openlakeforge.io/job={JOB_NAME}"


def stage_principals(stage: str) -> tuple[str, ...]:
    """What a stage keeps in Polaris, and `revoke_removed_stages` deletes with the stage."""
    return (f"olf-{stage}-processing", f"deployer-{stage}", f"trino-catalog-{stage}")


def principal_stage(principal: str) -> str | None:
    """The stage a Polaris principal belongs to, by the names `revoke_removed_stages` matches."""
    match = STAGE_PRINCIPALS.fullmatch(principal)
    return next(group for group in match.groups() if group) if match else None


def crawl_problems(
    held: Mapping[str, Collection[str]], present: Sequence[str], absent: Sequence[str]
) -> list[str]:
    """`held` maps a stage to the `Kind/name` of what its namespace holds of the crawl.

    A stage that must have the crawl needs its Secret and its CronJob (the Jobs come and go with the schedule); one
    that must not may have none of the three kinds.
    """
    problems = []
    for stage in present:
        if f"Secret/{POLARIS_SECRET}" not in held[stage]:
            problems.append(f"{stage} has no Secret/{POLARIS_SECRET}, but its governance is on")
        # Named by the catalog it crawls (`ingestion_cronjob`), so any CronJob carrying the crawl's label counts.
        if not any(item.startswith("CronJob/") for item in held[stage]):
            problems.append(f"{stage} has no crawl CronJob, but its governance is on")
    for stage in absent:
        for found in sorted(held[stage]):
            problems.append(f"{stage} still has {found}, but its governance is off")
    return problems


def namespace_problems(namespaces: Collection[str], present: Sequence[str], absent: Sequence[str]) -> list[str]:
    wanted = {stage_namespace(stage) for stage in present}
    unwanted = {stage_namespace(stage) for stage in absent}
    return [
        *(f"namespace {name} is missing" for name in sorted(wanted - set(namespaces))),
        *(f"namespace {name} still exists" for name in sorted(unwanted & set(namespaces))),
    ]


def principal_problems(principals: Collection[str], present: Sequence[str], absent: Sequence[str]) -> list[str]:
    problems = [
        f"Polaris principal {name} is missing"
        for stage in present
        for name in stage_principals(stage)
        if name not in principals
    ]
    for stage in absent:
        problems.extend(
            f"Polaris principal {name} still exists" for name in sorted(principals) if principal_stage(name) == stage
        )
    return problems


def _items(cfg: E2EConfig, args: list[str]) -> list[dict]:
    return list(json.loads(kubectl(cfg, [*args, "-o", "json"], capture=True, retry_transient=True)).get("items", []))


def crawl_in_namespace(cfg: E2EConfig, namespace: str) -> set[str]:
    """`Kind/name` of the crawl's Secret, CronJob and Jobs in `namespace`; empty when the namespace is gone."""
    workloads = _items(cfg, ["get", "cronjob,job", "-n", namespace, "-l", CRAWL_SELECTOR])
    found = {f"{item['kind']}/{item['metadata']['name']}" for item in workloads}
    names = kubectl(cfg, ["get", "secrets", "-n", namespace, "-o", "name"], capture=True, retry_transient=True)
    if f"secret/{POLARIS_SECRET}" in names.split():
        found.add(f"Secret/{POLARIS_SECRET}")
    return found


def cluster_namespaces(cfg: E2EConfig) -> set[str]:
    return {item["metadata"]["name"] for item in _items(cfg, ["get", "namespaces"])}


def polaris_principals(cfg: E2EConfig) -> set[str]:
    """Every principal Polaris holds, listed as root through a port-forward, as the bootstrap does."""
    contracts = load_provider_contracts_or_raise(cfg)
    rest_uri = next(stage["catalog"]["rest_uri"] for stage in contracts["stages"].values())
    service, _, namespace = (urllib.parse.urlparse(rest_uri).hostname or "").partition(".")
    port = urllib.parse.urlparse(rest_uri).port or 8181
    environ = cfg.environ
    log_path = "/tmp/openlakeforge-stage-drill.log"
    with k8s.port_forward(service, port, namespace, log_path=log_path, environ=environ) as local:

        def root(key: str) -> str:
            return k8s.secret_value(ROOT_CREDENTIALS_SECRET, key, namespace, environ=environ)

        client = PolarisClient(
            PolarisConfig(
                f"http://127.0.0.1:{local}",
                "",
                client_id=root("ROOT_CLIENT_ID"),
                client_secret=root("ROOT_CLIENT_SECRET"),
            )
        )
        client.login()
        body = client.management("GET", "/principals")
    return {entry["name"] for entry in body.get("principals") or ()}


def _conclude(what: str, problems: list[str]) -> None:
    if problems:
        raise E2EError(f"{what}: " + "; ".join(problems) + ".")
    log.info(f"{what}: as expected.")


def check_crawl(cfg: E2EConfig, present: Sequence[str], absent: Sequence[str]) -> None:
    held = {stage: crawl_in_namespace(cfg, stage_namespace(stage)) for stage in (*present, *absent)}
    _conclude("Trino crawl credential and workloads", crawl_problems(held, present, absent))


def check_namespaces(cfg: E2EConfig, present: Sequence[str], absent: Sequence[str]) -> None:
    _conclude("Stage namespaces", namespace_problems(cluster_namespaces(cfg), present, absent))


def check_principals(cfg: E2EConfig, present: Sequence[str], absent: Sequence[str]) -> None:
    _conclude("Stage Polaris principals", principal_problems(polaris_principals(cfg), present, absent))
