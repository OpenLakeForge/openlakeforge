from __future__ import annotations

from pathlib import Path

from _tooling_support import RecordingRunner

from olf.tooling.helm import Helm
from olf.tooling.resolver import PathExecutableResolver


def _helm() -> tuple[Helm, RecordingRunner]:
    runner = RecordingRunner()
    resolver = PathExecutableResolver(overrides={"helm": Path("helm")})
    return Helm(runner, resolver), runner


def test_repo_add_builds_exact_argv() -> None:
    helm, runner = _helm()

    helm.repo_add("trino", "https://trinodb.github.io/charts")

    assert runner.last_call.argv == [
        "helm",
        "repo",
        "add",
        "trino",
        "https://trinodb.github.io/charts",
        "--force-update",
    ]


def test_repo_add_without_force_update() -> None:
    helm, runner = _helm()

    helm.repo_add("trino", "https://trinodb.github.io/charts", force_update=False)

    assert "--force-update" not in runner.last_call.argv


def test_repo_and_cache_config_are_passed_as_scoped_environment() -> None:
    helm, runner = _helm()
    repo_config = Path("/repo/.tmp/helm/local/repositories.yaml")
    repo_cache = Path("/repo/.tmp/helm/local/repository-cache")

    helm.repo_update(repository_config=repo_config, repository_cache=repo_cache)

    assert runner.last_call.argv == ["helm", "repo", "update"]
    assert runner.last_call.kwargs["env"] == {
        "HELM_REPOSITORY_CONFIG": str(repo_config),
        "HELM_REPOSITORY_CACHE": str(repo_cache),
    }


def test_pull_builds_expected_argv_with_version_and_destination() -> None:
    helm, runner = _helm()

    helm.pull("trino/trino", version="1.42.2", destination=Path("/repo/.tmp/helm/local/charts"))

    assert runner.last_call.argv == [
        "helm",
        "pull",
        "trino/trino",
        "--version",
        "1.42.2",
        "--destination",
        "/repo/.tmp/helm/local/charts",
    ]


def test_pull_with_untar_options() -> None:
    helm, runner = _helm()

    helm.pull("dagster/dagster", version="1.13.6", untar=True, untar_dir=Path("/tmp/dagster-chart"))

    assert runner.last_call.argv == [
        "helm",
        "pull",
        "dagster/dagster",
        "--version",
        "1.13.6",
        "--untar",
        "--untardir",
        "/tmp/dagster-chart",
    ]


def test_show_chart_defaults_to_check_false() -> None:
    helm, runner = _helm()

    helm.show_chart(Path("/repo/.tmp/helm/local/charts/trino-1.42.2.tgz"))

    assert runner.last_call.kwargs["check"] is False


def test_status_defaults_to_check_false_and_builds_exact_argv() -> None:
    helm, runner = _helm()

    helm.status("trino", namespace="lakehouse", kube_context="kind-openlakeforge-local")

    assert runner.last_call.argv == [
        "helm",
        "--kube-context",
        "kind-openlakeforge-local",
        "status",
        "trino",
        "-n",
        "lakehouse",
    ]
    assert runner.last_call.kwargs["check"] is False


def test_uninstall_builds_exact_argv() -> None:
    helm, runner = _helm()

    helm.uninstall("polaris", namespace="lakehouse", kube_context="kind-openlakeforge-local")

    assert runner.last_call.argv == [
        "helm",
        "--kube-context",
        "kind-openlakeforge-local",
        "uninstall",
        "polaris",
        "-n",
        "lakehouse",
    ]


def test_kube_context_is_omitted_when_not_given() -> None:
    helm, runner = _helm()

    helm.status("seaweedfs", namespace="lakehouse")

    assert "--kube-context" not in runner.last_call.argv


def test_package_builds_expected_argv() -> None:
    helm, runner = _helm()

    helm.package(Path("/tmp/dagster-chart/dagster"), destination=Path("/repo/.tmp/helm/local/charts"))

    assert runner.last_call.argv == [
        "helm",
        "package",
        "/tmp/dagster-chart/dagster",
        "--destination",
        "/repo/.tmp/helm/local/charts",
    ]
