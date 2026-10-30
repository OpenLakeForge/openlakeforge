"""The process-execution primitives shared by every provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from olf.common.tooling.docker import Docker
from olf.common.tooling.helm import Helm
from olf.common.tooling.kind import Kind
from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import ProcessRunner
from olf.common.tooling.resolver import ExecutableResolver, build_resolver
from olf.common.tooling.terraform import Terraform


@dataclass(frozen=True)
class Toolkit:
    runner: ProcessRunner
    resolver: ExecutableResolver
    terraform: Terraform
    helm: Helm
    kubectl: Kubectl
    docker: Docker
    kind: Kind

    @classmethod
    def default(
        cls,
        *,
        log_commands: bool = False,
        overrides: Mapping[str, Path] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Toolkit:
        runner = ProcessRunner(log_commands=log_commands)
        resolver = build_resolver(overrides=overrides, environ=environ)
        return cls(
            runner=runner,
            resolver=resolver,
            terraform=Terraform(runner, resolver),
            helm=Helm(runner, resolver),
            kubectl=Kubectl(runner, resolver),
            docker=Docker(runner, resolver),
            kind=Kind(runner, resolver),
        )
