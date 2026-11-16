"""Failure injection for the log archive's delivery signals (ADR 0022, decision 2, #1189).

The Fluent Bit S3 output buffers into `store_dir` and returns success, so an unreachable object store leaves the
engine's error counters at zero. Two drills prove the signals the stage health route reads instead:

1. `check_delivery_signals_follow_an_outage`: with the collector's route to the object store cut, known lines are
   written; the spool's oldest-file age and the newest object's age pass their warning thresholds while the engine's
   S3 counters stay at zero; after the route is restored the spool drains, the signals recover and every line is in
   the archive exactly once.
2. `check_spool_limit_is_the_loss_point`: the same cut, then more log than `store_dir_limit_size` holds. It asserts
   the spool-size signal turns critical and the spool stays bounded, and records which lines the plugin discarded and
   what it logged, which the ADR lists as open [T] items.

The cut is the collector's own NetworkPolicy with its S3 egress rule removed, not an added policy: policies are
additive, so a second one could not take the allowed flow away. The signals are read the way the API reads them
(`openlakeforge_core.archive_health`): the probe sidecar is fetched from a platform API pod, which is F42's own path
(the API server's pod proxy would be refused by the collector's ingress policy), and the newest object comes from a
listing of the stage's archive prefix, so the thresholds under test are the production ones. Local only: it needs the
SeaweedFS port-forward and a collector with the probe sidecar, which a profile without the platform API lacks.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from openlakeforge_core import (
    HealthState,
    Signal,
    SpoolObservation,
    evaluate_newest_object,
    evaluate_spool_age,
    evaluate_spool_size,
    parse_spool_report,
    worst_state,
)

from olf.common import log
from olf.infra.e2e import _log_collector as collector
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl

POLICY = "olf-flows-log-collector"
S3_PORT = 8333
PROBE_PORT = 2021
SPOOL_PATH = "/spool"
_FETCH = "import sys,urllib.request;sys.stdout.write(urllib.request.urlopen(sys.argv[1],timeout=10).read().decode())"
ENGINE_PORT = 2020
SIGNAL_MAX_AGE = timedelta(seconds=120)
POLL_SECONDS = 60.0
# upload_timeout (5m) plus the spool age's 10m warning, with the object store's retry cycle on top.
OUTAGE_TIMEOUT_SECONDS = 40 * 60
DRAIN_TIMEOUT_SECONDS = 30 * 60
FILL_TIMEOUT_SECONDS = 30 * 60
# Objects land in the bucket before the spool file is removed, so an empty spool is already complete; this only
# guards a listing that lags the upload.
SETTLE_SECONDS = 60.0
SETTLE_TIMEOUT_SECONDS = 10 * 60
BASELINE_LINES = 20
OUTAGE_LINES = 60
# One awk call writes this many lines a second: fast enough to fill the spool in minutes, slow enough that the
# kubelet's 10 s rotation check does not rotate away what the collector has not read.
FILL_LINES_PER_SECOND = 5000
FILL_LINE_PADDING = 900
FILL_OVERSHOOT = 1.25
# Fluent Bit's server-side fields that a replacement of the policy must not carry.
_SERVER_FIELDS = ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "selfLink")
_ENGINE_FAILURE_COUNTERS = (
    "fluentbit_output_errors_total",
    "fluentbit_output_retries_failed_total",
    "fluentbit_output_dropped_records_total",
)
_PLUGIN_MESSAGE = re.compile(r"store_dir|limit|discard|drop|delet|quarantine|buffer|full", re.IGNORECASE)


def without_egress_port(policy: Mapping[str, Any], port: int) -> dict[str, Any]:
    """The policy with every egress rule that names `port` removed, and nothing else changed."""
    result = deepcopy(dict(policy))
    result["spec"]["egress"] = [
        rule
        for rule in result["spec"].get("egress", [])
        if not any(str(p.get("port")) == str(port) for p in rule.get("ports", []))
    ]
    return result


def replaceable(policy: Mapping[str, Any]) -> dict[str, Any]:
    """The policy minus the fields the API server owns, so it can be applied back as it was."""
    result = deepcopy(dict(policy))
    for name in _SERVER_FIELDS:
        result["metadata"].pop(name, None)
    result["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    result.pop("status", None)
    return result


def engine_failures(metrics: str) -> float:
    """The sum of the engine's S3 output failure counters in a Prometheus scrape; zero is what the ADR documents."""
    total = 0.0
    for line in metrics.splitlines():
        match = re.match(r"^(\w+)\{([^}]*)\}\s+([0-9.eE+-]+)$", line.strip())
        if match and match.group(1) in _ENGINE_FAILURE_COUNTERS and 'name="s3' in match.group(2):
            total += float(match.group(3))
    return total


