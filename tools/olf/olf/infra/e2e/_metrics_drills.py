"""The metrics backend's outage drill and footprint record (ADR 0024, slice M-f, #21).

`check_metrics_outage` scales Prometheus to zero. Every Prometheus series, the catalog and the stage health route's
`metrics` integration must then read `unavailable` within the API's read deadline, never `available` and never an
empty answer, while a pipeline run still succeeds; Prometheus is always scaled back and waited for.

`check_metrics_footprint` measures what Prometheus and kube-state-metrics cost on this run: CPU and memory from the
kubelet's stats summary, as `_log_footprint` measures the collector, the data volume's use and the head series. It is
one CI run on a deployment minutes old, not the 24 hours per size ADR 0024 asks for, so it is recorded as evidence
(`.tmp/e2e-evidence/`, the job summary and a `METRICS_FOOTPRINT_JSON` line), not compared with a published table.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from openlakeforge_core.metrics import MetricScope, MetricSource, catalog_for

from olf.common import log
from olf.infra.e2e import _metrics as metrics
from olf.infra.e2e._dagster import launch_and_poll_dagster_jobs
from olf.infra.e2e._log_footprint import SUMMARY_PATH_ENV, Usage, container_usage, parse_size
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl

PROMETHEUS = "prometheus"
KUBE_STATE_METRICS = "kube-state-metrics"
DATA_PATH = "/prometheus"
# The API holds every metrics read to 5 s (metrics_routes.READ_DEADLINE_SECONDS); the rest is the ingress and TLS.
RESPONSE_BOUND_SECONDS = 15.0
SCALE_TIMEOUT_SECONDS = 180
RECOVERY_TIMEOUT_SECONDS = 300
POLL_SECONDS = 10.0
FOOTPRINT_SECONDS = 120
FOOTPRINT_SAMPLE_SECONDS = 10.0


def outage_problem(envelope: Mapping[str, Any]) -> str | None:
    """Why a series read during the outage is not the `unavailable` answer with no lines, or None."""
    if envelope.get("state") != "unavailable":
        return f"reads {envelope.get('state')!r} ({envelope.get('reason')})"
    if envelope.get("lines"):
        return "is unavailable but carries lines"
    return None


def catalog_outage_problems(catalog: Mapping[str, Any]) -> list[str]:
    """Each catalog entry whose state does not follow its source: Prometheus's down, Dagster's still answering."""
    problems = []
    for entry in catalog.get("series", []):
        down = entry.get("source") == MetricSource.PROMETHEUS.value
        if down and entry.get("state") != "unavailable":
            problems.append(f"{entry['name']} is listed {entry.get('state')!r} with Prometheus down")
        if not down and entry.get("state") != "available":
            problems.append(f"{entry['name']} is listed {entry.get('state')!r}; Dagster still answers")
    return problems


def integration(health: Mapping[str, Any], name: str = "metrics") -> str | None:
    return next((i.get("status") for i in health.get("integrations", []) if i.get("name") == name), None)


def prometheus_series_paths(stage: str) -> dict[str, str]:
    """Every Prometheus series of both scopes, read the way the Portal reads it."""
    paths = {
        f"stage {name}": path
        for name, path in metrics.stage_series_paths(stage, "1h").items()
        if _source(name) is MetricSource.PROMETHEUS
    }
    paths.update({f"platform {name}": path for name, path in metrics.platform_series_paths("1h").items()})
    return paths


def _source(name: str) -> MetricSource:
    return next(s.source for s in catalog_for(MetricScope.STAGE) if s.name == name)


def _replicas(cfg: E2EConfig) -> int:
    out = kubectl(
        cfg,
        ["get", "deployment", PROMETHEUS, "-n", cfg.platform_namespace, "-o", "jsonpath={.spec.replicas}"],
        capture=True,
    )
    return int(out or "1")


def _pods(cfg: E2EConfig, name: str) -> list[dict[str, Any]]:
    out = kubectl(
        cfg,
        ["get", "pods", "-n", cfg.platform_namespace, "-l", f"app.kubernetes.io/name={name}", "-o", "json"],
        capture=True,
    )
    return list(json.loads(out)["items"])


def _wait(done: Callable[[], bool], what: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not done():
        if time.monotonic() >= deadline:
            raise E2EError(f"timed out after {timeout_seconds:.0f}s waiting for {what}")
        time.sleep(POLL_SECONDS)


@contextmanager
def prometheus_down(cfg: E2EConfig) -> Iterator[None]:
    """Prometheus scaled to zero with its pod gone; scaled back and Ready on exit, whatever happened."""
    replicas = _replicas(cfg)
    deployment = ["deployment", PROMETHEUS, "-n", cfg.platform_namespace]
    try:
        kubectl(cfg, ["scale", *deployment, "--replicas=0"])
        _wait(lambda: not _pods(cfg, PROMETHEUS), "the Prometheus pod to go", SCALE_TIMEOUT_SECONDS)
        log.info("Prometheus is scaled to zero and its pod is gone.")
        yield
    finally:
        kubectl(cfg, ["scale", *deployment, f"--replicas={replicas}"])
        kubectl(cfg, ["rollout", "status", *deployment[:2], "-n", cfg.platform_namespace, "--timeout=300s"])
        log.info(f"Prometheus is scaled back to {replicas} and Ready.")


def _read_during_outage(api: metrics.ApiSession, stage: str, when: str) -> None:
    problems = []
    for name, path in prometheus_series_paths(stage).items():
        reply = api.get(path, api.reader)
        took = reply.elapsed.total_seconds()
        if reply.status_code != 200:
            problems.append(f"{name}: HTTP {reply.status_code}")
        elif problem := outage_problem(reply.json()):
            problems.append(f"{name} {problem}")
        if took > RESPONSE_BOUND_SECONDS:
            problems.append(f"{name} took {took:.1f}s, past the {RESPONSE_BOUND_SECONDS:g}s bound")
    problems += catalog_outage_problems(api.read(f"/api/v1/stages/{stage}/metrics"))
    if (status := integration(api.read(f"/api/v1/stages/{stage}/health"))) != "unavailable":
        problems.append(f"the stage health route's metrics integration reads {status!r}")
    if problems:
        raise E2EError(f"with Prometheus down ({when}): " + "; ".join(problems))
    log.info(f"With Prometheus down ({when}), every metrics read is unavailable within the bound.")


def check_metrics_outage(cfg: E2EConfig) -> None:
    """Prometheus at zero reads `unavailable` everywhere, never healthy, and a pipeline run still succeeds."""
    if not metrics.metrics_on(metrics.metrics_contract(cfg)):
        log.info("Skipping the metrics outage drill: metrics are not served on this deployment.")
        return
    stage = metrics._stage(cfg)  # noqa: SLF001 - one helper, one home
    with metrics.api_session(cfg) as api:
        if api is None:
            return
        log.step("Checking a metrics outage reads unavailable and leaves pipelines running...")
        health = f"/api/v1/stages/{stage}/health"
        if (before := integration(api.read(health))) != "available":
            raise E2EError(f"the metrics integration reads {before!r} before the outage; the drill would prove nothing")
        with prometheus_down(cfg):
            _read_during_outage(api, stage, "at once")
            launch_and_poll_dagster_jobs(cfg, products=(cfg.inventory.default_product,))
            _read_during_outage(api, stage, "after a pipeline run")
        _wait(lambda: integration(api.read(health)) == "available", "the metrics integration to recover", 180)
        log.info("The metrics integration reads available again.")


@dataclass(frozen=True)
class WorkloadFootprint:
    name: str
    cpu_millicores_mean: float
    cpu_millicores_peak: float
    working_set_peak_bytes: int
    memory_request_bytes: int | None
    memory_limit_bytes: int | None
    cpu_request: str | None
    cpu_limit: str | None


def summarize(
    name: str, samples: Sequence[tuple[float, Mapping[str, Usage]]], pod: Mapping[str, Any]
) -> WorkloadFootprint:
    """A pod's CPU as the mean and busiest interval, and its peak working set, summed over its containers."""
    if len(samples) < 2 or samples[-1][0] <= samples[0][0]:
        raise E2EError(f"{name}: fewer than two samples over time, so no CPU rate can be computed")
    cpu = [sum(u.cpu_nanoseconds for u in usage.values()) for _, usage in samples]
    at = [moment for moment, _ in samples]
    rates = [(cpu[i + 1] - cpu[i]) / (at[i + 1] - at[i]) / 1e6 for i in range(len(samples) - 1) if at[i + 1] > at[i]]
    resources = pod["spec"]["containers"][0].get("resources", {})
    memory_request = resources.get("requests", {}).get("memory")
    memory_limit = resources.get("limits", {}).get("memory")
    return WorkloadFootprint(
        name=name,
        cpu_millicores_mean=(cpu[-1] - cpu[0]) / (at[-1] - at[0]) / 1e6,
        cpu_millicores_peak=max(rates),
        working_set_peak_bytes=max(sum(u.working_set_bytes for u in usage.values()) for _, usage in samples),
        memory_request_bytes=parse_size(memory_request) if memory_request else None,
        memory_limit_bytes=parse_size(memory_limit) if memory_limit else None,
        cpu_request=resources.get("requests", {}).get("cpu"),
        cpu_limit=resources.get("limits", {}).get("cpu"),
    )


