"""Alert evaluator e2e (#1173, ADR 0023): a stub webhook receiver, the evaluator run as Jobs, and what reaches the stub.

Every incident is made on the `gold_maintenance` job, which every stage has and whose runs are cheap: a retention
below Trino's minimum fails the run at once, a dry run succeeds, and PROD carries its weekly schedule (stopped by
default). The evaluator runs as a Job made from the CronJob with `--state-prefix alerts/e2e/`, so the real state under
`alerts/` is untouched, and `--now` moves its clock past a schedule's due time instead of waiting for one. The prefix
stays inside the `alerts/` grant of the evaluator's S3 key, which `--state-prefix` would otherwise leave. The Job also
trusts the stub's CA through its own settings file, whatever the route TLS mode is.

Alerts are silent unless a policy declares them (ADR 0023, decision 2), and these incidents are on a job of no
product, so they depend on the sample project's global rules in `lakehouse_code/alerts.yml` and on the conformance
profile's `on-call` receiver, which that file routes to. Change either and these cases go quiet.

The real CronJob is suspended meanwhile: it would send the same incidents to the same receiver from its own state.
Local only, and only when the stub is the evaluator's sole receiver, so a test incident never reaches a real webhook;
the receiver is a pod in the shared namespace that the evaluator reaches over flow F41 (ADR 0017).
"""

from __future__ import annotations

import contextlib
import copy
import json
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.config import Config
from openlakeforge_core.quality import (
    PipelineOutcome,
    QualityManifest,
    QualityOutcome,
    QualityResult,
    manifest_key,
)
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.notify import MAX_ATTEMPTS

from olf.common import log
from olf.infra import k8s
from olf.infra.access import contract_access
from olf.infra.e2e._artifacts import wait_for_bucket
from olf.infra.e2e._dagster import dagster_client, launch_and_poll
from olf.infra.e2e._secret_exposure import find_exposures, secret_values
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

STUB_NAME = "olf-e2e-alert-receiver"
# What the evaluator's NetworkPolicy names to let it reach the stub on 8443 (F41).
LABEL = "openlakeforge.io/e2e=alert-receiver"
LOCAL_CA_ISSUER = "local-ca"  # modules/access/cert-manager
# The Secret the profile's e2e receiver reads (openlakeforge.conformance.yaml); the nightly creates it before deploy.
RECEIVER_REF_NAME, RECEIVER_KEY = "olf-e2e-alert-receiver", "webhook-url"
EVALUATOR = "alert-evaluator"  # the CronJob's name (modules/observability/alerts)
# Inside the evaluator key's `alerts/*` grant; the real evaluator writes only `state/` and `history/` there.
STATE_PREFIX = "alerts/e2e/"
HARNESS_FIELD = "_idempotency_key"  # added by `payloads` from the recorded header; the only non-payload key
RECEIVER_ENV_PREFIX = "OPENLAKEFORGE_ALERT_RECEIVER_"  # modules/observability/alerts: one variable per receiver
E2E_MOUNT = "/e2e"  # where the Job's own settings and CA are mounted
SETTINGS_CONFIGMAP = f"{STUB_NAME}-settings"
JOB, JOB_OP = "gold_maintenance", "maintain_gold"
TAG = "olf-e2e/alert-tag"
# The evaluator's expected-run grace is 15 minutes (`SCHEDULE_GRACE`, platform API); one more minute clears it.
PAST_GRACE = timedelta(minutes=16)
JOB_TIMEOUT_SECONDS = 300
RUN_TIMEOUT_SECONDS = 600
# ADR 0023, decision 4: the closed list of payload fields and of links.
PAYLOAD_FIELDS = frozenset(
    (
        "schema event incident_id condition severity stage domain product table run_id opened_at event_at summary links"
    ).split()
)
LINK_FIELDS = frozenset({"run", "runbook"})
DOMAIN_TAG = "openlakeforge/silver-domain"
TERMINAL = {"SUCCESS", "FAILURE", "CANCELED"}


def stub_host(cfg: E2EConfig) -> str:
    return f"{STUB_NAME}.{cfg.platform_namespace}.svc"


