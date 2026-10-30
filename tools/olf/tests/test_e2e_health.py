import json
from pathlib import Path

import pytest
from conftest import e2e_cfg

from olf.e2e import _health
from olf.e2e._shell import E2EError


def test_unhealthy_pod_messages_accepts_ready_running_and_succeeded() -> None:
    payload = {
        "items": [
            {
                "metadata": {"name": "service"},
                "status": {"phase": "Running", "containerStatuses": [{"name": "app", "ready": True}]},
            },
            {"metadata": {"name": "bootstrap"}, "status": {"phase": "Succeeded"}},
        ]
    }

    assert _health.unhealthy_pod_messages(payload) == []


def test_unhealthy_pod_messages_reports_unready_and_failed_pods() -> None:
    payload = {
        "items": [
            {
                "metadata": {"name": "service"},
                "status": {"phase": "Running", "containerStatuses": [{"name": "app", "ready": False}]},
            },
            {"metadata": {"name": "bad-job"}, "status": {"phase": "Failed"}},
        ]
    }

    assert _health.unhealthy_pod_messages(payload) == [
        "service: Running but containers not ready: app",
        "bad-job: Failed",
    ]


def test_classify_pod_health_ignores_job_attempt_pods() -> None:
    assert _health.classify_pod_health(
        {
            "items": [
                {
                    "metadata": {
                        "name": "om-job-pod",
                        "ownerReferences": [{"kind": "Job", "name": "om-job-aws-glue-metadata-ingestion"}],
                        "labels": {"job-name": "om-job-aws-glue-metadata-ingestion"},
                    },
                    "status": {"phase": "Failed"},
                }
            ]
        }
    ) == []


def test_classify_job_health_accepts_completed_job_with_failed_retry_pod() -> None:
    bad, warned = _health.classify_job_health(
        {
            "items": [
                {
                    "metadata": {"name": "postgresql-bootstrap"},
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"openlakeforge.io/readiness": "required"}}
                        }
                    },
                    "status": {
                        "failed": 2,
                        "succeeded": 1,
                        "conditions": [{"type": "Complete", "status": "True"}],
                    },
                }
            ]
        }
    )
    assert warned == []
    assert bad == []


def test_classify_job_health_blocks_failed_platform_bootstrap_job() -> None:
    bad, warned = _health.classify_job_health(
        {
            "items": [
                {
                    "metadata": {"name": "polaris-bootstrap"},
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"openlakeforge.io/readiness": "required"}}
                        }
                    },
                    "status": {"conditions": [{"type": "Failed", "status": "True"}]},
                }
            ]
        }
    )

    assert warned == []
    assert bad == ["polaris-bootstrap: Failed"]


def test_classify_job_health_warns_for_historical_cron_job_failure() -> None:
    bad, warned = _health.classify_job_health(
        {
            "items": [
                {
                    "metadata": {"name": "openmetadata-polaris-refresh-old"},
                    "spec": {"template": {"metadata": {"labels": {"openlakeforge.io/job": "catalog-refresh"}}}},
                    "status": {"conditions": [{"type": "Failed", "status": "True"}]},
                }
            ]
        }
    )

    assert bad == []
    assert warned == [
        "openmetadata-polaris-refresh-old: non-blocking Job is Failed; continuing E2E readiness"
    ]


def test_classify_job_health_blocks_active_suite_job() -> None:
    bad, warned = _health.classify_job_health(
        {
            "items": [
                {
                    "metadata": {
                        "name": "dagster-run-current",
                        "labels": {"dagster/job": "sales_order_revenue_pipeline"},
                    },
                    "status": {"active": 1},
                }
            ]
        },
        suite_jobs=("sales_order_revenue_pipeline",),
    )

    assert bad == ["dagster-run-current: Running"]
    assert warned == []


