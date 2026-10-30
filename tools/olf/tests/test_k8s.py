import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from olf.infra import k8s


def test_secret_value_uses_explicit_kube_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []
    encoded = base64.b64encode(b"secret").decode("ascii")
    monkeypatch.setattr(
        k8s,
        "_kubectl",
        lambda args, **kwargs: calls.append((args, kwargs)) or encoded,
    )

    value = k8s.secret_value(
        "seaweedfs-s3-creds",
        "AWS_ACCESS_KEY_ID",
        "lakehouse",
        kube_context="kind-openlakeforge-local",
    )

    assert value == "secret"
    assert calls == [
        (
            [
                "get",
                "secret",
                "seaweedfs-s3-creds",
                "-n",
                "lakehouse",
                "-o",
                "jsonpath={.data.AWS_ACCESS_KEY_ID}",
            ],
            {"capture": True, "kube_context": "kind-openlakeforge-local"},
        )
    ]


def test_kubectl_command_uses_environment_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBE_CONTEXT", "aks-openlakeforge-poc")

    assert k8s.kubectl_command(["get", "pods"]) == [
        "kubectl",
        "--context",
        "aks-openlakeforge-poc",
        "get",
        "pods",
    ]


def test_kubectl_command_requires_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUBE_CONTEXT", raising=False)

    with pytest.raises(k8s.KubectlError, match="KUBE_CONTEXT is required"):
        k8s.kubectl_command(["get", "pods"])


def test_port_forward_uses_explicit_kube_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = Mock()
    process.poll.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(k8s.subprocess, "Popen", popen)
    monkeypatch.setattr(k8s, "_wait_for_port_forward", Mock())
    # Executable resolution (#127) is covered by test_toolchain_resolver.py;
    # here only the argv shape matters.
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda: "kubectl")

    with k8s.port_forward(
        "superset",
        8088,
        "lakehouse",
        local_port=18088,
        log_path=str(tmp_path / "port-forward.log"),
        kube_context="kind-openlakeforge-local",
    ) as local_port:
        assert local_port == 18088

    assert popen.call_args.args[0] == [
        "kubectl",
        "--context",
        "kind-openlakeforge-local",
        "port-forward",
        "svc/superset",
        "18088:8088",
        "-n",
        "lakehouse",
    ]
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10)


def test_wait_for_port_forward_retries_until_the_listener_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = Mock()
    process.poll.return_value = None
    attempts = iter((OSError("not ready"), MagicMock()))

    def create_connection(*_args: object, **_kwargs: object) -> MagicMock:
        result = next(attempts)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(k8s.socket, "create_connection", create_connection)
    monkeypatch.setattr(k8s.time, "sleep", lambda _delay: None)

    k8s._wait_for_port_forward(process, 18088, str(tmp_path / "port-forward.log"))


def test_wait_for_port_forward_reports_an_early_process_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_path = tmp_path / "port-forward.log"
    log_path.write_text("error: services \"missing\" not found\n")
    process = Mock()
    process.poll.return_value = 1

    with pytest.raises(k8s.KubectlError, match="services .*missing.*not found"):
        k8s._wait_for_port_forward(process, 18088, str(log_path))


