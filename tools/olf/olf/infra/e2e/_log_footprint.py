"""What the log collector costs per node at three log rates, measured on the running cluster (ADR 0022, #1123).

ADR 0022 declines to state the collector's CPU and memory because upstream publishes none for this configuration, so
the numbers come from here. Four phases run on the node of one collector pod: the stack's own background logging
(`idle`), then a Job in the stage namespace writing `lines_per_second` lines for `PHASE_SECONDS`. During each phase
the kubelet's stats summary gives the collector's containers' CPU time and memory, and the spool probe gives the S3
spool's bytes. Afterwards the archive's objects are read straight from the bucket to count what arrived and what the
objects weigh, so a drop at a rate is a number and not a surprise.

The kubelet summary is used because kind has no metrics-server and `kubectl top` would need one; the summary is the
source metrics-server itself reads. Local only, like the other collector checks: it needs the SeaweedFS port-forward.
The measured table is logged, written to the job summary when GitHub provides one, and printed as one JSON line
(`LOG_FOOTPRINT_JSON`) for the docs to quote.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openlakeforge_core import parse_spool_report

from olf.common import log
from olf.infra.e2e import _log_collector as collector
from olf.infra.e2e import _log_delivery as delivery
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl

TOKEN_PREFIX = "olf-e2e-fp"
IDLE_SECONDS = 60
PHASE_SECONDS = 90
SAMPLE_SECONDS = 5.0
# After the emitter exits the collector still holds its last chunks; one more sample sees that tail of work.
TAIL_SECONDS = 10.0
ARCHIVE_TIMEOUT_SECONDS = 20 * 60
ARCHIVE_POLL_SECONDS = 30.0
# Reads this many polls apart and equal, with the spool empty, mean the archive has stopped growing.
STABLE_POLLS = 3
# 32 groups of 4 hex digits seeded per awk call: text that gzip shrinks about 2x, the worst case for the archive's
# size. Real log text compresses better, and constant padding would flatter the number.
PAYLOAD_GROUPS = 32
SUMMARY_PATH_ENV = "GITHUB_STEP_SUMMARY"
_FOOTPRINT_CONTAINERS = ("fluent-bit", "spool-probe")


@dataclass(frozen=True)
class Size:
    name: str
    lines_per_second: int


# Rates a stage namespace can plausibly reach: a quiet one, a busy Dagster run, and a burst of several Floe pods.
# The heavy rate is reported, not required to lose nothing: where it does lose lines is what an operator needs.
SIZES = (Size("light", 100), Size("moderate", 1000), Size("heavy", 5000))
# A size that must reach the archive whole. The heavy size is measured past what the platform promises.
LOSSLESS = frozenset({"light", "moderate"})


@dataclass(frozen=True)
class Usage:
    cpu_nanoseconds: int
    working_set_bytes: int
    rss_bytes: int


@dataclass(frozen=True)
class Sample:
    at: float
    containers: Mapping[str, Usage]
    spool_bytes: int | None


@dataclass(frozen=True)
class PhaseStats:
    name: str
    lines_per_second: int
    seconds: float
    cpu_millicores: Mapping[str, float]
    cpu_peak_millicores: Mapping[str, float]
    working_set_peak_bytes: Mapping[str, int]
    rss_peak_bytes: Mapping[str, int]
    spool_peak_bytes: int | None


@dataclass(frozen=True)
class ArchiveStats:
    objects: int
    compressed_bytes: int
    uncompressed_bytes: int
    records: int
    seen: Mapping[str, set[int]]


def parse_size(value: str) -> int:
    """Bytes in a Kubernetes quantity such as `256Mi`; only the binary and decimal suffixes the module sets."""
    match = re.fullmatch(r"(\d+)(Ki|Mi|Gi|K|M|G)?", value.strip())
    if match is None:
        raise E2EError(f"cannot read the size {value!r}")
    factor = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "K": 1000, "M": 1000**2, "G": 1000**3}
    return int(match.group(1)) * factor[match.group(2)]


def container_usage(summary: Mapping[str, Any], namespace: str, pod: str) -> dict[str, Usage]:
    """The usage of each container of `pod` in a kubelet stats summary (`/stats/summary`)."""
    for entry in summary.get("pods", []):
        ref = entry.get("podRef", {})
        if ref.get("name") == pod and ref.get("namespace") == namespace:
            return {
                container["name"]: Usage(
                    cpu_nanoseconds=int(container.get("cpu", {}).get("usageCoreNanoSeconds", 0)),
                    working_set_bytes=int(container.get("memory", {}).get("workingSetBytes", 0)),
                    rss_bytes=int(container.get("memory", {}).get("rssBytes", 0)),
                )
                for container in entry.get("containers", [])
            }
    raise E2EError(f"the kubelet's stats summary has no pod {namespace}/{pod}")


def summarize_phase(name: str, lines_per_second: int, samples: Sequence[Sample]) -> PhaseStats:
    """CPU as the mean and the busiest sample interval, memory and spool as the peak over the samples."""
    if len(samples) < 2:
        raise E2EError(f"phase {name}: fewer than two samples, so no CPU rate can be computed")
    seconds = samples[-1].at - samples[0].at
    if seconds <= 0:
        raise E2EError(f"phase {name}: the samples span no time")
    names = [n for n in _FOOTPRINT_CONTAINERS if n in samples[0].containers]
    if not names:
        raise E2EError(f"phase {name}: no collector container in the stats summary")
    cpu = {
        n: (samples[-1].containers[n].cpu_nanoseconds - samples[0].containers[n].cpu_nanoseconds) / seconds / 1e6
        for n in names
    }
    peak: dict[str, float] = {}
    for n in names:
        rates = [
            (b.containers[n].cpu_nanoseconds - a.containers[n].cpu_nanoseconds) / (b.at - a.at) / 1e6
            for a, b in zip(samples, samples[1:], strict=False)
            if b.at > a.at
        ]
        peak[n] = max(rates)
    spool = [s.spool_bytes for s in samples if s.spool_bytes is not None]
    return PhaseStats(
        name=name,
        lines_per_second=lines_per_second,
        seconds=seconds,
        cpu_millicores=cpu,
        cpu_peak_millicores=peak,
        working_set_peak_bytes={n: max(s.containers[n].working_set_bytes for s in samples) for n in names},
        rss_peak_bytes={n: max(s.containers[n].rss_bytes for s in samples) for n in names},
        spool_peak_bytes=max(spool) if spool else None,
    )


def emit_script(token: str, lines_per_second: int, seconds: int) -> str:
    """A shell loop writing `lines_per_second` numbered lines to stderr each second for `seconds` seconds.

    Batch `k` is paced against the start time, not followed by a fixed sleep, so the time a batch takes to write does
    not lower the rate: the loop sleeps only while it is ahead of `start + k`. A node too slow for the rate runs the
    batches back to back, and the phase's elapsed time shows it.
    """
    awk = (
        'BEGIN{srand(s); for(n=s;n<=e;n++){p=""; '
        f'for(g=0;g<{PAYLOAD_GROUPS};g++) p=p sprintf("%04x",int(rand()*65536)); '
        'print t,"line",n,"ERROR",p}}'
    )
    return (
        f'start=$(date +%s); i=1; k=0; while [ "$k" -lt {seconds} ]; do end=$((i+{lines_per_second}-1)); '
        f"awk -v s=$i -v e=$end -v t={token} '{awk}' >&2; i=$((end+1)); k=$((k+1)); "
        'now=$(date +%s); if [ "$now" -lt "$((start+k))" ]; then sleep $((start+k-now)); fi; done'
    )


def archive_scan(
    objects: Sequence[tuple[int, bytes]], tokens: Sequence[str], *, into: ArchiveStats | None = None
) -> ArchiveStats:
    """Count what a list of (compressed size, gzipped NDJSON body) objects holds: lines per token and bytes."""
    pattern = re.compile(rb"(" + b"|".join(re.escape(t.encode()) for t in tokens) + rb") line (\d+) ")
    seen = {t: set(into.seen[t]) if into else set() for t in tokens}
    compressed = into.compressed_bytes if into else 0
    uncompressed = into.uncompressed_bytes if into else 0
    records = into.records if into else 0
    count = into.objects if into else 0
    for size, body in objects:
        raw = gzip.decompress(body)
        compressed += size
        uncompressed += len(raw)
        count += 1
        for line in raw.splitlines():
            records += 1
            if match := pattern.search(line):
                seen[match.group(1).decode()].add(int(match.group(2)))
    return ArchiveStats(count, compressed, uncompressed, records, seen)


def delivery_ratio(stats: ArchiveStats, token: str, expected: int) -> float:
    return len(stats.seen[token]) / expected if expected else 1.0


def render_table(phases: Sequence[PhaseStats], delivered: Mapping[str, tuple[int, int]], memory_limit: int) -> str:
    """The measured phases as a Markdown table, one row per size."""
    mib = 1024**2
    header = (
        "| Size | Lines/s | Collector CPU mean (m) | CPU busiest 5 s (m) | Collector memory peak (MiB) "
        "| Probe CPU mean (m) | Probe memory peak (MiB) | Spool peak (MiB) | Lines archived |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    rows = []
    for p in phases:
        spool = "n/a" if p.spool_peak_bytes is None else f"{p.spool_peak_bytes / mib:.1f}"
        got, want = delivered.get(p.name, (0, 0))
        archived = f"{got}/{want}" if want else "n/a"
        probe_cpu = p.cpu_millicores.get("spool-probe")
        probe_mem = p.working_set_peak_bytes.get("spool-probe")
        rows.append(
            f"| {p.name} | {p.lines_per_second} | {p.cpu_millicores['fluent-bit']:.0f} "
            f"| {p.cpu_peak_millicores['fluent-bit']:.0f} | {p.working_set_peak_bytes['fluent-bit'] / mib:.0f} "
            f"| {'n/a' if probe_cpu is None else f'{probe_cpu:.0f}'} "
            f"| {'n/a' if probe_mem is None else f'{probe_mem / mib:.0f}'} | {spool} | {archived} |"
        )
    return "\n".join([header, *rows, "", f"Collector memory limit: {memory_limit / mib:.0f} MiB."])


def _collector_pod(cfg: E2EConfig) -> dict[str, Any]:
    pods = json.loads(
        kubectl(
            cfg,
            [
                "get",
                "pods",
                "-n",
                cfg.platform_namespace,
                "-l",
                f"app.kubernetes.io/name={collector.COLLECTOR}",
                "-o",
                "json",
            ],
            capture=True,
        )
    )["items"]
    ready = [p for p in pods if p["spec"].get("nodeName") and p["status"].get("podIP")]
    if not ready:
        raise E2EError("no collector pod is scheduled with an IP")
    return ready[0]


def _memory_limit(pod: Mapping[str, Any]) -> int:
    for container in pod["spec"]["containers"]:
        if container["name"] == "fluent-bit":
            return parse_size(container["resources"]["limits"]["memory"])
    raise E2EError("the collector pod has no fluent-bit container")


@dataclass(frozen=True)
class Target:
    """The collector pod measured, and the platform API pod that reads its spool probe (None: no probe sidecar)."""

    pod: Mapping[str, Any]
    stage: str
    api_pod: str | None

    @property
    def node(self) -> str:
        return str(self.pod["spec"]["nodeName"])


def _target(cfg: E2EConfig) -> Target:
    api = delivery._api_pod(cfg) if delivery._has_probe(cfg) else None  # noqa: SLF001
    return Target(_collector_pod(cfg), delivery._stage(cfg), api)  # noqa: SLF001


def _take(cfg: E2EConfig, target: Target) -> Sample:
    summary = json.loads(
        kubectl(cfg, ["get", "--raw", f"/api/v1/nodes/{target.node}/proxy/stats/summary"], capture=True)
    )
    usage = container_usage(summary, cfg.platform_namespace, target.pod["metadata"]["name"])
    return Sample(time.monotonic(), usage, _spool_bytes(cfg, target))


def _spool_bytes(cfg: E2EConfig, target: Target) -> int | None:
    """The stage's spool bytes on the measured pod's node, or None when the collector has no probe sidecar."""
    if target.api_pod is None:
        return None
    url = f"http://{target.pod['status']['podIP']}:{delivery.PROBE_PORT}{delivery.SPOOL_PATH}"
    body = kubectl(cfg, delivery.exec_get_args(cfg.platform_namespace, target.api_pod, url), capture=True)
    return sum(o.size_bytes for o in parse_spool_report(json.loads(body)) if o.stage == target.stage)


def _restarts_and_oom(cfg: E2EConfig) -> tuple[int, int]:
    pods = json.loads(
        kubectl(
            cfg,
            [
                "get",
                "pods",
                "-n",
                cfg.platform_namespace,
                "-l",
                f"app.kubernetes.io/name={collector.COLLECTOR}",
                "-o",
                "json",
            ],
            capture=True,
        )
    )["items"]
    statuses = [s for p in pods for s in p["status"].get("containerStatuses", [])]
    oom = sum(1 for s in statuses if s.get("lastState", {}).get("terminated", {}).get("reason") == "OOMKilled")
    return sum(s["restartCount"] for s in statuses), oom


def _start_emitter(cfg: E2EConfig, node: str, size: Size) -> tuple[str, str]:
    run_id = str(uuid.uuid4())
    token = f"{TOKEN_PREFIX}-{run_id[:8]}"
    name = f"olf-e2e-fp-{size.name}-{run_id[:8]}"
    manifest = collector.emitter_manifest(
        name,
        cfg.namespace,
        image=collector._emitter_image(cfg),  # noqa: SLF001
        run_id=run_id,
        token=token,
        lines=1,
        interval_seconds=0,
        ttl_seconds=3600,
        fail=False,
    )
    pod_spec = manifest["spec"]["template"]["spec"]
    pod_spec["containers"][0]["command"] = ["sh", "-c", emit_script(token, size.lines_per_second, PHASE_SECONDS)]
    # The collector pod measured is the one on this node; an emitter elsewhere would be read by a different pod.
    pod_spec["nodeName"] = node
    kubectl(cfg, ["apply", "-f", "-"], input_text=json.dumps(manifest))
    return name, token


def _emitter_running(cfg: E2EConfig, name: str) -> bool:
    """Whether the emitter's pod runs or ran; a Job whose pod is not created yet is still starting."""
    phases = kubectl(
        cfg,
        ["get", "pods", "-n", cfg.namespace, "-l", f"job-name={name}", "-o", "jsonpath={.items[*].status.phase}"],
        capture=True,
    ).split()
    if "Failed" in phases:
        raise E2EError(f"the emitter {name} failed before it ran")
    return any(phase in ("Running", "Succeeded") for phase in phases)