def stub_network_policy(namespace: str) -> dict[str, Any]:
    """Ingress to the stub on 8443 from the evaluator's pods only.

    The stub's `/_records` and `/_status` routes have no authentication, and `olf-system` has no default deny, so
    without this any pod there could read or steer them. The harness reaches those routes by `kubectl exec` into the
    stub (loopback, which a NetworkPolicy does not see), so it needs no rule.
    """
    label_key, label_value = LABEL.split("=")
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": STUB_NAME, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {label_key: label_value}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"openlakeforge.io/job": EVALUATOR}}}],
                    "ports": [{"protocol": "TCP", "port": 8443}],
                }
            ],
        },
    }


def sole_receiver_is_the_stub(cronjob: Mapping[str, Any]) -> bool:
    """Whether the stub's Secret is the one receiver the evaluator was deployed with.

    The evaluator reads each receiver's URL from a `secretKeyRef` env var, so these are the receivers it would send to.
    """
    spec = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    env = [e for c in spec["containers"] for e in c.get("env", []) if e["name"].startswith(RECEIVER_ENV_PREFIX)]
    refs = [e.get("valueFrom", {}).get("secretKeyRef", {}) for e in env]
    return [(r.get("name"), r.get("key")) for r in refs] == [(RECEIVER_REF_NAME, RECEIVER_KEY)]


def e2e_settings(settings_json: str) -> str:
    """The evaluator's settings with the CA file pointed at the stub's CA; the CronJob's own settings are untouched."""
    settings = json.loads(settings_json)
    settings["ca_file"] = f"{E2E_MOUNT}/ca.crt"
    return json.dumps(settings)


def evaluation_job(
    cronjob: Mapping[str, Any],
    name: str,
    *,
    now: datetime | None,
    state_prefix: str,
    settings_configmap: str | None = None,
) -> dict[str, Any]:
    """A Job running the CronJob's pod template with the e2e-only flags appended to the evaluator's command.

    With `settings_configmap` (keys `alerts.json` and `ca.crt`) the Job reads those settings, which trust the stub's CA.
    """
    template = copy.deepcopy(cronjob["spec"]["jobTemplate"])
    pod = template["spec"]["template"]["spec"]
    container = pod["containers"][0]
    flags = ["--state-prefix", state_prefix] + (["--now", now.astimezone(UTC).isoformat()] if now else [])
    if settings_configmap:
        flags += ["--settings", f"{E2E_MOUNT}/alerts.json"]
        pod["volumes"] = [*pod.get("volumes", []), {"name": "e2e", "configMap": {"name": settings_configmap}}]
        container["volumeMounts"] = [
            *container.get("volumeMounts", []),
            {"name": "e2e", "mountPath": E2E_MOUNT, "readOnly": True},
        ]
    container["args"] = [*container.get("args", []), *flags]
    template["spec"].pop("ttlSecondsAfterFinished", None)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": cronjob["metadata"]["namespace"],
            "labels": template.get("metadata", {}).get("labels", {}),
        },
        "spec": template["spec"],
    }


def job_outcome(job: Mapping[str, Any]) -> str | None:
    status = job.get("status", {})
    if status.get("succeeded"):
        return "succeeded"
    if status.get("failed"):
        return "failed"
    return None


