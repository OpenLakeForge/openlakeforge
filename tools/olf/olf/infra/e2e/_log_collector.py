"""Log collector assertions (ADR 0022, #1123): what the node collector keeps that the polling CronJob could not.

Both checks read the archive the way the platform API does, through `LogArchive`, with the run id as the only key.
The emitters are Jobs in the stage namespace labelled `dagster/run-id`, the label the collector's Lua filter turns into
`run_id`. They run the k8s-bootstrap image, which is already prefetched into kind.

The S3 output buffers for `upload_timeout` (5m, `fluent-bit.conf.tftpl`) before it writes an object, so every wait
below is sized in minutes. Local only: the collector is a Slim and Full kind component and the reader needs the
SeaweedFS port-forward.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import boto3
import yaml
from botocore.config import Config
from openlakeforge_services.logs import LogArchive

from olf.common import log
from olf.infra import k8s
from olf.infra.e2e._artifacts import wait_for_bucket
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

COLLECTOR = "log-collector"
RUN_ID_LABEL = "dagster/run-id"

# upload_timeout (5m) plus a retry cycle and the kubelet's and the collector's own latency.
ARCHIVE_TIMEOUT_SECONDS = 15 * 60
ARCHIVE_POLL_SECONDS = 20.0
# A replay uploaded by a later flush would show as a duplicate; one more look after the lines are complete.
SETTLE_SECONDS = 90.0
# The collector reads the file every 5s and looks the pod up once; a TTL shorter than that races the lookup, which
# is a different failure (ADR 0022, "Pod gone before metadata lookup") from the one this check proves.
SHORT_LIVED_TTL_SECONDS = 30
SHORT_LIVED_LINES = 60
STREAM_LINES = 90
STREAM_INTERVAL_SECONDS = 1
KILL_AFTER_LINE = 30
# The reader is given the drill's start less this as its window start, so it lists only the hour partitions from then
# on, as a run's read does. The line times are the node's clock and the start is this process's; on kind they share
# the host's clock, and the margin keeps a line written as the drill starts in the window.
WINDOW_MARGIN = timedelta(minutes=2)
# A v2 key's partition: `.../date=<YYYY-MM-DD>/hour=<HH>/<object>`.
_PARTITION = re.compile(r"/date=(\d{4}-\d{2}-\d{2})/hour=(\d{2})/[^/]+$")


def emitter_manifest(
    name: str,
    namespace: str,
    *,
    image: str,
    run_id: str,
    token: str,
    lines: int,
    interval_seconds: int,
    ttl_seconds: int,
    fail: bool,
) -> dict:
    """A Job printing `<token> line <n> ERROR ...` to stderr, `lines` times, then exiting 1 when `fail`."""
    pause = f" sleep {interval_seconds};" if interval_seconds else ""
    script = (
        f'i=1; while [ "$i" -le {lines} ]; do echo "{token} line $i ERROR contract violated" >&2;{pause} '
        f"i=$((i+1)); done; exit {1 if fail else 0}"
    )
    labels = {RUN_ID_LABEL: run_id, "app.kubernetes.io/name": "olf-e2e-log-emitter"}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": ttl_seconds,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [{"name": "emit", "image": image, "command": ["sh", "-c", script]}],
                },
            },
        },
    }


def line_numbers(messages: list[str], token: str) -> list[int]:
    pattern = re.compile(rf"{re.escape(token)} line (\d+) ")
    return [int(match.group(1)) for message in messages if (match := pattern.search(message))]


def exactly_once_problems(seen: list[int], expected: int) -> list[str]:
    """Why `seen` is not each of 1..`expected` exactly once; empty when it is."""
    counts = Counter(seen)
    problems = []
    if missing := [n for n in range(1, expected + 1) if n not in counts]:
        problems.append(f"missing {len(missing)} of {expected}: {_ranges(missing)}")
    if repeated := sorted(n for n, times in counts.items() if times > 1):
        problems.append(f"duplicated {len(repeated)}: {_ranges(repeated)}")
    if extra := sorted(n for n in counts if not 1 <= n <= expected):
        problems.append(f"unexpected line numbers: {extra[:10]}")
    return problems


def _ranges(numbers: list[int]) -> str:
    spans: list[list[int]] = []
    for n in numbers:
        if spans and n == spans[-1][1] + 1:
            spans[-1][1] = n
        else:
            spans.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def await_exactly_once(
    read: Callable[[], list[int]],
    expected: int,
    *,
    what: str,
    timeout_seconds: float = ARCHIVE_TIMEOUT_SECONDS,
    poll_seconds: float = ARCHIVE_POLL_SECONDS,
    settle_seconds: float = SETTLE_SECONDS,
) -> None:
    """Poll until the archive holds lines 1..`expected` once each, then look again after `settle_seconds`.

    A line absent at the deadline is a drop. A duplicate fails at once and also after the settle read, because
    the replay of a killed collector can be uploaded by a later flush than the original.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        seen = read()
        problems = exactly_once_problems(seen, expected)
        if not problems:
            break
        if any(problem.startswith(("duplicated", "unexpected")) for problem in problems):
            raise E2EError(f"{what}: {'; '.join(problems)}")
        if time.monotonic() >= deadline:
            raise E2EError(f"{what}: not archived after {timeout_seconds:.0f}s: {'; '.join(problems)}")
        log.info(f"{what}: {len(set(seen))}/{expected} lines archived; waiting.")
        time.sleep(poll_seconds)
    time.sleep(settle_seconds)
    if problems := exactly_once_problems(read(), expected):
        raise E2EError(f"{what}: changed after the lines were complete: {'; '.join(problems)}")