def test_classify_job_health_warns_for_historical_suite_job_failure() -> None:
    bad, warned = _health.classify_job_health(
        {
            "items": [
                {
                    "metadata": {
                        "name": "dagster-run-old",
                        "labels": {"dagster/job": "sales_order_revenue_pipeline"},
                    },
                    "status": {"conditions": [{"type": "Failed", "status": "True"}]},
                }
            ]
        },
        suite_jobs=("sales_order_revenue_pipeline",),
    )

    assert bad == []
    assert warned == ["dagster-run-old: non-blocking Job is Failed; continuing E2E readiness"]


def test_bounded_pod_diagnostics_describes_a_pod_when_logs_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_kubectl(_cfg, args: list[str], *, capture: bool = False) -> str:
        calls.append(args)
        if args[0] == "logs":
            raise E2EError("container is waiting to start")
        return "Events:\n  Failed to pull image"

    monkeypatch.setattr(_health, "kubectl", fake_kubectl)

    diagnostics = _health._bounded_pod_diagnostics(e2e_cfg(tmp_path), ["dagster-pod"])

    assert calls == [
        ["logs", "-n", "lakehouse", "pod/dagster-pod", "--all-containers", "--tail=80"],
        ["describe", "pod", "-n", "lakehouse", "dagster-pod"],
    ]
    assert "Failed to pull image" in diagnostics


def test_check_pods_ready_retries_until_pods_are_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pod_payloads = iter(
        [
            '{"items":[{"metadata":{"name":"service"},"status":{"phase":"Running","containerStatuses":[{"name":"app","ready":false}]}}]}',
            '{"items":[{"metadata":{"name":"service"},"status":{"phase":"Running","containerStatuses":[{"name":"app","ready":true}]}}]}',
        ]
    )

    def kubectl(_cfg, args, capture=False):  # noqa: ANN001, ARG001 - test double
        return next(pod_payloads) if args[1] == "pods" else '{"items":[]}'

    monkeypatch.setattr(_health, "kubectl", kubectl)
    monkeypatch.setattr(_health.time, "sleep", lambda _delay: None)

    _health.check_pods_ready(e2e_cfg(tmp_path))


def test_check_pods_ready_uses_job_completion_not_retry_pod_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    pod_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "postgresql-bootstrap-retry",
                        "ownerReferences": [{"kind": "Job", "name": "postgresql-bootstrap"}],
                    },
                    "status": {"phase": "Failed"},
                }
            ]
        }
    )
    job_payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "postgresql-bootstrap"},
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"openlakeforge.io/readiness": "required"}}
                        }
                    },
                    "status": {"conditions": [{"type": "Complete", "status": "True"}]},
                }
            ]
        }
    )

    def kubectl(_cfg, args, capture=False):  # noqa: ANN001, ARG001 - test double
        calls.append(args)
        return pod_payload if args[1] == "pods" else job_payload

    monkeypatch.setattr(_health, "kubectl", kubectl)

    _health.check_pods_ready(e2e_cfg(tmp_path))

    assert calls == [
        ["get", "pods", "-n", "lakehouse", "-o", "json"],
        ["get", "jobs", "-n", "lakehouse", "-o", "json"],
    ]


def test_job_diagnostics_use_configured_namespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(_health, "kubectl", lambda _cfg, args, capture=False: calls.append(args) or "job log")

    assert _health._bounded_job_diagnostics(e2e_cfg(tmp_path), ["polaris-bootstrap"]) == "polaris-bootstrap:\njob log"
    assert calls == [["logs", "-n", "lakehouse", "job/polaris-bootstrap", "--tail=80"]]


def test_pod_diagnostics_use_configured_namespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(_health, "kubectl", lambda _cfg, args, capture=False: calls.append(args) or "pod log")

    assert (
        _health._bounded_pod_diagnostics(e2e_cfg(tmp_path), ["trino-coordinator"])
        == "trino-coordinator:\npod log"
    )
    assert calls == [["logs", "-n", "lakehouse", "pod/trino-coordinator", "--all-containers", "--tail=80"]]