def payloads(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The recorded bodies, each with the `Idempotency-Key` it arrived under."""
    found = []
    for record in records:
        body = json.loads(record["body"])
        if HARNESS_FIELD in body:
            raise E2EError(f"the payload itself carries the harness field {HARNESS_FIELD!r}")
        body[HARNESS_FIELD] = record["headers"].get("idempotency-key", "")
        found.append(body)
    return found


def select(found: list[dict[str, Any]], **want: str) -> list[dict[str, Any]]:
    return [item for item in found if all(item.get(key) == value for key, value in want.items())]


def exactly_one(found: list[dict[str, Any]], what: str, **want: str) -> dict[str, Any]:
    matches = select(found, **want)
    if len(matches) != 1:
        raise E2EError(f"{what}: expected one notification matching {want}, got {len(matches)} of {len(found)}")
    return matches[0]


def expected_run_link(routes: Mapping[str, str], stage: str, run_id: str) -> str | None:
    """The run link ADR 0023 (decision 5) promises: the stage's Dagster route, which is behind the perimeter."""
    base = routes.get(f"stage/{stage}/orchestration")
    return f"{base.rstrip('/')}/runs/{run_id}" if base else None


def payload_problems(payload: Mapping[str, Any], *, link: str | None, secrets: Mapping[str, str]) -> list[str]:
    """Why a payload breaks the contract: a field off the allowlist, a wrong or missing link, or a Secret value."""
    body = {key: value for key, value in payload.items() if key != HARNESS_FIELD}
    problems = [f"unlisted field {key!r}" for key in sorted(set(body) - PAYLOAD_FIELDS)]
    links = body.get("links", {})
    problems += [f"unlisted link {key!r}" for key in sorted(set(links) - LINK_FIELDS)]
    if body.get("run_id") and links.get("run") != link:
        problems.append(f"run link is {links.get('run')!r}, expected {link!r}")
    problems += find_exposures(secrets, {"the payload": json.dumps(body)})
    return problems


def quality_manifest(
    base: QualityManifest, outcome: QualityOutcome, severity: str, pipeline: PipelineOutcome
) -> QualityManifest:
    """`base`'s run, complete, with one Floe check that ended as `outcome`."""
    result = QualityResult(
        engine="floe",
        stage=base.stage,
        domain=base.domain,
        product=base.product,
        check_id="e2e-alert-check",
        engine_severity=severity,
        outcome=outcome,
        pipeline_outcome=pipeline,
        observed_at=datetime.now(UTC),
        dagster_run_id=base.dagster_run_id,
    )
    return QualityManifest(base.stage, base.domain, base.product, base.dagster_run_id, (result,), complete=True)


@dataclass(frozen=True)
class ScheduleRef:
    location: str
    repository: str
    name: str
    # The next two due times; the first is the one a missed run is measured against.
    due: tuple[datetime, datetime]

    def selector(self) -> dict[str, str]:
        return {"repositoryLocationName": self.location, "repositoryName": self.repository, "scheduleName": self.name}


SCHEDULES = """
query Schedules {
  repositoriesOrError {
    __typename
    ... on RepositoryConnection {
      nodes {
        name
        location { name }
        schedules { name pipelineName futureTicks(limit: 2) { results { timestamp } } }
      }
    }
    ... on PythonError { message }
  }
}
"""


def find_schedule(data: Mapping[str, Any], job: str) -> ScheduleRef | None:
    result = data["repositoriesOrError"]
    if result.get("__typename") != "RepositoryConnection":
        raise E2EError(f"Dagster could not list its schedules: {result.get('message', result)}")
    for repository in result["nodes"]:
        for schedule in repository["schedules"]:
            if schedule["pipelineName"] != job:
                continue
            ticks = [datetime.fromtimestamp(tick["timestamp"], UTC) for tick in schedule["futureTicks"]["results"]]
            if len(ticks) < 2:
                raise E2EError(f"Dagster reports fewer than two future ticks for {schedule['name']}")
            return ScheduleRef(
                repository["location"]["name"], repository["name"], schedule["name"], (ticks[0], ticks[1])
            )
    return None


@dataclass
class Harness:
    cfg: E2EConfig
    client: DagsterClient
    s3: Any
    bucket: str
    stage: str
    pod: str
    cronjob: dict[str, Any]
    routes: dict[str, str]
    secrets: dict[str, str]
    jobs: int = 0

    def stub(self, method: str, path: str) -> str:
        script = f"curl -sk --max-time 10 -X {method} https://127.0.0.1:8443{path}"
        return kubectl(
            self.cfg, ["exec", "-n", self.cfg.platform_namespace, self.pod, "--", "sh", "-c", script], capture=True
        )

    def received(self) -> list[dict[str, Any]]:
        return payloads(json.loads(self.stub("GET", "/_records") or "[]"))

    def reset(self) -> None:
        self.stub("DELETE", "/_records")

    def evaluate(self, now: datetime | None = None) -> None:
        """One evaluator run to completion; the notifications it sent are what `received` returns."""
        self.jobs += 1
        name = f"olf-e2e-alert-eval-{self.jobs}-{uuid.uuid4().hex[:6]}"
        manifest = evaluation_job(
            self.cronjob, name, now=now, state_prefix=STATE_PREFIX, settings_configmap=SETTINGS_CONFIGMAP
        )
        namespace = self.cfg.platform_namespace
        kubectl(self.cfg, ["apply", "-f", "-"], input_text=json.dumps(manifest))
        try:
            deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
            while (
                outcome := job_outcome(
                    json.loads(kubectl(self.cfg, ["get", "job", name, "-n", namespace, "-o", "json"], capture=True))
                )
            ) is None:
                if time.monotonic() >= deadline:
                    raise E2EError(f"evaluator job {name} did not finish in {JOB_TIMEOUT_SECONDS}s")
                time.sleep(3)
            if outcome == "failed":
                tail = kubectl(self.cfg, ["logs", f"job/{name}", "-n", namespace, "--tail=30"], capture=True)
                raise E2EError(f"evaluator job {name} failed:\n{tail}")
        finally:
            kubectl(self.cfg, ["delete", "job", name, "-n", namespace, "--ignore-not-found", "--wait=false"])

    def verify(self, found: list[dict[str, Any]]) -> None:
        for item in found:
            link = expected_run_link(self.routes, self.stage, item.get("run_id", ""))
            problems = payload_problems(item, link=link, secrets=self.secrets)
            if problems:
                raise E2EError(f"alert payload {item.get('incident_id')}: " + "; ".join(problems))

    def keys(self, prefix: str) -> list[str]:
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        return [item["Key"] for page in pages for item in page.get("Contents", [])]

    def delivery_records(self, incident_id: str) -> list[dict[str, Any]]:
        keys = [key for key in self.keys(f"{STATE_PREFIX}history/") if incident_id in key and "-delivery-" in key]
        return [json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()) for key in keys]

    def clear_state(self) -> None:
        for key in self.keys(STATE_PREFIX):
            self.s3.delete_object(Bucket=self.bucket, Key=key)

    def launch(self, config: Mapping[str, Any], token: str) -> str:
        return self.client.launch(JOB, tags={TAG: token}, run_config={"ops": {JOB_OP: {"config": dict(config)}}})

    def fail_a_run(self, token: str) -> str:
        run_id = self.launch({"older_than_days": 1}, token)
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        while (run := self.client.get_run(run_id)) is None or run.status not in TERMINAL:
            if time.monotonic() >= deadline:
                raise E2EError(f"run {run_id} did not finish in {RUN_TIMEOUT_SECONDS}s")
            time.sleep(3)
        if run.status != "FAILURE":
            raise E2EError(f"run {run_id} was expected to fail, ended {run.status}")
        return run_id

    def succeed_a_run(self, token: str) -> str:
        return launch_and_poll(
            self.cfg, self.client, JOB, tags={TAG: token}, run_config={"ops": {JOB_OP: {"config": {"dry_run": True}}}}
        )

    def set_schedule(self, ref: ScheduleRef, on: bool) -> None:
        mutation = "startSchedule" if on else "resetSchedule"
        result = self.client.graphql(
            f"mutation S($s: ScheduleSelector!) {{ {mutation}(scheduleSelector: $s) {{ __typename "
            "... on PythonError { message } ... on UnauthorizedError { message } "
            "... on ScheduleNotFoundError { message } } }",
            {"s": ref.selector()},
        )[mutation]
        if result["__typename"] != "ScheduleStateResult":
            raise E2EError(f"{mutation} {ref.name}: {result}")


