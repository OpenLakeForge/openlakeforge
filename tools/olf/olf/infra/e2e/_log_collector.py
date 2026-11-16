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
import json
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta

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
        stage = cfg.namespace.removeprefix("olf-")
        yield LogArchive(client, f"s3://{bucket}/activations/{stage}/logs")


def _run_lines(archive: LogArchive, cfg: E2EConfig, run_id: str, token: str, since: datetime) -> list[int]:
    """Line numbers the reader returns for `run_id`, with no pod name, across every UTC day since `since`."""
    messages: list[str] = []
    day: date = since.date()
    while day <= datetime.now(UTC).date():
        for line in archive.read(cfg.namespace, day, run_id=run_id, limit=10_000):
            if line.line is not None:
                messages.append(line.line)
        day += timedelta(days=1)
    return line_numbers(messages, token)


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
        _wait_until(lambda: not _pods_of(cfg, run_id), f"the TTL to delete the pod of {name}", 300)
        with _archive(cfg) as archive:
            await_exactly_once(
                lambda: _run_lines(archive, cfg, run_id, token, started),
                SHORT_LIVED_LINES,
                what=f"run {run_id} (pod already deleted)",
            )
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
            out = kubectl(cfg, ["logs", f"job/{name}", "-n", cfg.namespace, "--tail=1"], capture=True)
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
            await_exactly_once(
                lambda: _run_lines(archive, cfg, run_id, token, started),
                STREAM_LINES,
                what=f"run {run_id} across a collector restart",
            )
    finally:
        kubectl(cfg, ["delete", "job", name, "-n", cfg.namespace, "--ignore-not-found", "--wait=false"])
