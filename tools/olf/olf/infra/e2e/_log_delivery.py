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
(`openlakeforge_core.archive_health`), from the probe sidecar through the API server's pod proxy and from a listing
of the stage's archive prefix, so the thresholds under test are the production ones. Local only: it needs the
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
ENGINE_PORT = 2020
SIGNAL_MAX_AGE = timedelta(seconds=120)
POLL_SECONDS = 60.0
# upload_timeout (5m) plus the spool age's 10m warning, with the object store's retry cycle on top.
OUTAGE_TIMEOUT_SECONDS = 40 * 60
DRAIN_TIMEOUT_SECONDS = 30 * 60
FILL_TIMEOUT_SECONDS = 30 * 60
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


def _pods(cfg: E2EConfig) -> list[str]:
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
    return [item["metadata"]["name"] for item in json.loads(out)["items"]]


def _proxy(cfg: E2EConfig, pod: str, port: int, path: str) -> str:
    return kubectl(
        cfg,
        ["get", "--raw", f"/api/v1/namespaces/{cfg.platform_namespace}/pods/{pod}:{port}/proxy{path}"],
        capture=True,
    )


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
    observations: list[SpoolObservation] = []
    for pod in _pods(cfg):
        observations.extend(parse_spool_report(json.loads(_proxy(cfg, pod, PROBE_PORT, "/spool"))))
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
    return sum(engine_failures(_proxy(cfg, pod, ENGINE_PORT, "/api/v2/metrics/prometheus")) for pod in _pods(cfg))


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

    drained = _wait_signals(
        cfg,
        lambda s: all(signal.state is HealthState.HEALTHY for signal in s.values()),
        "the spool to drain and the signals to recover",
        DRAIN_TIMEOUT_SECONDS,
    )
    log.info(f"Recovered: {_describe(drained)}")
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
            time.sleep(120)
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
    _wait_signals(
        cfg,
        lambda s: all(signal.state is HealthState.HEALTHY for signal in s.values()),
        "the spool to drain and the signals to recover",
        DRAIN_TIMEOUT_SECONDS,
    )
    collector._wait_until(lambda: collector._collector_ready(cfg), "the collector to be Ready", 120)  # noqa: SLF001
    if (restarts := _restarts(cfg)) != restarts_before:
        raise E2EError(f"the collector restarted while the spool filled ({restarts_before} -> {restarts})")
    seen = _archived_numbers(cfg, token, started)
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
    for pod in _pods(cfg):
        text = kubectl(
            cfg,
            ["logs", pod, "-n", cfg.platform_namespace, "-c", "fluent-bit", f"--since-time={since.isoformat()}"],
            capture=True,
        )
        matched = [line for line in text.splitlines() if _PLUGIN_MESSAGE.search(line)]
        log.info(f"{pod}: {len(matched)} plugin messages about the spool since the fill began")
        for line in matched[:20]:
            log.info(f"  {line[:300]}")
