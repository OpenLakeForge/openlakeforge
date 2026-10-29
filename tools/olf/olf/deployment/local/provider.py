"""`DeploymentProvider` implementation for the local (kind-based) stack.

Every method is a one-line delegate into a sibling `olf.deployment.local.*`
module, so the granular Make target delegates (`--phase foundation`, etc.)
and the combined `olf deploy`/`olf destroy` verbs share the exact same code
path - nothing is duplicated between them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from olf import log
from olf.deployment.engine import DeploymentPhase, Toolkit
from olf.deployment.inspection import DoctorItem, DoctorReport, base_report, docker_health
from olf.deployment.local.config import LocalDeploymentConfig
from olf.tooling.docker import ambient_registry_env

if TYPE_CHECKING:
    from olf.deployment.context import DeploymentContext
    from olf.deployment.status import StatusReport
    from olf.github_app import GitHubApp
    from olf.profile import GitHubSpec


def _auth_off_notes(governance: bool) -> tuple[str, ...]:
    login = " OpenMetadata login: admin@open-metadata.org, password in Secret openmetadata-admin" if governance else ""
    return (f"Authentication is off.{login}",)


@dataclass
class LocalProvider:
    config: LocalDeploymentConfig
    tools: Toolkit
    _environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    _github_app: GitHubApp | None = field(default=None, init=False, repr=False)

    @classmethod
    def create(
        cls,
        config: LocalDeploymentConfig,
        *,
        toolkit: Toolkit | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> LocalProvider:

        return cls(
            config=config,
            tools=toolkit or Toolkit.default(),
            _environ=environ if environ is not None else os.environ,
        )

    @property
    def context(self) -> DeploymentContext:
        return self.config.context

    @cached_property
    def env(self) -> dict[str, str]:
        """The command-scoped environment overlay, built once per provider instance.

        Reproduces `scripts/lib/common.sh::configure_deployment_scope`'s
        ordering exactly: resolve the ambient Docker context's real engine
        endpoint (only when `DOCKER_HOST` isn't already set) before scoping
        `DOCKER_CONFIG`, so an isolated config can't silently fall back to
        `/var/run/docker.sock`.

        Deliberately built without `self._environ` as `base`: this dict is
        what `ProcessRunner` stores verbatim on `CommandExecutionError` for
        diagnostics, and `ProcessRunner._run_once` already layers it over a
        full `os.environ` snapshot for actual execution - so carrying the
        ambient environment forward here too would only leak arbitrary
        inherited secrets (e.g. `DATABASE_URL`) into failure output without
        changing what a subprocess actually sees.
        """
        docker_host = None
        if not self._environ.get("DOCKER_HOST"):
            # Docker contexts live in the caller's own config; `_environ`
            # already carries the scoped OLF-owned one, which has none. A
            # Rancher Desktop or remote-engine context would otherwise be
            # invisible and every command target the default socket.
            docker_host = self.tools.docker.resolve_current_engine_endpoint(
                env=ambient_registry_env(self._environ)
            )
        self.context.prepare_directories()
        return self.context.command_env(docker_host=docker_host)

    def foundation_up(self) -> None:
        from olf.deployment.local import foundation, network_canary

        foundation.foundation_up(self.config, self.tools, env=self.env)
        network_canary.require_policy_enforcement(self.config, self.tools, env=self.env)

    def foundation_down(self, *, force: bool = False) -> None:
        from olf.deployment.local import foundation

        foundation.foundation_down(
            self.config, self.tools, env=self.env, force=force or self.config.force_foundation_down
        )

    def prepare_images(self) -> None:
        from olf.deployment.local import prefetch

        prefetch.prefetch_images(self.config, self.tools, env=self.env)

    def build_project_image(self, *, push: bool = True) -> str:
        """Build the project-code image, optionally push it, and load it into kind.

        Local has no registry of its own, so a publishable revision depends on
        an external one (GHCR, Docker Hub) named by
        `PROJECT_CODE_IMAGE_REPOSITORY`. Pushing is what creates the registry
        digest `olf project build` requires -- a kind-only image has just a
        local config Id, which is not pullable. The kind load still happens
        either way so the default `Never` pull policy keeps working.

        The unmodified example is already published by digest: that reference is the result, with no build.
        """
        from olf.deployment.local import images

        if self.config.images.project_code_published:
            return self.config.images.project_code_image
        image = images.build_project_code_image(self.config, self.tools, env=self.env)
        if push:
            log.step(f"Pushing project-code image: {image}")
            self.tools.docker.push(
                image, env=ambient_registry_env(self.env), retry_policy=self.config.images.push_retry
            )
        images.load_image_into_kind(image, self.config, self.tools, env=self.env)
        return image

    def before_deploy(self) -> None:
        """Run the GitHub manifest flow now, while the operator is still at the terminal. The credentials stay in
        this object until `platform_up` writes the Secret."""
        from olf import github_app
        from olf.deployment.errors import DeploymentPreconditionError
        from olf.deployment.local import platform

        if self._github_app is None and platform.github_connect_needed(self.config, self.tools, env=self.env):
            try:
                self._github_app = github_app.create_app(self.context)
            except github_app.GitHubConnectError as exc:
                raise DeploymentPreconditionError(str(exc)) from exc

    def platform_up(self) -> None:
        from olf.deployment.local import platform

        if self.config.paths.foundation_state_path.is_file():  # else platform_up refuses; do not spend the App first
            self.before_deploy()
        platform.platform_up(self.config, self.tools, env=self.env, github_app=self._github_app)

    def platform_down(self) -> None:
        from olf.deployment.local import teardown

        teardown.platform_down(self.config, self.tools, env=self.env)

    def artifacts_deploy(self) -> None:
        from olf.deployment.local import artifacts

        artifacts.artifacts_deploy(self.config, self.tools, env=self.env)
        self._log_sign_in_steps()

    def _log_sign_in_steps(self) -> None:
        # Last output of a full deploy, so the install step is not buried under the artifacts phase.
        topology = self.context.topology
        if topology.access is None:
            return
        scheme = "http" if topology.access.issuer == "none" else "https"
        log.step(f"OpenLakeForge is ready: {scheme}://{topology.access.base_domain}")
        if topology.identity and topology.identity.github:
            self._log_github_install(topology.identity.github)

    def _log_github_install(self, github: GitHubSpec) -> None:
        from olf import github_app
        from olf.deployment.local import platform

        secret = platform.read_github_app_secret(self.config, self.tools, env=self.env)
        if "app-id" not in secret:
            return  # a hand-made OAuth App, or a Secret from before app-id was stored: nothing to check or link
        host = github.host or "github.com"
        installed = github_app.app_installed(
            host=host, organization=github.organization, app_id=secret["app-id"], pem=secret["private-key"]
        )
        if installed:
            log.info(f"GitHub App installed on {github.organization}")
        else:
            link = github_app.install_url(host, secret["app-slug"])
            qualifier = "" if installed is False else " (if not installed; GitHub could not confirm)"
            log.info(f"Install the GitHub App on {github.organization}{qualifier}: {link}")

    def status(self) -> StatusReport:
        from olf.access import contract_access
        from olf.contracts import ProviderContractError
        from olf.deployment.status import collect_status

        try:
            urls = contract_access(self._contracts()).get("routes")
        except ProviderContractError as exc:
            log.warn(f"no service URLs: {exc}")
            urls = None
        return collect_status(
            self.tools.kubectl,
            namespaces=(
                self.context.shared_namespace,
                *(self.context.namespace_for(stage) for stage in self.context.enabled_stages),
            ),
            context=self.config.kube_context,
            kubeconfig=self.config.paths.kubeconfig_path,
            env=self.env,
            urls=urls,
            certificates=self.context.topology.access is not None,  # a quickstart has no cert-manager
            # Authentication off (ADR 0014): Superset opens as Admin, but OpenMetadata has no anonymous mode.
            notes=() if self.context.topology.identity else _auth_off_notes(self.config.features.governance_enabled),
        )

    def _contract_dir(self) -> Path:
        return Path(
            self._environ.get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", self.config.paths.platform_terraform_dir)
        ).resolve()

    def _contracts(self) -> dict | None:
        from olf.contracts import load_provider_contracts

        # environ=self._environ (not bare os.environ): an installed
        # distribution's platform state lives under OLF_HOME, not next to
        # the contract dir - without the scoped environ here, `terraform
        # output` reads the read-only payload's absent default state and
        # this always reports "unavailable".
        return load_provider_contracts(str(self._contract_dir()), environ=self._environ, resolver=self.tools.resolver)

    def forward(self) -> None:
        from olf.deployment.local import forward as forward_module
        from olf.deployment.portforward import PortForwardSupervisor

        spec = forward_module.local_forward_spec(self.config, self.tools, env=self.env)
        for line in spec.banner:
            print(line)  # noqa: T201 - user-facing CLI banner
        supervisor = PortForwardSupervisor(self.tools.kubectl, log_prefix=self.config.paths.port_forward_log_prefix)
        supervisor.run(spec, env=self.env)

    def plan(self, phase: DeploymentPhase) -> bool:
        from olf.deployment.errors import DeploymentPreconditionError
        from olf.deployment.local import foundation, platform

        changes = False
        if phase in (DeploymentPhase.ALL, DeploymentPhase.FOUNDATION):
            self.context.prepare_directories()
            self.tools.terraform.init(self.config.paths.foundation_terraform_dir, env=self.env)
            result = self.tools.terraform.plan(
                self.config.paths.foundation_terraform_dir,
                variables=foundation.foundation_apply_variables(self.config, self.tools),
                detailed_exitcode=True,
                env=self.env,
            )
            changes = changes or result.returncode == 2
        if phase in (DeploymentPhase.ALL, DeploymentPhase.PLATFORM):
            if not self.config.paths.foundation_state_path.is_file():
                if phase == DeploymentPhase.PLATFORM:
                    raise DeploymentPreconditionError("foundation state is required before planning the local platform")
                return changes
            platform.prepare_charts(self.config, self.tools, env=self.env)
            self.tools.terraform.init(self.config.paths.platform_terraform_dir, env=self.env)
            result = self.tools.terraform.plan(
                self.config.paths.platform_terraform_dir,
                var_files=platform.platform_var_files(self.config),
                variables=platform.platform_apply_variables(self.config),
                detailed_exitcode=True,
                env=self.env,
            )
            changes = changes or result.returncode == 2
        return changes

    def doctor(self, phase: DeploymentPhase) -> DoctorReport:
        required = ["terraform", "kubectl", "docker", "kind"]
        if phase in (DeploymentPhase.ALL, DeploymentPhase.PLATFORM):
            required.append("helm")
        items = base_report(
            project_root=self.config.paths.repo_root,
            distribution_root=self.config.paths.distribution_root,
            tools=self.tools,
            required_tools=required,
        )
        # See the cloud provider's doctor: a scoped DOCKER_CONFIG loses the
        # user's `currentContext`, so resolve the active engine endpoint or a
        # non-default context is falsely reported unreachable.
        docker_host = None
        if not self._environ.get("DOCKER_HOST"):
            docker_host = self.tools.docker.resolve_current_engine_endpoint(env=dict(self._environ))
        items.append(docker_health(self.tools, env=self.context.command_env(docker_host=docker_host)))
        if phase in (DeploymentPhase.ALL, DeploymentPhase.PLATFORM):
            tfvars = self.config.terraform.var_file
            if tfvars is not None:
                items.append(DoctorItem("local platform tfvars", tfvars.is_file(), str(tfvars)))
        if phase in (DeploymentPhase.PLATFORM, DeploymentPhase.ARTIFACTS):
            items.append(
                DoctorItem(
                    "foundation state",
                    self.config.paths.foundation_state_path.is_file(),
                    str(self.config.paths.foundation_state_path),
                )
            )
        if phase in (DeploymentPhase.ALL, DeploymentPhase.ARTIFACTS):
            from olf.contracts import ProviderContractError

            contract_dir = self._contract_dir()
            try:
                provider_contracts = self._contracts()
            except ProviderContractError as exc:
                items.append(DoctorItem("local platform provider contracts", False, str(exc)))
            else:
                detail = str(contract_dir) if provider_contracts is not None else f"unavailable from {contract_dir}"
                items.append(DoctorItem("local platform provider contracts", provider_contracts is not None, detail))
        return DoctorReport(tuple(items))
