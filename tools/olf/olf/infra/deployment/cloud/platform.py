"""Cloud (AWS/Azure) static platform Terraform orchestration.

Port of `scripts/{aws,azure}/stack/platform-up.sh`. Reuses `deployment.
charts.prepare_cached_chart`/`prepare_cached_dagster_chart_no_schema` and
`deployment.kube_ops` exactly as the local provider does; only the
Terraform variables differ by provider, routed through `CloudBackend`.
"""

from __future__ import annotations

from collections.abc import Mapping

from olf.common import log
from olf.common.deployment.charts import prepare_chart
from olf.common.deployment.errors import CommandExecutionError
from olf.common.tooling.retry import RetryPolicy, run_with_retry
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment import kube_ops
from olf.infra.deployment.cloud.backend import CloudBackend, FoundationFacts
from olf.infra.deployment.cloud.config import CloudDeploymentConfig
from olf.infra.deployment.local.platform import require_no_shared_namespace_replacement, require_no_stage_removal
from olf.infra.deployment.storage_resize import require_storage_size_applies

_NAMESPACE_RESOURCE_ADDR = "kubernetes_namespace_v1.shared"
_POLARIS_JOB_PREFIXES = ("polaris-bootstrap-", "polaris-metastore-bootstrap-", "postgresql-credentials-")


def _retry_if_logging(description: str, policy: RetryPolicy):  # noqa: ANN202
    def _predicate(exc: CommandExecutionError, attempt: int) -> bool:  # noqa: ARG001
        if attempt < policy.max_attempts:
            delay = policy.delay_for_attempt(attempt)
            log.warn(f"{description} failed on attempt {attempt}/{policy.max_attempts}; retrying in {delay:g}s...")
        else:
            log.error(f"{description} failed after {attempt} attempt(s).")
        return True

    return _predicate


def prepare_charts(config: CloudDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    for setting in config.charts.values():
        prepare_chart(
            setting,
            helm=tools.helm,
            paths=config.paths,
            env=env,
            retry_policy=config.terraform.apply_retry,
        )


def platform_up(
    config: CloudDeploymentConfig,
    tools: Toolkit,
    backend: CloudBackend,
    facts: FoundationFacts,
    *,
    env: Mapping[str, str],
) -> None:
    platform_dir = config.paths.platform_terraform_dir

    config.context.prepare_directories()
    prepare_charts(config, tools, env=env)

    log.step(f"Initializing Terraform {backend.scope} platform...")
    tools.terraform.init(platform_dir, env=env)

    require_no_stage_removal(config, tools, kube_context=facts.kube_context, env=env)
    require_no_shared_namespace_replacement(config, tools, kube_context=facts.kube_context, env=env)
    require_storage_size_applies(config, tools, kube_context=facts.kube_context, env=env)

    variables = backend.platform_apply_variables(config, facts)
    var_files = (str(config.terraform.var_file),) if config.terraform.var_file is not None else ()

    kube_ops.import_namespace_if_missing_in_state(
        tools.terraform,
        tools.kubectl,
        terraform_dir=platform_dir,
        resource_addr=_NAMESPACE_RESOURCE_ADDR,
        namespace=config.context.shared_namespace,
        var_files=var_files,
        variables=variables,
        context=facts.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    )

    def _apply_once() -> None:
        if backend.cleanup_polaris_jobs_before_apply():
            for prefix in _POLARIS_JOB_PREFIXES:
                kube_ops.cleanup_failed_jobs_by_prefix(
                    tools.kubectl,
                    prefix,
                    namespace=config.context.shared_namespace,
                    context=facts.kube_context,
                    kubeconfig=config.paths.kubeconfig_path,
                    env=env,
                )
        tools.terraform.apply(platform_dir, var_files=var_files, variables=variables, env=env)

    log.step(f"Applying Terraform {backend.scope} platform...")
    run_with_retry(
        _apply_once,
        policy=config.terraform.apply_retry,
        retry_if=_retry_if_logging("Terraform apply", config.terraform.apply_retry),
    )

    log.step(f"Static OpenLakeForge {backend.scope} platform is applied.")