def volume_used_bytes(summary: Mapping[str, Any], namespace: str, pod: str, claim: str) -> int | None:
    """The claim's used bytes in a kubelet stats summary; None where the volume plugin reports no stats."""
    for entry in summary.get("pods", []):
        ref = entry.get("podRef", {})
        if ref.get("name") == pod and ref.get("namespace") == namespace:
            for volume in entry.get("volume", []):
                if volume.get("pvcRef", {}).get("name") == claim and "usedBytes" in volume:
                    return int(volume["usedBytes"])
    return None


def size_for_volume(sizes: Mapping[str, Any], volume: str) -> str | None:
    """The `spec.storage.size` row of release/storage-sizes.yaml whose metrics volume is `volume`."""
    return next((name for name, row in sizes.items() if row.get("metrics", {}).get("volume") == volume), None)


def render_footprint(workloads: Sequence[WorkloadFootprint], record: Mapping[str, Any]) -> str:
    mib = 1024**2
    rows = [
        "| Workload | CPU mean (m) | CPU busiest interval (m) | Memory peak (MiB) | Memory request / limit (MiB) |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for w in workloads:
        request = "n/a" if w.memory_request_bytes is None else f"{w.memory_request_bytes / mib:.0f}"
        limit = "n/a" if w.memory_limit_bytes is None else f"{w.memory_limit_bytes / mib:.0f}"
        rows.append(
            f"| {w.name} | {w.cpu_millicores_mean:.0f} | {w.cpu_millicores_peak:.0f} "
            f"| {w.working_set_peak_bytes / mib:.0f} | {request} / {limit} |"
        )
    used = record["volume_used_bytes"]
    used_text = "not reported" if used is None else f"{used / mib:.1f} MiB"
    rows += [
        "",
        f"Size `{record['size']}`, volume {record['volume']}: {used_text}"
        f" used after {record['prometheus_age_seconds'] / 60:.0f} minutes. Head series: {record['head_series']:.0f}"
        f" (budget {metrics.SERIES_BUDGET}).",
    ]
    return "\n".join(rows)


def _summary(cfg: E2EConfig, node: str) -> dict[str, Any]:
    return dict(json.loads(kubectl(cfg, ["get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary"], capture=True)))


def _du_bytes(cfg: E2EConfig, pod: str) -> int:
    """The data directory's size from inside the pod, for a volume whose plugin reports no stats (kind's local-path)."""
    out = kubectl(cfg, ["exec", pod, "-n", cfg.platform_namespace, "--", "du", "-sk", DATA_PATH], capture=True)
    return int(out.split()[0]) * 1024


def _restarts(pods: Sequence[Mapping[str, Any]]) -> int:
    return sum(s["restartCount"] for p in pods for s in p["status"].get("containerStatuses", []))


def check_metrics_footprint(cfg: E2EConfig) -> None:
    """Record Prometheus's and kube-state-metrics' CPU, memory and volume use, and the head series."""
    if not metrics.metrics_on(contract := metrics.metrics_contract(cfg)):
        log.info("Skipping the metrics footprint: metrics are not served on this deployment.")
        return
    log.step(f"Measuring the metrics backend's footprint over {FOOTPRINT_SECONDS}s...")
    pods = {
        name: [p for p in _pods(cfg, name) if p["status"].get("phase") == "Running"]
        for name in (PROMETHEUS, KUBE_STATE_METRICS)
    }
    if not all(pods.values()):
        raise E2EError(f"a metrics workload has no running pod: { {n: len(p) for n, p in pods.items()} }")
    restarts_before = _restarts([p for found in pods.values() for p in found])
    targets = {name: found[0] for name, found in pods.items()}
    samples: dict[str, list[tuple[float, Mapping[str, Usage]]]] = {name: [] for name in targets}
    summaries: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + FOOTPRINT_SECONDS
    while True:
        summaries = {node: _summary(cfg, node) for node in {p["spec"]["nodeName"] for p in targets.values()}}
        moment = time.monotonic()
        for name, pod in targets.items():
            usage = container_usage(summaries[pod["spec"]["nodeName"]], cfg.platform_namespace, pod["metadata"]["name"])
            samples[name].append((moment, usage))
        if moment >= deadline:
            break
        time.sleep(FOOTPRINT_SAMPLE_SECONDS)
    workloads = [summarize(name, samples[name], targets[name]) for name in targets]
    prometheus = targets[PROMETHEUS]
    claim = json.loads(
        kubectl(cfg, ["get", "pvc", PROMETHEUS, "-n", cfg.platform_namespace, "-o", "json"], capture=True)
    )
    volume = claim["spec"]["resources"]["requests"]["storage"]
    used = volume_used_bytes(
        summaries[prometheus["spec"]["nodeName"]], cfg.platform_namespace, prometheus["metadata"]["name"], PROMETHEUS
    )
    used_from = "kubelet volume stats"
    if used is None:
        used, used_from = _du_bytes(cfg, prometheus["metadata"]["name"]), "du of the data directory"
    sizes = yaml.safe_load((cfg.distribution_root / "release/storage-sizes.yaml").read_text())["sizes"]
    started = datetime.fromisoformat(prometheus["status"]["startTime"].replace("Z", "+00:00"))
    record = {
        "namespace": cfg.namespace,
        "measured_seconds": FOOTPRINT_SECONDS,
        "size": size_for_volume(sizes, volume) or "unknown",
        "volume": volume,
        "volume_used_bytes": used,
        "volume_used_from": used_from,
        "prometheus_age_seconds": (datetime.now(UTC) - started).total_seconds(),
        "head_series": metrics.head_series(cfg, str(contract["query_url"])),
        "retention_days": contract.get("retention_days"),
        "workloads": [asdict(w) for w in workloads],
    }
    _publish(cfg, render_footprint(workloads, record), record)
    after = _restarts([p for name in targets for p in _pods(cfg, name)])
    if after != restarts_before:
        raise E2EError(f"a metrics workload restarted while it was measured ({restarts_before} -> {after})")
    for w in workloads:
        if w.memory_limit_bytes is not None and w.working_set_peak_bytes >= w.memory_limit_bytes:
            raise E2EError(f"{w.name} reached its {w.memory_limit_bytes} byte memory limit")


def _publish(cfg: E2EConfig, table: str, record: Mapping[str, Any]) -> None:
    path = cfg.repo_root / ".tmp/e2e-evidence" / f"metrics-footprint-{cfg.namespace}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    log.info("Metrics backend footprint (one CI run, not the 24 hour measurement):\n" + table)
    log.info("METRICS_FOOTPRINT_JSON " + json.dumps(record, sort_keys=True))
    if summary := cfg_environ(cfg).get(SUMMARY_PATH_ENV):
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("## Metrics backend footprint\n\n" + table + "\n\n")
