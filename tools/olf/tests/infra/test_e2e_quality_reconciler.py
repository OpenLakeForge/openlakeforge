"""The e2e's handling of the quality reconciler CronJob (#797): found, paused around the manifests, run once."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from olf.infra.e2e import _quality_publication as quality
from olf.infra.e2e._shell import E2EError

CRONJOB = {
    "metadata": {"name": "quality-reconciler-dev", "namespace": "olf-system"},
    "spec": {"suspend": False},
}


class Cluster:
    """A kubectl that answers the reads the helpers make and records every call."""

    def __init__(self, *, cronjob: dict[str, Any] | None = CRONJOB, job_status: dict[str, Any] | None = None) -> None:
        self.cronjob = cronjob
        self.job_status = job_status or {"succeeded": 1}
        self.calls: list[list[str]] = []
        self.active_polls = 0

    def __call__(self, cfg: Any, args: list[str], *, capture: bool = False, **_: Any) -> str:
        self.calls.append(args)
        if args[:2] == ["get", "cronjob"]:
            if "--ignore-not-found" in args:
                return json.dumps(self.cronjob) if self.cronjob else ""
            self.active_polls -= 1
            return json.dumps({"status": {"active": [{}] if self.active_polls > 0 else []}})
        if args[:2] == ["get", "job"]:
            return json.dumps({"status": self.job_status})
        if args[0] == "logs":
            return "boom"
        return ""

    def patches(self) -> list[bool]:
        return [json.loads(call[call.index("-p") + 1])["spec"]["suspend"] for call in self.calls if call[0] == "patch"]


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> Cluster:
    fake = Cluster()
    monkeypatch.setattr(quality, "kubectl", fake)
    monkeypatch.setattr(quality.time, "sleep", lambda _s: None)
    return fake


def _cfg(env: str = "local") -> Any:
    return SimpleNamespace(env=env, platform_namespace="olf-system")


def test_the_stages_cronjob_is_found_in_the_platform_namespace(cluster: Cluster) -> None:
    assert quality._reconciler_cronjob(_cfg(), "dev") == CRONJOB
    assert cluster.calls[0][:4] == ["get", "cronjob", "quality-reconciler-dev", "-n"]


def test_a_governed_local_stage_without_the_cronjob_fails_and_a_cloud_stack_skips(cluster: Cluster) -> None:
    cluster.cronjob = None

    with pytest.raises(E2EError, match="no quality-reconciler-dev CronJob"):
        quality._reconciler_cronjob(_cfg("local"), "dev")
    assert quality._reconciler_cronjob(_cfg("aws"), "dev") is None


def test_the_cronjob_is_suspended_for_the_block_and_its_own_setting_comes_back(cluster: Cluster) -> None:
    cluster.active_polls = 3

    with quality._reconciler_paused(_cfg(), CRONJOB):
        assert cluster.patches() == [True]

    assert cluster.patches() == [True, False]


def test_a_cronjob_that_was_already_suspended_stays_suspended(cluster: Cluster) -> None:
    suspended = {**CRONJOB, "spec": {"suspend": True}}

    with quality._reconciler_paused(_cfg(), suspended):
        pass

    assert cluster.patches() == [True, True]


def test_the_setting_comes_back_when_the_block_fails(cluster: Cluster) -> None:
    with pytest.raises(RuntimeError), quality._reconciler_paused(_cfg(), CRONJOB):
        raise RuntimeError("check failed")

    assert cluster.patches() == [True, False]


def test_without_a_cronjob_nothing_is_touched(cluster: Cluster) -> None:
    with quality._reconciler_paused(_cfg(), None):
        pass

    assert cluster.calls == []


def test_a_job_made_from_the_cronjob_is_waited_for_and_removed(cluster: Cluster) -> None:
    quality._run_reconciler_job(_cfg(), CRONJOB)

    created = next(call for call in cluster.calls if call[0] == "create")
    assert created[1] == "job" and "--from=cronjob/quality-reconciler-dev" in created
    assert cluster.calls[-1][:2] == ["delete", "job"]


def test_a_failed_job_reports_its_log_and_is_still_removed(cluster: Cluster) -> None:
    cluster.job_status = {"failed": 1}

    with pytest.raises(E2EError, match="boom"):
        quality._run_reconciler_job(_cfg(), CRONJOB)

    assert cluster.calls[-1][:2] == ["delete", "job"]


def test_a_backlog_is_drained_with_further_passes_until_the_result_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passes: list[int] = []
    answers = iter([[], [], [{"testCaseStatus": "Success"}]])
    monkeypatch.setattr(quality, "_run_reconciler_job", lambda cfg, cronjob: passes.append(1))
    monkeypatch.setattr(quality, "_results", lambda client, fqn: next(answers))

    quality._drain_with_reconciler_jobs(_cfg(), CRONJOB, object(), 't."olf.floe.e2e_cron_rule"')

    assert len(passes) == 3


def test_a_result_still_missing_after_the_pass_budget_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    passes: list[int] = []
    monkeypatch.setattr(quality, "_run_reconciler_job", lambda cfg, cronjob: passes.append(1))
    monkeypatch.setattr(quality, "_results", lambda client, fqn: [])

    with pytest.raises(E2EError, match="passes of the reconciler's Job did not publish"):
        quality._drain_with_reconciler_jobs(_cfg(), CRONJOB, object(), "fqn")

    assert len(passes) == quality.MAX_RECONCILER_PASSES
