"""Suspending CronJobs around a restore (#911): what each CronJob holds afterwards, against a stateful kubectl."""

from __future__ import annotations

import json
from typing import Any

import pytest

from olf.infra import k8s


class FakeCronJobs:
    """`kubectl get cronjob` and `patch cronjob` over CronJobs; a running Job ends after `polls` listings."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.running: dict[str, int] = {}  # CronJob -> listings until its Job ends
        monkeypatch.setattr(k8s, "_kubectl", self.kubectl)
        monkeypatch.setattr(k8s.time, "sleep", lambda _seconds: None)

    def add(self, name: str, *, suspend: bool, running: int = 0) -> None:
        self.items[name] = {"metadata": {"name": name}, "spec": {"suspend": suspend}}
        self.running[name] = running

    def kubectl(self, args: list[str], **_: Any) -> str:
        if args[0] == "get":
            for name, item in self.items.items():
                active = self.running[name] > 0
                item["status"] = {"active": [{"name": f"{name}-1"}]} if active else {}
                self.running[name] = max(0, self.running[name] - 1)
            return json.dumps({"items": list(self.items.values())})
        assert args[:2] == ["patch", "cronjob"]
        merge(self.items[args[2]], json.loads(args[args.index("-p") + 1]))
        return ""

    def state(self) -> dict[str, tuple[bool, dict[str, str]]]:
        return {
            name: (item["spec"]["suspend"], item["metadata"].get("annotations") or {})
            for name, item in self.items.items()
        }


def merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            merge(target.setdefault(key, {}), value)
        else:
            target[key] = value


def test_suspend_waits_for_running_jobs_and_resume_restores_what_each_cronjob_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = FakeCronJobs(monkeypatch)
    cluster.add("log-archive", suspend=False, running=3)
    cluster.add("kept-suspended", suspend=True)

    k8s.suspend_cronjobs("olf-dev")

    assert all(suspended for suspended, _ in cluster.state().values())
    assert cluster.running == {"log-archive": 0, "kept-suspended": 0}  # it waited for the Job to end

    k8s.resume_cronjobs("olf-dev")

    assert cluster.state() == {"log-archive": (False, {}), "kept-suspended": (True, {})}


def test_a_rerun_after_a_failure_still_restores_the_original_suspension(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = FakeCronJobs(monkeypatch)
    cluster.add("log-archive", suspend=False)

    k8s.suspend_cronjobs("olf-dev")
    k8s.suspend_cronjobs("olf-dev")  # the restore failed and ran again; the CronJob is already suspended
    k8s.resume_cronjobs("olf-dev")

    assert cluster.state() == {"log-archive": (False, {})}


def test_a_cronjob_created_during_the_restore_is_not_touched_by_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = FakeCronJobs(monkeypatch)
    cluster.add("late", suspend=True)

    k8s.resume_cronjobs("olf-dev")

    assert cluster.state() == {"late": (True, {})}
