from __future__ import annotations

from pathlib import Path

from _tooling_support import RecordingRunner

from olf.tooling.kind import Kind
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver


def _kind(result: CommandResult | None = None) -> tuple[Kind, RecordingRunner]:
    runner = RecordingRunner(result)
    resolver = PathExecutableResolver(overrides={"kind": Path("kind")})
    return Kind(runner, resolver), runner


def test_export_kubeconfig_builds_exact_argv() -> None:
    kind, runner = _kind()
    kubeconfig = Path("/repo/.tmp/kubeconfigs/local.yaml")

    kind.export_kubeconfig("openlakeforge-local", kubeconfig_path=kubeconfig)

    assert runner.last_call.argv == [
        "kind",
        "export",
        "kubeconfig",
        "--name",
        "openlakeforge-local",
        "--kubeconfig",
        str(kubeconfig),
    ]


def test_load_docker_image_builds_exact_argv() -> None:
    kind, runner = _kind()

    kind.load_docker_image("ghcr.io/openlakeforge/project-code:local", cluster_name="openlakeforge-local")

    assert runner.last_call.argv == [
        "kind",
        "load",
        "docker-image",
        "ghcr.io/openlakeforge/project-code:local",
        "--name",
        "openlakeforge-local",
    ]


def test_get_nodes_builds_exact_argv_and_parses_output_lines() -> None:
    kind, runner = _kind(
        CommandResult(
            argv=(),
            returncode=0,
            stdout="openlakeforge-local-control-plane\n",
            stderr="",
            duration_seconds=0.0,
        )
    )

    nodes = kind.get_nodes("openlakeforge-local")

    assert runner.last_call.argv == ["kind", "get", "nodes", "--name", "openlakeforge-local"]
    assert nodes == ["openlakeforge-local-control-plane"]


def test_get_clusters_parses_output_lines() -> None:
    kind, _runner = _kind(
        CommandResult(argv=(), returncode=0, stdout="openlakeforge-local\n", stderr="", duration_seconds=0.0)
    )

    assert kind.get_clusters() == ["openlakeforge-local"]


def test_create_and_delete_cluster_build_expected_argv() -> None:
    kind, runner = _kind()
    config = Path("/repo/infra/kind/local/kind-cluster.yaml")
    kubeconfig = Path("/repo/.tmp/kubeconfigs/local.yaml")

    kind.create_cluster("openlakeforge-local", config_path=config, kubeconfig_path=kubeconfig, wait="120s")
    assert runner.last_call.argv == [
        "kind",
        "create",
        "cluster",
        "--name",
        "openlakeforge-local",
        "--config",
        str(config),
        "--kubeconfig",
        str(kubeconfig),
        "--wait",
        "120s",
    ]

    kind.delete_cluster("openlakeforge-local", kubeconfig_path=kubeconfig)
    assert runner.last_call.argv == [
        "kind",
        "delete",
        "cluster",
        "--name",
        "openlakeforge-local",
        "--kubeconfig",
        str(kubeconfig),
    ]
