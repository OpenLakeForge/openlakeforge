"""Thin kind adapter. Cluster lifecycle stays owned by Terraform for now."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from olf.tooling.process import CommandResult, ProcessRunner
from olf.tooling.resolver import ExecutableResolver


class Kind:
    def __init__(self, runner: ProcessRunner, resolver: ExecutableResolver) -> None:
        self._runner = runner
        self._resolver = resolver

    def _executable(self) -> Path:
        return self._resolver.resolve("kind")

    def _run(self, args: list[str], *, env: Mapping[str, str] | None = None, check: bool = True) -> CommandResult:
        return self._runner.run([str(self._executable()), *args], env=env, check=check)

    def get_clusters(self, *, env: Mapping[str, str] | None = None) -> list[str]:
        result = self._run(["get", "clusters"], env=env)
        return [line for line in result.stdout.splitlines() if line]

    def get_nodes(self, cluster_name: str, *, env: Mapping[str, str] | None = None) -> list[str]:
        result = self._run(["get", "nodes", "--name", cluster_name], env=env)
        return [line for line in result.stdout.splitlines() if line]

    def export_kubeconfig(
        self,
        cluster_name: str,
        *,
        kubeconfig_path: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return self._run(
            ["export", "kubeconfig", "--name", cluster_name, "--kubeconfig", str(kubeconfig_path)],
            env=env,
        )

    def load_docker_image(
        self,
        image: str,
        *,
        cluster_name: str,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return self._run(["load", "docker-image", image, "--name", cluster_name], env=env)

    def create_cluster(
        self,
        cluster_name: str,
        *,
        config_path: Path | None = None,
        kubeconfig_path: Path | None = None,
        wait: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["create", "cluster", "--name", cluster_name]
        if config_path is not None:
            args += ["--config", str(config_path)]
        if kubeconfig_path is not None:
            args += ["--kubeconfig", str(kubeconfig_path)]
        if wait is not None:
            args += ["--wait", wait]
        return self._run(args, env=env)

    def delete_cluster(
        self,
        cluster_name: str,
        *,
        kubeconfig_path: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = ["delete", "cluster", "--name", cluster_name]
        if kubeconfig_path is not None:
            args += ["--kubeconfig", str(kubeconfig_path)]
        return self._run(args, env=env)