def stage_signals(
    stage: str, observations: Iterable[SpoolObservation], newest_age: timedelta | None, *, now: datetime
) -> dict[str, Signal]:
    """The three delivery signals of one stage, the worst over every collector pod, as the health route reports them."""
    mine = [o for o in observations if o.stage == stage]
    spool_age = [evaluate_spool_age(stage, o, now=now, max_age=SIGNAL_MAX_AGE) for o in mine]
    spool_size = [evaluate_spool_size(stage, o, now=now, max_age=SIGNAL_MAX_AGE) for o in mine]
    newest = evaluate_newest_object(stage, newest_age, observed_at=now, now=now, max_age=SIGNAL_MAX_AGE)
    return {"spool-age": _worst(spool_age), "spool-size": _worst(spool_size), "newest-object": newest}


def _worst(signals: Sequence[Signal]) -> Signal:
    if not signals:
        raise E2EError("no collector pod reported the stage's spool")
    state = worst_state(s.state for s in signals)
    return next(s for s in signals if s.state is state)


def describe_loss(seen: set[int], expected: int) -> str:
    """Which of lines 1..`expected` are not in `seen`, as ranges."""
    missing = [n for n in range(1, expected + 1) if n not in seen]
    if not missing:
        return f"all {expected} lines archived"
    return f"{len(missing)} of {expected} lines missing: {collector._ranges(missing)}"  # noqa: SLF001 - one helper, one home


def fill_script(token: str, lines: int) -> str:
    awk = (
        'BEGIN{p=sprintf("%*s",' + str(FILL_LINE_PADDING) + ',""); gsub(/ /,"x",p); '
        'for(n=s;n<=e;n++) print t,"line",n,p}'
    )
    return (
        f'i=1; while [ "$i" -le {lines} ]; do end=$((i+{FILL_LINES_PER_SECOND}-1)); '
        f"awk -v s=$i -v e=$end -v t={token} '{awk}' >&2; i=$((end+1)); sleep 1; done"
    )


def _stage(cfg: E2EConfig) -> str:
    return cfg.namespace.removeprefix("olf-")


