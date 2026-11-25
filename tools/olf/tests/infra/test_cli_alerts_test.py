"""`olf infra alerts test` runs the evaluator once as a Job built from its CronJob and reports the receiver's answer (#1431).

Only the cluster is faked: a runner standing in for `kubectl` that holds the CronJob, keeps the Job it is applied and
answers its status and logs. The real `Kubectl` adapter builds the argv.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openlakeforge_core.stages import StageName
from typer.testing import CliRunner

from olf.cli import app
from olf.common.profile import AlertReceiver, SecretRef
from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import CommandResult, ProcessRunner
from olf.infra.commands import alerts as alerts_cmd

runner = CliRunner()
CRONJOB = {
    "metadata": {"name": "alert-evaluator", "namespace": "olf-system"},
    "spec": {
        "jobTemplate": {
            "metadata": {"labels": {"openlakeforge.io/job": "alert-evaluator"}},
            "spec": {
                "ttlSecondsAfterFinished": 60,
                "template": {"spec": {"containers": [{"name": "evaluator", "command": ["python", "-m", "x"]}]}},
            },
        }
    },
}
DELIVERED = '{"receiver": "on-call", "status": "delivered", "http_status": 200, "attempts": 1}'


class FakeCluster(ProcessRunner):
    def __init__(self) -> None:
        super().__init__()
        self.cronjob: dict[str, Any] | None = CRONJOB
        self.job: dict[str, Any] | None = None
        self.job_status: dict[str, Any] = {"succeeded": 1}
        self.logs = DELIVERED
        self.deleted: list[str] = []
        self.calls: list[list[str]] = []

    def run(self, command: Any, **kwargs: Any) -> CommandResult:  # type: ignore[override]
        argv = [str(part) for part in command]
        self.calls.append(argv)
        verb = next(part for part in argv[1:] if part in ("get", "apply", "logs", "delete"))
        stdout = stderr = ""
        code = 0
        if verb == "apply":
            self.job = json.loads(kwargs["input_text"])
        elif verb == "delete":
            self.deleted.append(argv[argv.index("delete") + 2])
        elif verb == "logs":
            stdout = self.logs
        elif argv[argv.index("get") + 1] == "cronjob":
            if self.cronjob is None:
                code, stderr = 1, 'Error from server (NotFound): cronjobs.batch "alert-evaluator" not found'
            else:
                stdout = json.dumps(self.cronjob)
        else:
            stdout = json.dumps({"status": self.job_status})
        return CommandResult(argv=tuple(argv), returncode=code, stdout=stdout, stderr=stderr, duration_seconds=0.0)


class FakeResolver:
    def resolve(self, _name: str) -> Path:
        return Path("kubectl")


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    fake = FakeCluster()
    receivers = (
        AlertReceiver("on-call", "webhook", SecretRef("alert-webhooks", "on-call"), stages=(StageName.PROD,)),
        AlertReceiver("team", "github-discussions", repository="o/r", category="Alerts"),
    )
    provider = SimpleNamespace(
        tools=SimpleNamespace(kubectl=Kubectl(fake, FakeResolver())), env={"KUBE_CONTEXT": "kind-x"}
    )
    context = SimpleNamespace(
        shared_namespace="olf-system",
        kube_context="kind-y",
        paths=SimpleNamespace(kubeconfig_path=None),
        enabled_stages=(StageName.DEV, StageName.PROD),
        topology=SimpleNamespace(observability=SimpleNamespace(alert_receivers=receivers)),
    )
    monkeypatch.setattr("olf.infra.commands._shared.deployment_context_for_profile", lambda *_a, **_k: context)
    monkeypatch.setattr("olf.infra.commands.project._profile_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(alerts_cmd, "_POLL_SECONDS", 0)
    return fake


def _test(receiver: str, *args: str) -> Any:
    return runner.invoke(app, ["infra", "alerts", "test", receiver, "-f", "openlakeforge.yaml", *args])


def _args(cluster: FakeCluster) -> list[str]:
    assert cluster.job is not None
    return cluster.job["spec"]["template"]["spec"]["containers"][0]["args"]


def test_a_delivered_test_reports_the_answer_and_deletes_the_job(cluster: FakeCluster) -> None:
    cluster.logs = "2026 INFO x: webhook on-call: attempt 1\n2026 INFO other line\n" + DELIVERED

    result = _test("on-call")

    assert result.exit_code == 0, result.output
    assert "receiver on-call: delivered (HTTP 200, 1 attempt(s))" in result.output
    assert "webhook on-call: attempt 1" in result.output and "other line" not in result.output
    assert _args(cluster) == ["--test-receiver", "on-call", "--test-stage", "prod"]
    assert cluster.job is not None and cluster.job["spec"]["backoffLimit"] == 0
    assert cluster.deleted == [cluster.job["metadata"]["name"]]


def test_the_default_stage_is_the_first_enabled_one_when_the_receiver_names_none(cluster: FakeCluster) -> None:
    result = _test("team")

    assert result.exit_code == 0, result.output
    assert _args(cluster)[-1] == "dev"


def test_a_stage_the_receiver_does_not_hear_fails_before_anything_runs(cluster: FakeCluster) -> None:
    result = _test("on-call", "--stage", "dev")

    assert result.exit_code == 1
    assert "does not hear about stage 'dev'" in result.output and "prod" in result.output
    assert cluster.calls == [] and cluster.job is None


def test_an_unknown_receiver_lists_the_declared_ones(cluster: FakeCluster) -> None:
    result = _test("nope")

    assert result.exit_code == 1
    assert "on-call, team" in result.output
    assert cluster.job is None


def test_without_the_cronjob_it_says_no_receivers_are_deployed(cluster: FakeCluster) -> None:
    cluster.cronjob = None

    result = _test("on-call")

    assert result.exit_code == 1
    assert "no alert receivers are deployed" in result.output and "olf infra deploy" in result.output
    assert cluster.job is None


def test_a_rejection_exits_non_zero_with_the_receivers_log_lines(cluster: FakeCluster) -> None:
    cluster.job_status = {"failed": 1}
    cluster.logs = (
        "github-discussions team: rejected: the GitHub App is not installed on o/r\n"
        '{"receiver": "team", "status": "rejected", "http_status": 404, "attempts": 1}'
    )

    result = _test("team")

    assert result.exit_code == 3
    assert "the GitHub App is not installed on o/r" in result.output
    assert "receiver team: rejected (HTTP 404, 1 attempt(s))" in result.output
    assert cluster.deleted


def test_a_job_that_never_finishes_times_out_and_is_still_deleted(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster.job_status = {}
    monkeypatch.setattr(alerts_cmd, "_TEST_TIMEOUT_SECONDS", 0)

    result = _test("on-call")

    assert result.exit_code == 1
    assert "did not finish" in result.output
    assert cluster.deleted


def test_a_job_that_fails_before_sending_shows_why(cluster: FakeCluster) -> None:
    cluster.job_status = {"failed": 1}
    cluster.logs = "ERROR alert test cannot run: unknown receiver 'on-call'; the evaluator has: ops"

    result = _test("on-call")

    assert result.exit_code == 1
    assert "unknown receiver 'on-call'" in result.output
