from __future__ import annotations

from pathlib import Path

from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment import kube_ops
from olf.tooling.kubectl import Kubectl
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver
from olf.tooling.terraform import Terraform

_KUBECONFIG = Path("/repo/.tmp/kubeconfigs/local.yaml")
_CONTEXT = "kind-openlakeforge-local"


def _kubectl(runner: RecordingRunner) -> Kubectl:
    return Kubectl(runner, PathExecutableResolver(overrides={"kubectl": Path("kubectl")}))


def _terraform(runner: RecordingRunner) -> Terraform:
    return Terraform(runner, PathExecutableResolver(overrides={"terraform": Path("terraform")}))


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


def _fail() -> CommandResult:
    return CommandResult(argv=(), returncode=1, stdout="", stderr="", duration_seconds=0.0)


def test_jobs_with_prefix_filters_and_strips_job_batch_prefix() -> None:
    runner = RecordingRunner(_ok("job.batch/polaris-bootstrap-abc\njob.batch/dagster-run-xyz\n"))
    kubectl = _kubectl(runner)

    jobs = kube_ops.jobs_with_prefix(
        kubectl, "polaris-bootstrap-", namespace="lakehouse", context=_CONTEXT, kubeconfig=_KUBECONFIG
    )

    assert jobs == ["polaris-bootstrap-abc"]


class _JobStatusRunner(RecordingRunner):
    def __init__(self, *, job_list: str, failed_status: dict[str, str]) -> None:
        super().__init__()
        self._job_list = job_list
        self._failed_status = failed_status

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if "-o" in argv and "name" in argv:
            return _ok(self._job_list)
        if "jsonpath={.status.failed}" in argv:
            job_name = argv[argv.index("get") + 2]
            return _ok(self._failed_status.get(job_name, ""))
        return _ok()


def test_cleanup_failed_jobs_by_prefix_only_deletes_failed_jobs() -> None:
    runner = _JobStatusRunner(
        job_list="job.batch/polaris-bootstrap-a\njob.batch/polaris-bootstrap-b\n",
        failed_status={"polaris-bootstrap-a": "1", "polaris-bootstrap-b": "0"},
    )
    kubectl = _kubectl(runner)

    deleted = kube_ops.cleanup_failed_jobs_by_prefix(
        kubectl, "polaris-bootstrap-", namespace="lakehouse", context=_CONTEXT, kubeconfig=_KUBECONFIG
    )

    assert deleted == ["polaris-bootstrap-a"]
    delete_calls = [c for c in runner.calls if "delete" in c.argv]
    assert len(delete_calls) == 1
    assert "polaris-bootstrap-a" in delete_calls[0].argv
    assert "polaris-bootstrap-b" not in delete_calls[0].argv


def test_import_namespace_skips_when_already_in_state() -> None:
    runner = RecordingRunner(_ok())  # state show succeeds
    kubectl = _kubectl(runner)
    terraform = _terraform(runner)

    result = kube_ops.import_namespace_if_missing_in_state(
        terraform,
        kubectl,
        terraform_dir=Path("/repo/infra/terraform/environments/local"),
        resource_addr="kubernetes_namespace_v1.lakehouse",
        namespace="lakehouse",
        context=_CONTEXT,
        kubeconfig=_KUBECONFIG,
    )

    assert result is False
    assert not any(c.argv[2] == "import" for c in runner.calls if c.argv[0] == "terraform")


class _ImportScriptedRunner(RecordingRunner):
    def __init__(self, *, state_ok: bool, namespace_ok: bool) -> None:
        super().__init__()
        self._state_ok = state_ok
        self._namespace_ok = namespace_ok

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if argv[0] == "terraform" and "state" in argv:
            return _ok() if self._state_ok else _fail()
        if argv[0] == "kubectl" and "namespace" in argv:
            return _ok() if self._namespace_ok else _fail()
        return _ok()


def test_import_namespace_skips_when_namespace_absent_in_cluster() -> None:
    runner = _ImportScriptedRunner(state_ok=False, namespace_ok=False)
    kubectl = _kubectl(runner)
    terraform = _terraform(runner)

    result = kube_ops.import_namespace_if_missing_in_state(
        terraform,
        kubectl,
        terraform_dir=Path("/repo/infra/terraform/environments/local"),
        resource_addr="kubernetes_namespace_v1.lakehouse",
        namespace="lakehouse",
        context=_CONTEXT,
        kubeconfig=_KUBECONFIG,
    )

    assert result is False
    assert not any(c.argv[2] == "import" for c in runner.calls if c.argv[0] == "terraform")


def test_import_namespace_imports_when_present_in_cluster_but_missing_in_state() -> None:
    runner = _ImportScriptedRunner(state_ok=False, namespace_ok=True)
    kubectl = _kubectl(runner)
    terraform = _terraform(runner)

    result = kube_ops.import_namespace_if_missing_in_state(
        terraform,
        kubectl,
        terraform_dir=Path("/repo/infra/terraform/environments/local"),
        resource_addr="kubernetes_namespace_v1.lakehouse",
        namespace="lakehouse",
        variables={"namespace": "lakehouse"},
        context=_CONTEXT,
        kubeconfig=_KUBECONFIG,
    )

    assert result is True
    import_call = next(c for c in runner.calls if c.argv[0] == "terraform" and c.argv[2] == "import")
    assert import_call.argv[-2:] == ["kubernetes_namespace_v1.lakehouse", "lakehouse"]
    assert "-var=namespace=lakehouse" in import_call.argv