def test_wait_for_port_forward_times_out_and_reports_the_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_path = tmp_path / "port-forward.log"
    log_path.write_text("still starting\n")
    process = Mock()
    process.poll.return_value = None
    clock = iter((0.0, 0.0, k8s.PORT_FORWARD_READY_TIMEOUT_SECONDS + 0.1))

    monkeypatch.setattr(k8s.socket, "create_connection", Mock(side_effect=OSError("not ready")))
    monkeypatch.setattr(k8s.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(k8s.time, "sleep", lambda _delay: None)

    with pytest.raises(k8s.KubectlError, match=r"(?s)timed out.*still starting"):
        k8s._wait_for_port_forward(process, 18088, str(log_path))


def test_port_forward_cleans_up_after_readiness_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = Mock()
    process.poll.return_value = 0
    monkeypatch.setattr(k8s.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(k8s, "_wait_for_port_forward", Mock(side_effect=k8s.KubectlError("not ready")))
    monkeypatch.setattr(k8s, "_kubectl_executable", lambda: "kubectl")

    with pytest.raises(k8s.KubectlError, match="not ready"):
        with k8s.port_forward(
            "polaris",
            8181,
            "lakehouse",
            log_path=str(tmp_path / "port-forward.log"),
            kube_context="kind-openlakeforge-local",
        ):
            pass

    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10)


def test_dagster_yaml_job_image_rewrite_preserves_indent_and_trailing_newline() -> None:
    src = 'run_launcher:\n  config:\n    job_image: "old:tag"\n'
    out = k8s.dagster_yaml_with_job_image(src, "new:tag")
    assert '    job_image: "new:tag"' in out
    assert out.endswith("\n")


def test_dagster_yaml_job_image_missing_raises() -> None:
    with pytest.raises(k8s.KubectlError):
        k8s.dagster_yaml_with_job_image("run_launcher:\n  config: {}\n", "new:tag")


def test_deployment_patch_syncs_dagster_current_image() -> None:
    containers = [
        {"name": "dagster", "env": [{"name": "DAGSTER_CURRENT_IMAGE", "value": "old"}]},
        {"name": "sidecar"},
    ]
    patch = k8s.deployment_container_patch(containers, "new:tag")
    entries = patch["spec"]["template"]["spec"]["containers"]
    dagster_entry = next(c for c in entries if c["name"] == "dagster")
    sidecar_entry = next(c for c in entries if c["name"] == "sidecar")
    assert dagster_entry["image"] == "new:tag"
    assert dagster_entry["env"] == [{"name": "DAGSTER_CURRENT_IMAGE", "value": "new:tag"}]
    assert sidecar_entry == {"name": "sidecar", "image": "new:tag"}


def test_deployment_patch_syncs_the_floe_job_image() -> None:
    """Floe's domain Jobs run on this image; a stale one runs the previous project."""
    env = [
        {"name": "DAGSTER_CURRENT_IMAGE", "value": "old"},
        {"name": "OPENLAKEFORGE_PROJECT_CODE_IMAGE", "value": "old"},
    ]
    patch = k8s.deployment_container_patch([{"name": "dagster", "env": env}], "new:tag")

    assert patch["spec"]["template"]["spec"]["containers"][0]["env"] == [
        {"name": "DAGSTER_CURRENT_IMAGE", "value": "new:tag"},
        {"name": "OPENLAKEFORGE_PROJECT_CODE_IMAGE", "value": "new:tag"},
    ]


def test_deployment_patch_combines_image_and_rollout_annotation() -> None:
    patch = k8s.deployment_container_patch(
        [{"name": "dagster"}],
        "new:tag",
        restarted_at="2026-07-20T12:00:00+00:00",
    )

    assert patch["spec"]["template"]["metadata"]["annotations"] == {
        "kubectl.kubernetes.io/restartedAt": "2026-07-20T12:00:00+00:00"
    }
    assert patch["spec"]["template"]["spec"]["containers"] == [{"name": "dagster", "image": "new:tag"}]


def test_deployment_patch_without_containers_raises() -> None:
    with pytest.raises(k8s.KubectlError):
        k8s.deployment_container_patch([], "new:tag")


def test_cronjob_patch_shape() -> None:
    patch = k8s.cronjob_container_patch([{"name": "archive"}], "new:tag")
    containers = patch["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    assert containers == [{"name": "archive", "image": "new:tag"}]


def test_patch_dagster_instance_configmap_rewrites_dagster_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    dagster_yaml = 'run_launcher:\n  config:\n    job_image: "repo/project-code:old"\n'

    monkeypatch.setattr(
        k8s,
        "resource_exists",
        lambda kind, name, namespace: (kind, name, namespace) == ("configmap", "dagster-instance", "lakehouse"),
    )
    monkeypatch.setattr(
        k8s,
        "_get_json",
        lambda kind, name, namespace: {"data": {"dagster.yaml": dagster_yaml}},
    )
    monkeypatch.setattr(k8s, "_kubectl", lambda args, **kwargs: calls.append(args) or "")

    k8s.patch_dagster_instance_configmap("repo/project-code:new", "lakehouse")

    assert calls == [
        [
            "patch",
            "configmap",
            "dagster-instance",
            "-n",
            "lakehouse",
            "--type",
            "merge",
            "-p",
            json.dumps(
                {"data": {"dagster.yaml": ('run_launcher:\n  config:\n    job_image: "repo/project-code:new"\n')}}
            ),
        ]
    ]


def test_patch_deployment_image_if_exists_uses_strategic_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(k8s, "resource_exists", lambda kind, name, namespace: True)
    monkeypatch.setattr(
        k8s,
        "_get_json",
        lambda kind, name, namespace: {"spec": {"template": {"spec": {"containers": [{"name": "dagster"}]}}}},
    )
    monkeypatch.setattr(k8s, "_kubectl", lambda args, **kwargs: calls.append(args) or "")

    k8s.patch_deployment_image_if_exists("dagster-webserver", "repo/project-code:new", "lakehouse")

    assert calls == [
        [
            "patch",
            "deployment",
            "dagster-webserver",
            "-n",
            "lakehouse",
            "--type",
            "strategic",
            "-p",
            json.dumps(
                {
                    "spec": {
                        "template": {"spec": {"containers": [{"name": "dagster", "image": "repo/project-code:new"}]}}
                    }
                }
            ),
        ]
    ]


def test_discover_dagster_user_deployments_filters_chart_generated_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        k8s,
        "_kubectl",
        lambda args, **kwargs: calls.append(args)
        or "\n".join(
            [
                "dagster-user-deployments-openlakeforge-dagster",
                "dagster-dagster-user-deployments-current-chart",
                "dagster-user-deployments-domain-a",
                "dagster-user-deployments-domain-b",
            ]
        ),
    )

    deployments = k8s.discover_dagster_user_deployments("lakehouse")

    assert deployments == [
        "dagster-user-deployments-openlakeforge-dagster",
        "dagster-dagster-user-deployments-current-chart",
        "dagster-user-deployments-domain-a",
        "dagster-user-deployments-domain-b",
    ]
    assert calls == [
        [
            "get",
            "deployments",
            "-n",
            "lakehouse",
            "-l",
            "app.kubernetes.io/name=dagster-user-deployments,app.kubernetes.io/instance=dagster",
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        ]
    ]


def test_set_project_code_image_updates_all_dagster_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    configmap_images = []
    deployment_images = []
    cronjob_images = []
    monkeypatch.setattr(
        k8s,
        "patch_dagster_instance_configmap",
        lambda image, namespace: configmap_images.append(image),
    )
    monkeypatch.setattr(
        k8s,
        "patch_deployment_image_if_exists",
        lambda deployment, image, namespace, **kwargs: (
            deployment_images.append((deployment, image, namespace, kwargs["restarted_at"])) or True
        ),
    )
    monkeypatch.setattr(
        k8s,
        "patch_cronjob_image_if_exists",
        lambda cronjob, image, namespace: cronjob_images.append((cronjob, image, namespace)),
    )
    monkeypatch.setattr(
        k8s,
        "discover_dagster_user_deployments",
        lambda namespace: ["dagster-user-deployments-domain-a", "dagster-user-deployments-domain-b"],
    )
    rollout_waits = []
    monkeypatch.setattr(
        k8s,
        "_wait_for_rollout_with_diagnostics",
        lambda deployment, namespace, timeout: rollout_waits.append((deployment, namespace, timeout)),
    )

    k8s.set_project_code_image("repo/project-code:new", "lakehouse")

    assert configmap_images == ["repo/project-code:new"]
    assert [(deployment, image, namespace) for deployment, image, namespace, _ in deployment_images] == [
        ("dagster-dagster-webserver", "repo/project-code:new", "lakehouse"),
        ("dagster-dagster-daemon", "repo/project-code:new", "lakehouse"),
        ("dagster-webserver", "repo/project-code:new", "lakehouse"),
        ("dagster-daemon", "repo/project-code:new", "lakehouse"),
        ("dagster-user-deployments-domain-a", "repo/project-code:new", "lakehouse"),
        ("dagster-user-deployments-domain-b", "repo/project-code:new", "lakehouse"),
    ]
    assert len({restarted_at for *_, restarted_at in deployment_images}) == 1
    assert cronjob_images == [("openlakeforge-k8s-log-archive", "repo/project-code:new", "lakehouse")]
    assert rollout_waits == [(deployment, "lakehouse", "600s") for deployment, *_ in deployment_images]


def _job_kubectl(status: str, log: str = ""):
    def fake(args, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        return {"get": status, "logs": log}.get(args[0], "")

    return fake


_JOB = {"metadata": {"name": "ingest", "namespace": "olf-system"}}


def test_run_job_returns_on_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "_kubectl", _job_kubectl("Complete"))
    k8s.run_job(_JOB, timeout_seconds=60)


def test_run_job_raises_promptly_on_failed_with_the_token_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(k8s, "_kubectl", _job_kubectl("Failed", "boom jwt=eyJhbGci.eyJzdWIi.c2ln-_x end"))
    monkeypatch.setattr(k8s.time, "sleep", lambda _s: pytest.fail("kept polling a failed Job"))
    with pytest.raises(k8s.KubectlError) as raised:
        k8s.run_job(_JOB, timeout_seconds=60)
    assert "boom jwt=*** end" in str(raised.value)
    assert "eyJ" not in str(raised.value)
