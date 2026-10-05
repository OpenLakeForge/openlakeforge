from __future__ import annotations

import json
from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.status import StatusReport, StatusSection, collect_status
from olf.tooling.kubectl import Kubectl
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver


def test_collect_status_queries_pods_services_and_pvcs_in_order() -> None:
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout="ok\n", stderr="", duration_seconds=0.0))
    kubectl = Kubectl(runner, PathExecutableResolver(overrides={"kubectl": Path("kubectl")}))

    report = collect_status(
        kubectl,
        namespaces=("olf-system", "olf-dev"),
        context="kind-openlakeforge-local",
        kubeconfig=Path("/repo/.tmp/kubeconfigs/local.yaml"),
    )

    resources = [call.argv[call.argv.index("get") + 1] for call in runner.calls]
    assert resources == ["pods", "svc", "pvc", "pods", "svc", "pvc"]
    assert [section.title for section in report.sections] == [
        "Pods (olf-system)",
        "Services (olf-system)",
        "PVCs (olf-system)",
        "Pods (olf-dev)",
        "Services (olf-dev)",
        "PVCs (olf-dev)",
    ]
    assert all(call.kwargs["check"] is False for call in runner.calls)


def test_render_joins_sections_with_headers() -> None:
    result = CommandResult(argv=(), returncode=0, stdout="", stderr="no resources", duration_seconds=0.0)
    runner = RecordingRunner(result)
    kubectl = Kubectl(runner, PathExecutableResolver(overrides={"kubectl": Path("kubectl")}))

    report = collect_status(
        kubectl,
        namespaces=("olf-system", "olf-dev"),
        context="kind-openlakeforge-local",
        kubeconfig=Path("/repo/.tmp/kubeconfigs/local.yaml"),
    )

    rendered = report.render()
    assert "=== Pods (olf-system) ===" in rendered
    assert "=== Services (olf-dev) ===" in rendered
    assert "=== PVCs (olf-system) ===" in rendered


def test_collect_status_raises_when_a_query_fails() -> None:
    class _FailOnServices(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if "svc" in argv:
                return CommandResult(argv=(), returncode=1, stdout="", stderr="Unauthorized", duration_seconds=0.0)
            return CommandResult(argv=(), returncode=0, stdout="ok\n", stderr="", duration_seconds=0.0)

    runner = _FailOnServices()
    kubectl = Kubectl(runner, PathExecutableResolver(overrides={"kubectl": Path("kubectl")}))

    with pytest.raises(DeploymentPreconditionError, match="Services.*Unauthorized"):
        collect_status(
            kubectl,
            namespaces=("olf-system", "olf-dev"),
            context="kind-openlakeforge-local",
            kubeconfig=Path("/repo/.tmp/kubeconfigs/local.yaml"),
        )

    # Stops at the first failure, matching the old Make target's per-line
    # fail-fast behavior -- PVCs is never queried.
    assert not any("pvc" in call.argv for call in runner.calls)


def test_report_leads_with_the_contract_urls_and_serializes_them() -> None:
    report = StatusReport(
        sections=(StatusSection(title="Pods (olf-dev)", output="dagster Running"),),
        urls={"stage/dev/orchestration": "https://dagster.dev.olf.localhost"},
    )

    assert report.render().startswith("=== URLs ===\nstage/dev/orchestration: https://dagster.dev.olf.localhost\n\n")
    assert report.as_dict() == {
        "urls": {"stage/dev/orchestration": "https://dagster.dev.olf.localhost"},
        "sections": {"Pods (olf-dev)": "dagster Running"},
    }


class _CertificatesRunner(RecordingRunner):
    """Answers the certificates query with `result`, everything else with "ok"."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__()
        self._result = result

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if "certificates.cert-manager.io" in argv:
            return self._result
        return CommandResult(argv=(), returncode=0, stdout="ok", stderr="", duration_seconds=0.0)


def _certificate_report(result: CommandResult) -> StatusReport:
    return collect_status(
        Kubectl(_CertificatesRunner(result), PathExecutableResolver(overrides={"kubectl": Path("kubectl")})),
        namespaces=("olf-system", "olf-dev", "olf-prod"),
        context="kind-openlakeforge-local",
        kubeconfig=Path("/repo/.tmp/kubeconfigs/local.yaml"),
        certificates=True,
    )


def test_certificate_section_lists_owned_certificates_that_are_not_ready() -> None:
    def _certificate(namespace: str, name: str, *conditions: dict[str, str]) -> dict[str, object]:
        return {"metadata": {"namespace": namespace, "name": name}, "status": {"conditions": list(conditions)}}

    certificates = [
        _certificate("olf-system", "local-ca-probe", {"type": "Ready", "status": "True"}),
        _certificate(
            "olf-dev",
            "openlakeforge-routes-tls",
            {"type": "Ready", "status": "False", "reason": "Failed", "message": "issuer local-ca not ready"},
        ),
        _certificate("olf-prod", "openlakeforge-routes-tls"),
        _certificate("elsewhere", "theirs", {"type": "Ready", "status": "False", "reason": "Failed"}),
    ]
    stdout = json.dumps({"items": certificates})

    report = _certificate_report(CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0))

    assert report.sections[-1] == StatusSection(
        title="Certificates not Ready",
        output="olf-dev/openlakeforge-routes-tls: Failed: issuer local-ca not ready\n"
        "olf-prod/openlakeforge-routes-tls: NoReadyCondition: ",
    )


def test_certificate_section_reports_a_cluster_without_cert_manager() -> None:
    failed = CommandResult(argv=(), returncode=1, stdout="", stderr="no matches for kind\n", duration_seconds=0.0)

    assert _certificate_report(failed).sections[-1].output == "unavailable: no matches for kind"