def _job_done(cfg: E2EConfig, name: str) -> bool:
    job = json.loads(kubectl(cfg, ["get", "job", name, "-n", cfg.namespace, "-o", "json"], capture=True))
    state = delivery.job_state(job)
    if state == "failed":
        raise E2EError(f"the emitter {name} failed: not every planned line was written")
    return state == "complete"


def _measure(cfg: E2EConfig, target: Target, size: Size | None) -> tuple[PhaseStats, str | None]:
    name = token = None
    if size is not None:
        name, token = _start_emitter(cfg, target.node, size)
        collector._wait_until(lambda: _emitter_running(cfg, str(name)), f"{name} to run", 180, poll_seconds=2.0)  # noqa: SLF001
    samples = [_take(cfg, target)]
    try:
        if size is None:
            deadline = time.monotonic() + IDLE_SECONDS
            while time.monotonic() < deadline:
                time.sleep(SAMPLE_SECONDS)
                samples.append(_take(cfg, target))
        else:
            deadline = time.monotonic() + 3 * PHASE_SECONDS
            while not _job_done(cfg, str(name)):
                if time.monotonic() >= deadline:
                    raise E2EError(f"the emitter {name} had not finished after {3 * PHASE_SECONDS}s")
                time.sleep(SAMPLE_SECONDS)
                samples.append(_take(cfg, target))
            time.sleep(TAIL_SECONDS)
            samples.append(_take(cfg, target))
    finally:
        if name:
            kubectl(cfg, ["delete", "job", name, "-n", cfg.namespace, "--ignore-not-found", "--wait=false"])
    return summarize_phase(size.name if size else "idle", size.lines_per_second if size else 0, samples), token