@contextlib.contextmanager
def _archive(cfg: E2EConfig) -> Iterator[LogArchive]:
    with _s3(cfg) as (client, bucket):
        stage = cfg.namespace.removeprefix("olf-")
        yield LogArchive(client, f"s3://{bucket}/activations/{stage}/logs")


@contextlib.contextmanager
def _s3(cfg: E2EConfig) -> Iterator[tuple[Any, str]]:
    """A client for the ops bucket through the SeaweedFS port-forward, and the bucket's name."""
    if cfg.seaweedfs_local_port is None:
        raise E2EError("the log collector check needs the SeaweedFS local port")
    bucket = load_provider_contracts_or_raise(cfg)["shared"]["ops_storage"]["bucket_name"]
    credentials = {
        key: k8s.secret_value(
            "seaweedfs-s3-creds", key, cfg.platform_namespace, kube_context=cfg.kube_context, environ=cfg.environ
        )
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    }
    with k8s.port_forward(
        "seaweedfs-s3",
        8333,
        cfg.platform_namespace,
        local_port=cfg.seaweedfs_local_port,
        log_path=f"/tmp/openlakeforge-{cfg.env}-seaweedfs-s3-log-collector-port-forward.log",
        kube_context=cfg.kube_context,
        environ=cfg.environ,
    ):
        endpoint = f"http://127.0.0.1:{cfg.seaweedfs_local_port}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=credentials["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=credentials["AWS_SECRET_ACCESS_KEY"],
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),
        )
        wait_for_bucket(client, bucket, endpoint)
        yield client, bucket


def _run_lines(archive: LogArchive, cfg: E2EConfig, run_id: str, token: str, since: datetime) -> list[int]:
    """Line numbers the reader returns for `run_id`, with no pod name, from `since` on across UTC days.

    The window start makes the reader list only the hour partitions it touches, so a line archived under the wrong
    hour is missing here.
    """
    messages: list[str] = []
    start = since - WINDOW_MARGIN
    day: date = start.date()
    while day <= datetime.now(UTC).date():
        for line in archive.read(cfg.namespace, day, run_id=run_id, since=start, limit=10_000):
            if line.line is not None:
                messages.append(line.line)
        day += timedelta(days=1)
    return line_numbers(messages, token)


def partition_problems(key: str, records: list[dict[str, Any]]) -> list[str]:
    """Why the v2 object `key` is not in the partition of every record's own UTC date and hour; empty when it is."""
    match = _PARTITION.search(key)
    if match is None:
        return [f"{key} is not under date=<day>/hour=<HH>/"]
    partition = f"{match.group(1)}T{match.group(2)}"
    stray = sorted({str(record.get("time")) for record in records if str(record.get("time"))[:13] != partition})
    return [f"{key} holds records of another hour: {stray[:3]}"] if stray else []