def _settle(h: Harness, what: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Evaluate once and return only what that run sent."""
    h.reset()
    h.evaluate(now)
    found = h.received()
    h.verify(found)
    log.info(f"{what}: {len(found)} notification(s).")
    return found


def _quiet(h: Harness, what: str, now: datetime | None = None) -> None:
    if found := _settle(h, what, now):
        raise E2EError(f"{what}: expected nothing, got {[(n['condition'], n['event']) for n in found]}")


def run_failed_and_resolved(h: Harness, token: str) -> None:
    run_id = h.fail_a_run(token)
    opened = exactly_one(_settle(h, "failed run"), "failed run", condition="run_failed", event="opened", product=JOB)
    if (opened["severity"], opened["run_id"], opened["stage"]) != ("critical", run_id, h.stage):
        raise E2EError(f"run_failed carries the wrong context: {opened}")
    _quiet(h, "second evaluation of the same failure")
    h.succeed_a_run(token)
    resolved = exactly_one(
        _settle(h, "succeeded run"), "recovery", condition="run_failed", event="resolved", product=JOB
    )
    if resolved["incident_id"] != opened["incident_id"]:
        raise E2EError("the resolve names another incident than the one that opened")
    _quiet(h, "second evaluation after recovery")


def bounded_retries(h: Harness, token: str) -> None:
    h.fail_a_run(token)
    h.stub("PUT", "/_status/503")
    try:
        sent = select(_settle(h, "receiver answering 503"), condition="run_failed", product=JOB)
    finally:
        h.stub("PUT", "/_status/200")
    if not 1 < len(sent) <= MAX_ATTEMPTS or len({item["_idempotency_key"] for item in sent}) != 1:
        raise E2EError(f"expected 2 to {MAX_ATTEMPTS} attempts under one Idempotency-Key, got {len(sent)}")
    incident = sent[0]["incident_id"]
    deliveries = h.delivery_records(incident)
    if [(d["outcome"], d["attempts"]) for d in deliveries] != [("retryable", len(sent))]:
        raise E2EError(f"history should hold one retryable delivery of {len(sent)} attempts, got {deliveries}")
    again = exactly_one(_settle(h, "receiver back"), "redelivery", condition="run_failed", product=JOB)
    if again["_idempotency_key"] != sent[0]["_idempotency_key"]:
        raise E2EError("the redelivery used another Idempotency-Key")
    h.succeed_a_run(token)
    exactly_one(_settle(h, "recovery"), "recovery", condition="run_failed", event="resolved", product=JOB)


def quality_warn_then_error(h: Harness) -> None:
    """A Floe WARN and then a blocking ERROR in the latest run's manifest, then a pass.

    The manifest is replaced in place and restored after: forcing a real Floe warning would mean editing the sample
    contracts, and the evaluator reads only the manifest.
    """
    found = next(
        (
            (job.name, run)
            for job in h.client.list_jobs()
            if job.name.endswith("_pipeline")
            for run in h.client.list_runs(job_name=job.name, limit=1)
            if run.status == "SUCCESS" and DOMAIN_TAG in run.tags
        ),
        None,
    )
    if found is None:
        raise E2EError("no product pipeline has a successful run to attach a quality outcome to")
    job, run = found
    domain, product = run.tags[DOMAIN_TAG], job.removesuffix("_pipeline")
    key = f"activations/{h.stage}/{manifest_key(domain, product, run.run_id)}"
    try:
        original = h.s3.get_object(Bucket=h.bucket, Key=key)["Body"].read()
    except h.s3.exceptions.NoSuchKey:
        original = None
    base = QualityManifest(h.stage, domain, product, run.run_id)

    def publish(outcome: QualityOutcome, severity: str, pipeline: PipelineOutcome) -> None:
        body = quality_manifest(base, outcome, severity, pipeline).to_json().encode()
        h.s3.put_object(Bucket=h.bucket, Key=key, Body=body, ContentType="application/json")

    try:
        publish(QualityOutcome.WARN, "warn", PipelineOutcome.CONTINUED)
        warn = exactly_one(_settle(h, "Floe warn"), "Floe warn", condition="quality", event="opened", product=job)
        if warn["severity"] != "warning":
            raise E2EError(f"a Floe warn should open a warning, got {warn['severity']}")
        publish(QualityOutcome.ERROR, "error", PipelineOutcome.BLOCKED)
        error = exactly_one(_settle(h, "Floe error"), "Floe error", condition="quality", event="escalated", product=job)
        if (error["severity"], error["incident_id"]) != ("critical", warn["incident_id"]):
            raise E2EError("a blocking error should escalate the same incident to critical")
        _quiet(h, "second evaluation of the quality error")
        publish(QualityOutcome.PASSED, "error", PipelineOutcome.CONTINUED)
        exactly_one(_settle(h, "Floe pass"), "Floe pass", condition="quality", event="resolved", product=job)
    finally:
        if original is None:
            h.s3.delete_object(Bucket=h.bucket, Key=key)
        else:
            h.s3.put_object(Bucket=h.bucket, Key=key, Body=original, ContentType="application/json")


def schedule_cases(h: Harness) -> None:
    """A missed run, silent when the schedule is stopped, and a stale output; the clock is moved, not the runs."""
    ref = find_schedule(h.client.graphql(SCHEDULES), JOB)
    if ref is None:
        log.info(f"Skipping the schedule cases: {JOB} has no schedule in stage {h.stage} (only PROD does).")
        return
    first, second = ref.due
    missed = first + PAST_GRACE
    try:
        _quiet(h, "a stopped schedule past its due time", missed)
        h.set_schedule(ref, True)
        opened = exactly_one(_settle(h, "missed run", missed), "missed run", condition="missed_run", product=JOB)
        if opened["event"] != "opened" or opened["severity"] != "critical":
            raise E2EError(f"a missed run should open a critical incident, got {opened}")
        _quiet(h, "second evaluation of the missed run", missed)
        h.set_schedule(ref, False)
        _quiet(h, "the schedule stopped while missed_run was open", missed)
        if not any("resolved_silently" in key for key in h.keys(f"{STATE_PREFIX}history/")):
            raise E2EError("history should record the missed run as resolved_silently")
        h.set_schedule(ref, True)
        stale = _settle(h, "stale output", second + timedelta(minutes=1))
        exactly_one(stale, "stale output", condition="stale_output", event="opened", product=JOB)
        if select(stale, condition="missed_run"):
            raise E2EError("a run inside its grace period must not read as missed")
    finally:
        h.set_schedule(ref, False)


@contextlib.contextmanager
def alert_receiver(cfg: E2EConfig) -> Iterator[str]:
    """The stub pod and its Service, serving a certificate from the local CA; yields the pod name."""
    namespace = cfg.platform_namespace
    image = yaml.safe_load((cfg.distribution_root / "release/component-catalog.yaml").read_text())["components"][
        "images"
    ]["k8s_bootstrap"]
    certificate = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": STUB_NAME, "namespace": namespace},
        "spec": {
            "secretName": f"{STUB_NAME}-tls",
            "dnsNames": [stub_host(cfg)],
            "issuerRef": {"kind": "ClusterIssuer", "name": LOCAL_CA_ISSUER},
        },
    }
    spec = {
        "containers": [
            {
                "name": STUB_NAME,
                "image": image,
                "command": ["python3", "-c", (Path(__file__).parent / "alert_receiver_stub.py").read_text()],
                "volumeMounts": [{"name": "tls", "mountPath": "/tls"}],
                "readinessProbe": {"tcpSocket": {"port": 8443}, "periodSeconds": 1},
            }
        ],
        "volumes": [{"name": "tls", "secret": {"secretName": f"{STUB_NAME}-tls"}}],
    }

    def remove() -> None:
        kubectl(
            cfg,
            [
                "delete",
                f"pod/{STUB_NAME}",
                f"networkpolicy/{STUB_NAME}",
                f"service/{STUB_NAME}",
                f"certificate/{STUB_NAME}",
            ]
            + [f"secret/{STUB_NAME}-tls", f"configmap/{SETTINGS_CONFIGMAP}", "-n", namespace, "--ignore-not-found"],
        )

    remove()
    try:
        kubectl(cfg, ["apply", "-f", "-"], input_text=json.dumps(stub_network_policy(namespace)))
        kubectl(cfg, ["apply", "-f", "-"], input_text=json.dumps(certificate))
        kubectl(cfg, ["wait", "--for=condition=Ready", f"certificate/{STUB_NAME}", "-n", namespace, "--timeout=120s"])
        run = ["run", STUB_NAME, "-n", namespace, "--restart=Never", f"--image={image}", f"--labels={LABEL}"]
        kubectl(cfg, [*run, f"--overrides={json.dumps({'spec': spec})}"])
        kubectl(cfg, ["expose", "pod", STUB_NAME, "-n", namespace, "--port=443", "--target-port=8443"])
        kubectl(cfg, ["wait", "--for=condition=Ready", f"pod/{STUB_NAME}", "-n", namespace, "--timeout=180s"])
        # The evaluator trusts the public roots plus this CA (its `ca_file`), but only the route-TLS deployment mounts
        # the local CA. The CA that signed the stub's certificate is in that certificate's Secret in every mode.
        ca = k8s.secret_value(
            f"{STUB_NAME}-tls", r"ca\.crt", namespace, kube_context=cfg.kube_context, environ=cfg.environ
        )
        live = kubectl(cfg, ["get", "configmap", EVALUATOR, "-n", namespace, "-o", "json"], capture=True)
        settings = {"alerts.json": e2e_settings(json.loads(live)["data"]["alerts.json"]), "ca.crt": ca}
        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": SETTINGS_CONFIGMAP, "namespace": namespace},
            "data": settings,
        }
        kubectl(cfg, ["apply", "-f", "-"], input_text=json.dumps(configmap))
        yield STUB_NAME
    finally:
        remove()


@contextlib.contextmanager
def evaluator_paused(cfg: E2EConfig, cronjob: Mapping[str, Any]) -> Iterator[None]:
    """The CronJob suspended, with no run of it still going, for the block; its own setting is put back."""
    namespace = cfg.platform_namespace

    def suspend(value: bool) -> None:
        patch = json.dumps({"spec": {"suspend": value}})
        kubectl(cfg, ["patch", "cronjob", EVALUATOR, "-n", namespace, "-p", patch])

    was = bool(cronjob["spec"].get("suspend"))
    suspend(True)
    try:
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while True:
            live = json.loads(kubectl(cfg, ["get", "cronjob", EVALUATOR, "-n", namespace, "-o", "json"], capture=True))
            if not live.get("status", {}).get("active"):
                break
            if time.monotonic() >= deadline:
                raise E2EError("a scheduled evaluator run is still active")
            time.sleep(3)
        yield
    finally:
        suspend(was)


@contextlib.contextmanager
def _ops_bucket(cfg: E2EConfig) -> Iterator[tuple[Any, str]]:
    if cfg.seaweedfs_local_port is None:
        raise E2EError("the alert check needs the SeaweedFS local port")
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
        log_path=f"/tmp/openlakeforge-{cfg.env}-seaweedfs-s3-alerts-port-forward.log",
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


def check_alert_delivery(cfg: E2EConfig) -> None:
    """Failed, recovered, unreachable, quality and schedule incidents each reach the receiver exactly once."""
    if cfg.env != "local":
        return
    namespace = cfg.platform_namespace
    listing = kubectl(
        cfg, ["get", "cronjob", EVALUATOR, "-n", namespace, "--ignore-not-found", "-o", "json"], capture=True
    )
    if not listing.strip():
        log.info("Skipping the alert checks: the profile declares no receiver, so there is no evaluator.")
        return
    cronjob = json.loads(listing)
    if not sole_receiver_is_the_stub(cronjob):
        log.info(
            "Skipping the alert checks: the evaluator's only receiver is not the conformance profile's "
            "e2e receiver, and the checks must not send test incidents to a real one."
        )
        return
    log.step("Checking alert notifications reach a receiver once per incident...")
    stage = cfg.namespace.removeprefix("olf-")
    token = uuid.uuid4().hex
    url = k8s.secret_value(
        RECEIVER_REF_NAME, RECEIVER_KEY, namespace, kube_context=cfg.kube_context, environ=cfg.environ
    )
    routes = contract_access(load_provider_contracts_or_raise(cfg), stage=stage).get("routes", {})
    secrets = {**secret_values(cfg), "the receiver URL": url, "a run tag": token}
    with (
        evaluator_paused(cfg, cronjob),
        alert_receiver(cfg) as pod,
        _ops_bucket(cfg) as (s3, bucket),
        dagster_client(cfg) as client,
    ):
        h = Harness(cfg, client, s3, bucket, stage, pod, cronjob, routes, secrets)
        h.clear_state()
        try:
            # Whatever is already firing in the stage is told once here, so the cases below see only their own.
            h.evaluate()
            run_failed_and_resolved(h, token)
            bounded_retries(h, token)
            quality_warn_then_error(h)
            schedule_cases(h)
        finally:
            h.clear_state()