def _scan_archive(
    cfg: E2EConfig, since: datetime, tokens: Sequence[str], cache: dict[str, tuple[int, bytes]]
) -> ArchiveStats:
    """Read the stage's v2 objects written since `since`; an object is immutable once written, so it is fetched once."""
    prefix = f"activations/{delivery._stage(cfg)}/logs/v2/namespace={cfg.namespace}/"  # noqa: SLF001
    with collector._s3(cfg) as (client, bucket):  # noqa: SLF001
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                if item["LastModified"] >= since and item["Key"] not in cache:
                    cache[item["Key"]] = (
                        item["Size"],
                        client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read(),
                    )
    return archive_scan(list(cache.values()), tokens)


def _await_archive(cfg: E2EConfig, target: Target, since: datetime, expected: Mapping[str, int]) -> ArchiveStats:
    """Poll until every token is whole or the archive has stopped growing with the spool empty.

    Without a probe the spool is unknown, never empty: a partly filled buffer may wait `upload_timeout` before it is
    sent, so only the deadline ends the wait.
    """
    cache: dict[str, tuple[int, bytes]] = {}
    deadline = time.monotonic() + ARCHIVE_TIMEOUT_SECONDS
    last: tuple[int, ...] | None = None
    unchanged = 0
    while True:
        stats = _scan_archive(cfg, since, list(expected), cache)
        counts = tuple(len(stats.seen[t]) for t in expected)
        if all(len(stats.seen[t]) >= want for t, want in expected.items()):
            return stats
        unchanged = unchanged + 1 if counts == last and _spool_bytes(cfg, target) == 0 else 0
        last = counts
        if unchanged >= STABLE_POLLS or time.monotonic() >= deadline:
            return stats
        log.info(f"Footprint: archived {dict(zip(expected, counts, strict=True))} of {dict(expected)}; waiting.")
        time.sleep(ARCHIVE_POLL_SECONDS)