def collector_addresses(pods_json: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Name and IP of each collector pod. A pod without an IP is an error: the API could not reach it either."""
    result = []
    for item in pods_json["items"]:
        name, ip = item["metadata"]["name"], item.get("status", {}).get("podIP")
        if not ip:
            raise E2EError(f"collector pod {name} has no IP yet")
        result.append((name, ip))
    return result


def exec_get_args(namespace: str, pod: str, url: str, *, container: str | None = None) -> list[str]:
    """`kubectl exec` arguments that GET `url` from inside `pod` with the image's own python.

    Exec reaches the pod through the kubelet, so a NetworkPolicy never sees it: what the request itself crosses is
    decided by where it runs. From a platform API pod, a collector's address is F42's own path; from the collector's
    probe container, localhost is no network flow at all.
    """
    target = ["-c", container] if container else []
    return ["exec", pod, "-n", namespace, *target, "--", "python", "-c", _FETCH, url]


def _collector_pods(cfg: E2EConfig) -> list[tuple[str, str]]:
    out = kubectl(
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
    return collector_addresses(json.loads(out))


def _api_pod(cfg: E2EConfig) -> str:
    out = kubectl(
        cfg,
        ["get", "pods", "-n", cfg.platform_namespace, "-l", "app.kubernetes.io/name=platform-api", "-o", "name"],
        capture=True,
    )
    pods = out.split()
    if not pods:
        raise E2EError("no platform API pod to read the collector's probe from")
    return pods[0].removeprefix("pod/")


def _has_probe(cfg: E2EConfig) -> bool:
    out = kubectl(
        cfg,
        [
            "get",
            "daemonset",
            collector.COLLECTOR,
            "-n",
            cfg.platform_namespace,
            "-o",
            "jsonpath={.spec.template.spec.containers[*].name}",
        ],
        capture=True,
    )
    return "spool-probe" in out.split()


def _read_spool(cfg: E2EConfig) -> list[SpoolObservation]:
    """Each collector's probe, read from a platform API pod: the source and port F42 admits."""
    api = _api_pod(cfg)
    observations: list[SpoolObservation] = []
    for _, ip in _collector_pods(cfg):
        body = kubectl(
            cfg, exec_get_args(cfg.platform_namespace, api, f"http://{ip}:{PROBE_PORT}{SPOOL_PATH}"), capture=True
        )
        observations.extend(parse_spool_report(json.loads(body)))
    return observations


def _newest_age(cfg: E2EConfig, now: datetime) -> timedelta | None:
    stage = _stage(cfg)
    newest: datetime | None = None
    with collector._s3(cfg) as (client, bucket):  # noqa: SLF001
        for back in range(2):
            day = (now - timedelta(days=back)).date().isoformat()
            prefix = f"activations/{stage}/logs/v2/namespace={cfg.namespace}/date={day}/"
            token: str | None = None
            while True:
                args: dict[str, Any] = {
                    "Bucket": bucket,
                    "Prefix": prefix,
                    **({"ContinuationToken": token} if token else {}),
                }
                page = client.list_objects_v2(**args)
                for item in page.get("Contents", []):
                    newest = item["LastModified"] if newest is None else max(newest, item["LastModified"])
                token = page.get("NextContinuationToken")
                if not page.get("IsTruncated") or not token:
                    break
    return None if newest is None else max(timedelta(0), now - newest)


def _signals(cfg: E2EConfig) -> dict[str, Signal]:
    now = datetime.now(UTC)
    return stage_signals(_stage(cfg), _read_spool(cfg), _newest_age(cfg, now), now=now)


def _describe(signals: Mapping[str, Signal]) -> str:
    return "; ".join(f"{name}={signal.state.value} ({signal.reason})" for name, signal in signals.items())


def _wait_signals(
    cfg: E2EConfig, done: Callable[[Mapping[str, Signal]], bool], what: str, timeout_seconds: float
) -> dict[str, Signal]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        signals = _signals(cfg)
        log.info(f"{what}: {_describe(signals)}")
        if done(signals):
            return signals
        if time.monotonic() >= deadline:
            raise E2EError(f"timed out after {timeout_seconds:.0f}s waiting for {what}: {_describe(signals)}")
        time.sleep(POLL_SECONDS)


def _unhealthy(signal: Signal) -> bool:
    return signal.state in (HealthState.WARNING, HealthState.CRITICAL)


@contextlib.contextmanager
def _s3_unreachable(cfg: E2EConfig) -> Iterator[None]:
    """Replace the collector's NetworkPolicy by itself without the S3 rule; put the original back on exit."""
    original = replaceable(
        json.loads(
            kubectl(cfg, ["get", "networkpolicy", POLICY, "-n", cfg.platform_namespace, "-o", "json"], capture=True)
        )
    )
    if not any(
        str(p.get("port")) == str(S3_PORT) for rule in original["spec"].get("egress", []) for p in rule.get("ports", [])
    ):
        raise E2EError(f"{POLICY} has no egress rule for port {S3_PORT}: the cut would prove nothing")
    log.info(f"Cutting the collector's route to the object store (port {S3_PORT}) by replacing {POLICY}.")
    try:
        kubectl(cfg, ["replace", "-f", "-"], input_text=json.dumps(without_egress_port(original, S3_PORT)))
        yield
    finally:
        kubectl(cfg, ["replace", "-f", "-"], input_text=json.dumps(original))
        log.info("Restored the collector's route to the object store.")


def _engine_failures(cfg: E2EConfig) -> float:
    """The engine's counters, read over loopback from the probe container: nothing admits port 2020 but the kubelet."""
    url = f"http://127.0.0.1:{ENGINE_PORT}/api/v2/metrics/prometheus"
    return sum(
        engine_failures(
            kubectl(cfg, exec_get_args(cfg.platform_namespace, pod, url, container="spool-probe"), capture=True)
        )
        for pod, _ in _collector_pods(cfg)
    )


def spool_drained(observations: Iterable[SpoolObservation], stage: str) -> bool:
    """Whether every collector reports the stage's spool readable and empty; none reporting is not drained."""
    mine = [o for o in observations if o.stage == stage]
    return bool(mine) and all(not o.unreadable and o.oldest_file_age is None and o.size_bytes == 0 for o in mine)


def _wait_drained(cfg: E2EConfig, timeout_seconds: float) -> None:
    """Healthy signals are not enough: under half the limit and ten minutes old also reads healthy mid-drain."""
    stage = _stage(cfg)
    _wait_signals(
        cfg,
        lambda s: (
            all(signal.state is HealthState.HEALTHY for signal in s.values()) and spool_drained(_read_spool(cfg), stage)
        ),
        "the spool to drain and the signals to recover",
        timeout_seconds,
    )


def await_stable(read: Callable[[], set[int]], *, settle_seconds: float, timeout_seconds: float) -> set[int]:
    """Read until two reads `settle_seconds` apart agree, so the answer is not one taken mid-upload."""
    deadline = time.monotonic() + timeout_seconds
    previous: set[int] | None = None
    while True:
        current = read()
        if current == previous:
            return current
        previous = current
        if time.monotonic() >= deadline:
            raise E2EError(f"the archive was still changing after {timeout_seconds:.0f}s")
        time.sleep(settle_seconds)


def check_delivery_signals_follow_an_outage(cfg: E2EConfig) -> None:
    """Healthy, then warning while the object store is unreachable, then healthy again with every line archived."""
    if not _has_probe(cfg):
        log.info("The collector has no spool-probe sidecar (no platform API image): skipping the delivery signals.")
        return
    log.step("Checking the log archive's delivery signals through an object-store outage...")
    started = datetime.now(UTC)
    _, base_run, base_token = collector._start_emitter(  # noqa: SLF001
        cfg, lines=BASELINE_LINES, interval=0, ttl=3600, fail=False
    )
    with collector._archive(cfg) as archive:  # noqa: SLF001
        collector.await_exactly_once(
            lambda: collector._run_lines(archive, cfg, base_run, base_token, started),  # noqa: SLF001
            BASELINE_LINES,
            what=f"baseline run {base_run}",
        )
    baseline = _signals(cfg)
    if any(signal.state is not HealthState.HEALTHY for signal in baseline.values()):
        raise E2EError(f"the signals are not healthy before the outage: {_describe(baseline)}")

    with _s3_unreachable(cfg):
        blocked_at = datetime.now(UTC)
        _, run_id, token = collector._start_emitter(  # noqa: SLF001
            cfg, lines=OUTAGE_LINES, interval=0, ttl=3600, fail=False
        )
        signals = _wait_signals(
            cfg,
            lambda s: _unhealthy(s["spool-age"]) and _unhealthy(s["newest-object"]),
            "the spool age and the newest object to pass their warning thresholds",
            OUTAGE_TIMEOUT_SECONDS,
        )
        if (spooled := max(o.size_bytes for o in _read_spool(cfg) if o.stage == _stage(cfg))) <= 0:
            raise E2EError(f"the spool age rose but the spool holds no bytes: {_describe(signals)}")
        # The reason the signals exist: the engine reports success, so its counters say nothing is wrong.
        if failures := _engine_failures(cfg):
            raise E2EError(
                f"the engine's S3 failure counters rose to {failures} during the outage: ADR 0022 needs review"
            )
        log.info(f"During the outage {spooled} bytes are spooled and the engine's S3 failure counters stay at zero.")

    _wait_drained(cfg, DRAIN_TIMEOUT_SECONDS)
    log.info(f"Recovered: {_describe(_signals(cfg))}")
    with collector._archive(cfg) as archive:  # noqa: SLF001
        collector.await_exactly_once(
            lambda: collector._run_lines(archive, cfg, run_id, token, blocked_at),  # noqa: SLF001
            OUTAGE_LINES,
            what=f"run {run_id} written during the outage",
        )


def _archived_numbers(cfg: E2EConfig, token: str, since: datetime) -> set[int]:
    """Line numbers of `token` in every v2 object of the stage written since `since`, read straight from the bucket."""
    prefix = f"activations/{_stage(cfg)}/logs/v2/namespace={cfg.namespace}/"
    pattern = re.compile(rf"{re.escape(token)} line (\d+) ")
    seen: set[int] = set()
    with collector._s3(cfg) as (client, bucket):  # noqa: SLF001
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        for page in pages:
            for item in page.get("Contents", []):
                if item["LastModified"] < since:
                    continue
                body = gzip.decompress(client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read())
                for raw in body.splitlines():
                    if match := pattern.search(json.loads(raw).get("message", "")):
                        seen.add(int(match.group(1)))
    return seen


def job_state(job: Mapping[str, Any]) -> str:
    """`complete`, `failed` or `running`, from a Job's status; a failed pod counts, since backoffLimit is 0."""
    status = job.get("status", {})
    if status.get("succeeded", 0) >= 1:
        return "complete"
    if status.get("failed", 0) >= 1:
        return "failed"
    return "running"


def fill_timeout_seconds(lines: int) -> float:
    """Twice the time the emitter needs at its own rate, plus the pod's start-up: slack for a busy node."""
    return 2 * lines / FILL_LINES_PER_SECOND + 180


def _wait_job_complete(cfg: E2EConfig, name: str, timeout_seconds: float) -> None:
    """Wait for the emitter to finish, so the loss is measured against lines that were all written."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        job = json.loads(kubectl(cfg, ["get", "job", name, "-n", cfg.namespace, "-o", "json"], capture=True))
        state = job_state(job)
        if state == "complete":
            return
        if state == "failed":
            raise E2EError(f"the fill emitter {name} failed: not every planned line was written")
        if time.monotonic() >= deadline:
            raise E2EError(f"the fill emitter {name} had not finished after {timeout_seconds:.0f}s")
        time.sleep(POLL_SECONDS)


def check_spool_limit_is_the_loss_point(cfg: E2EConfig) -> None:
    """Fill the spool past `store_dir_limit_size` during an outage; assert it stays bounded, record what is lost."""
    if not _has_probe(cfg):
        log.info("The collector has no spool-probe sidecar (no platform API image): skipping the spool limit drill.")
        return
    log.step("Checking the log archive's spool limit is the loss point...")
    stage = _stage(cfg)
    limit = next((o.limit_bytes for o in _read_spool(cfg) if o.stage == stage), None)
    if limit is None:
        raise E2EError(f"no collector pod reports a spool for stage {stage}")
    lines = math.ceil(limit * FILL_OVERSHOOT / (FILL_LINE_PADDING + 40) / FILL_LINES_PER_SECOND) * FILL_LINES_PER_SECOND
    restarts_before = _restarts(cfg)
    with _s3_unreachable(cfg):
        started = datetime.now(UTC)
        name, token = _start_fill(cfg, lines)
        peak = 0
        try:

            def full(signals: Mapping[str, Signal]) -> bool:
                nonlocal peak
                peak = max([peak, *(o.size_bytes for o in _read_spool(cfg) if o.stage == stage)])
                return signals["spool-size"].state is HealthState.CRITICAL

            _wait_signals(cfg, full, "the spool to fill past its critical threshold", FILL_TIMEOUT_SECONDS)
            _wait_job_complete(cfg, name, fill_timeout_seconds(lines))
            peak = max([peak, *(o.size_bytes for o in _read_spool(cfg) if o.stage == stage)])
            # One 8M file is in flight when the plugin checks the limit.
            if peak > limit + 16 * 1000**2:
                raise E2EError(
                    f"the spool reached {peak} bytes, past its {limit} byte limit: the limit does not bound it"
                )
            log.info(f"Spool peak {peak} bytes against a {limit} byte limit; {lines} lines were written.")
            _record_plugin_messages(cfg, started)
        finally:
            kubectl(cfg, ["delete", "job", name, "-n", cfg.namespace, "--ignore-not-found", "--wait=false"])
    _wait_drained(cfg, DRAIN_TIMEOUT_SECONDS)
    collector._wait_until(lambda: collector._collector_ready(cfg), "the collector to be Ready", 120)  # noqa: SLF001
    if (restarts := _restarts(cfg)) != restarts_before:
        raise E2EError(f"the collector restarted while the spool filled ({restarts_before} -> {restarts})")
    seen = await_stable(
        lambda: _archived_numbers(cfg, token, started),
        settle_seconds=SETTLE_SECONDS,
        timeout_seconds=SETTLE_TIMEOUT_SECONDS,
    )
    log.info(f"After the limit was hit: {describe_loss(seen, lines)}")
    if not seen:
        raise E2EError("no line written during the fill reached the archive after the outage")


def _start_fill(cfg: E2EConfig, lines: int) -> tuple[str, str]:
    """Start the Job that writes `lines` numbered lines at a steady rate; returns its name and token."""
    run_id = str(uuid.uuid4())
    token = f"olf-e2e-{run_id[:8]}"
    name = f"olf-e2e-log-fill-{run_id[:8]}"
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
    manifest["spec"]["template"]["spec"]["containers"][0]["command"] = ["sh", "-c", fill_script(token, lines)]
    kubectl(cfg, ["apply", "-f", "-"], input_text=json.dumps(manifest))
    return name, token


def _restarts(cfg: E2EConfig) -> int:
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
    return sum(
        status["restartCount"]
        for pod in pods
        for status in pod["status"].get("containerStatuses", [])
        if status["name"] == "fluent-bit"
    )


def _record_plugin_messages(cfg: E2EConfig, since: datetime) -> None:
    """What the plugin logs at the limit: the ADR leaves this open, so it is recorded, not asserted."""
    for pod, _ in _collector_pods(cfg):
        text = kubectl(
            cfg,
            ["logs", pod, "-n", cfg.platform_namespace, "-c", "fluent-bit", f"--since-time={since.isoformat()}"],
            capture=True,
        )
        matched = [line for line in text.splitlines() if _PLUGIN_MESSAGE.search(line)]
        log.info(f"{pod}: {len(matched)} plugin messages about the spool since the fill began")
        for line in matched[:20]:
            log.info(f"  {line[:300]}")