def check_hour_partitions(cfg: E2EConfig, token: str, since: datetime) -> None:
    """Every object holding `token` sits under the date and hour of each of its records (ADR 0022, decision 4)."""
    prefix = f"activations/{cfg.namespace.removeprefix('olf-')}/logs/v2/namespace={cfg.namespace}/"
    problems: list[str] = []
    found = 0
    with _s3(cfg) as (client, bucket):
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                if item["LastModified"] < since:
                    continue
                body = gzip.decompress(client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read())
                records = [json.loads(raw) for raw in body.splitlines() if raw.strip()]
                if any(token in str(record.get("message", "")) for record in records):
                    found += 1
                    problems += partition_problems(item["Key"], records)
    if not found:
        raise E2EError(f"no v2 object under {prefix} holds {token}")
    if problems:
        raise E2EError(f"the collector's keys do not name their records' hour: {'; '.join(problems[:5])}")
    log.info(f"{found} object(s) holding {token} sit under the date and hour of their records.")


def diagnose_missing_lines(cfg: E2EConfig, token: str, since: datetime, run_id: str) -> None:
    """Say where lines of `token` went when the reader cannot find them by `run_id`: in the bucket, with what fields.

    The reader selects on `run_id`, which the collector takes from the pod's labels through the Kubernetes API; a
    line archived without it is in the bucket and invisible to the reader, which a missing line is not.
    """
    prefix = f"activations/{cfg.namespace.removeprefix('olf-')}/logs/v2/namespace={cfg.namespace}/"
    matched: list[dict[str, Any]] = []
    with _s3(cfg) as (client, bucket):
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                if item["LastModified"] < since:
                    continue
                body = gzip.decompress(client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read())
                matched.extend(json.loads(raw) for raw in body.splitlines() if token.encode() in raw)
    with_run = [r for r in matched if r.get("run_id") == run_id]
    log.info(f"Diagnosis for {token}: {len(matched)} archived records carry it, {len(with_run)} with run_id {run_id}.")
    for record in matched[:2]:
        log.info(f"  sample record: {json.dumps({k: v for k, v in record.items() if k != 'message'})}")
    selector = f"app.kubernetes.io/name={COLLECTOR}"
    text = kubectl(
        cfg,
        ["logs", "-n", cfg.platform_namespace, "-l", selector, "-c", "fluent-bit", "--tail=-1"],
        capture=True,
    )
    lines = [line for line in text.splitlines() if re.search(r"kube|filter|metadata|api", line, re.IGNORECASE)]
    log.info(f"  collector lines about the Kubernetes filter ({len(lines)}); the last 15:")
    for line in lines[-15:]:
        log.info(f"    {line[:300]}")


def _emitter_image(cfg: E2EConfig) -> str:
    catalog = yaml.safe_load((cfg.distribution_root / "release/component-catalog.yaml").read_text())
    return str(catalog["components"]["images"]["k8s_bootstrap"])


def _start_emitter(cfg: E2EConfig, *, lines: int, interval: int, ttl: int, fail: bool) -> tuple[str, str, str]:
    run_id = str(uuid.uuid4())
    token = f"olf-e2e-{run_id[:8]}"
    name = f"olf-e2e-log-{run_id[:8]}"
    manifest = emitter_manifest(
        name,
        cfg.namespace,
        image=_emitter_image(cfg),
        run_id=run_id,
        token=token,
        lines=lines,
        interval_seconds=interval,
        ttl_seconds=ttl,
        fail=fail,
    )
    kubectl(cfg, ["apply", "-f", "-"], input_text=json.dumps(manifest))
    return name, run_id, token


def _pods_of(cfg: E2EConfig, run_id: str) -> list[str]:
    out = kubectl(
        cfg, ["get", "pods", "-n", cfg.namespace, "-l", f"{RUN_ID_LABEL}={run_id}", "-o", "name"], capture=True
    )
    return out.split()


