from __future__ import annotations

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