def _publish(cfg: E2EConfig, table: str, payload: Mapping[str, Any]) -> None:
    log.info("Log collector footprint (measured on this run):\n" + table)
    log.info("LOG_FOOTPRINT_JSON " + json.dumps(payload, sort_keys=True))
    if path := cfg_environ(cfg).get(SUMMARY_PATH_ENV):
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write("## Log collector footprint\n\n" + table + "\n\n")


def check_collection_footprint(cfg: E2EConfig) -> None:
    """Measure the collector's CPU, memory and spool at three log rates, and what reaches the archive."""
    log.step("Measuring the log collector's footprint at three log rates...")
    started = datetime.now(UTC)
    target = _target(cfg)
    stage = target.stage
    restarts_before, _ = _restarts_and_oom(cfg)
    limit = _memory_limit(target.pod)
    phases = []
    tokens: dict[str, str] = {}
    for size in (None, *SIZES):
        stats, token = _measure(cfg, target, size)
        phases.append(stats)
        if token:
            tokens[stats.name] = token
        log.info(
            f"Footprint {stats.name}: {stats.cpu_millicores['fluent-bit']:.0f}m CPU, "
            f"{stats.working_set_peak_bytes['fluent-bit'] / 1024**2:.0f} MiB."
        )
    emitted = {tokens[s.name]: s.lines_per_second * PHASE_SECONDS for s in SIZES}
    archive = _await_archive(cfg, target, started, emitted)
    delivered = {s.name: (len(archive.seen[tokens[s.name]]), emitted[tokens[s.name]]) for s in SIZES}
    table = render_table(phases, delivered, limit)
    payload = {
        "stage": stage,
        "phase_seconds": PHASE_SECONDS,
        "memory_limit_bytes": limit,
        "phases": [
            {
                "name": p.name,
                "lines_per_second": p.lines_per_second,
                "cpu_millicores": p.cpu_millicores,
                "cpu_peak_millicores": p.cpu_peak_millicores,
                "working_set_peak_bytes": p.working_set_peak_bytes,
                "rss_peak_bytes": p.rss_peak_bytes,
                "spool_peak_bytes": p.spool_peak_bytes,
            }
            for p in phases
        ],
        "delivered": {name: {"archived": got, "emitted": want} for name, (got, want) in delivered.items()},
        "archive": {
            "objects": archive.objects,
            "compressed_bytes": archive.compressed_bytes,
            "uncompressed_bytes": archive.uncompressed_bytes,
            "records": archive.records,
        },
    }
    _publish(cfg, table, payload)
    restarts_after, oom = _restarts_and_oom(cfg)
    if restarts_after != restarts_before or oom:
        raise E2EError(
            f"the collector restarted during the footprint run ({restarts_before} -> {restarts_after}, {oom} OOMKilled)"
        )
    peak = max(p.working_set_peak_bytes["fluent-bit"] for p in phases)
    if peak >= limit:
        raise E2EError(f"collector memory reached {peak} bytes, its {limit} byte limit")
    short = [name for name, (got, want) in delivered.items() if name in LOSSLESS and got < want]
    if short:
        raise E2EError(
            "lines were dropped at a size the collector must carry whole: "
            + ", ".join(f"{n} {delivered[n][0]}/{delivered[n][1]}" for n in short)
        )