def _wait_until(done: Callable[[], bool], what: str, timeout_seconds: float, poll_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not done():
        if time.monotonic() >= deadline:
            raise E2EError(f"timed out after {timeout_seconds:.0f}s waiting for {what}")
        time.sleep(poll_seconds)


def _collector_ready(cfg: E2EConfig) -> bool:
    """Every node has a Ready collector pod; the killed pods are gone from the API, so none is counted twice."""
    daemonset = json.loads(
        kubectl(cfg, ["get", "daemonset", COLLECTOR, "-n", cfg.platform_namespace, "-o", "json"], capture=True)
    )
    pods = json.loads(
        kubectl(
            cfg,
            ["get", "pods", "-n", cfg.platform_namespace, "-l", f"app.kubernetes.io/name={COLLECTOR}", "-o", "json"],
            capture=True,
        )
    )["items"]
    ready = [
        pod
        for pod in pods
        if not pod["metadata"].get("deletionTimestamp")
        and any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
    ]
    return bool(ready) and len(ready) == daemonset["status"]["desiredNumberScheduled"]


def check_short_lived_pod_output(cfg: E2EConfig) -> None:
    """A failing pod deleted by its Job TTL leaves all its output in the archive, found by run id alone (#1121)."""
    log.step("Checking a failed pod deleted by TTL keeps its full output in the log archive...")
    started = datetime.now(UTC)
    name, run_id, token = _start_emitter(
        cfg, lines=SHORT_LIVED_LINES, interval=0, ttl=SHORT_LIVED_TTL_SECONDS, fail=True
    )
    try:
        # See the pod first: before the Job controller creates it, "no pod" would pass the deletion wait at once
        # and the drill would no longer prove the output outlives the pod.
        _wait_until(lambda: bool(_pods_of(cfg, run_id)), f"the pod of {name} to be created", 120, poll_seconds=1.0)
        _wait_until(lambda: not _pods_of(cfg, run_id), f"the TTL to delete the pod of {name}", 300)
        with _archive(cfg) as archive:
            try:
                await_exactly_once(
                    lambda: _run_lines(archive, cfg, run_id, token, started),
                    SHORT_LIVED_LINES,
                    what=f"run {run_id} (pod already deleted)",
                )
            except E2EError:
                diagnose_missing_lines(cfg, token, started, run_id)
                raise
        check_hour_partitions(cfg, token, started)
    finally:
        kubectl(cfg, ["delete", "job", name, "-n", cfg.namespace, "--ignore-not-found", "--wait=false"])


def check_collector_restart(cfg: E2EConfig) -> None:
    """Killing the collector while a pod is logging loses no line and repeats none in the reader's output."""
    log.step("Checking a collector killed mid-stream drops and duplicates no line...")
    started = datetime.now(UTC)
    name, run_id, token = _start_emitter(
        cfg, lines=STREAM_LINES, interval=STREAM_INTERVAL_SECONDS, ttl=3600, fail=False
    )
    selector = f"app.kubernetes.io/name={COLLECTOR}"
    try:

        def streamed() -> bool:
            try:
                out = kubectl(cfg, ["logs", f"job/{name}", "-n", cfg.namespace, "--tail=1"], capture=True)
            except E2EError:
                # `kubectl logs` gives up after 20 s while the pod is still being scheduled or pulled: on a busy
                # cluster that is a slow start, not a collector failure, so keep polling within the deadline.
                return False
            return bool(line_numbers([out], token)) and line_numbers([out], token)[-1] >= KILL_AFTER_LINE

        _wait_until(streamed, f"{name} to reach line {KILL_AFTER_LINE}", 180, poll_seconds=2.0)
        # No grace period: the collector gets no chance to flush, so recovery comes from the offsets and chunks on disk.
        kubectl(
            cfg,
            [
                "delete",
                "pod",
                "-n",
                cfg.platform_namespace,
                "-l",
                selector,
                "--grace-period=0",
                "--force",
                "--wait=false",
            ],
        )
        _wait_until(lambda: _collector_ready(cfg), "the collector DaemonSet to be Ready again", 180)
        kubectl(
            cfg,
            ["wait", "--for=condition=complete", f"job/{name}", "-n", cfg.namespace, "--timeout=300s"],
        )
        with _archive(cfg) as archive:
            try:
                await_exactly_once(
                    lambda: _run_lines(archive, cfg, run_id, token, started),
                    STREAM_LINES,
                    what=f"run {run_id} across a collector restart",
                )
            except E2EError:
                diagnose_missing_lines(cfg, token, started, run_id)
                raise
    finally:
        kubectl(cfg, ["delete", "job", name, "-n", cfg.namespace, "--ignore-not-found", "--wait=false"])
