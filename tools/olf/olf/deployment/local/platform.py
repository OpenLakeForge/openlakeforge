"""Local static platform Terraform orchestration.

Port of `scripts/local/stack/platform-up.sh`. Preserves the exact drift
recovery, namespace adoption, Polaris job cleanup, and bounded-retry
semantics of the shell script.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from olf import log
from olf.deployment import kube_ops
from olf.deployment.charts import TERRAFORM_VARIABLE_KEY, prepare_chart
from olf.deployment.context import stage_namespace
from olf.deployment.context import topology_variables as _topology_variables
from olf.deployment.engine import DeploymentConfig, Toolkit
from olf.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.deployment.local.config import LocalDeploymentConfig
from olf.deployment.retry import RetryPolicy, run_with_retry
from olf.tooling.kubectl import KubeContextUnreachableError

_SEAWEEDFS_RESOURCE_ADDR = "module.seaweedfs.helm_release.seaweedfs"
_SHARED_NAMESPACE_RESOURCE_ADDR = "kubernetes_namespace_v1.shared"
_POLARIS_JOB_PREFIXES = (
    "polaris-bootstrap-",
    "polaris-metastore-bootstrap-",
    "keycloak-credentials-",
    "keycloak-realm-",
)
# Every root's shared-services namespace was named "lakehouse" and addressed
# as `kubernetes_namespace_v1.lakehouse` before the stage-aware rewrite.
_LEGACY_SHARED_NAMESPACE = "lakehouse"


def stage_namespace_resource_addr(stage: str) -> str:
    return f'kubernetes_namespace_v1.stage["{stage}"]'


def platform_var_files(config: LocalDeploymentConfig) -> tuple[str, ...]:
    return (str(config.terraform.var_file),) if config.terraform.var_file is not None else ()


def prepare_charts(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    """Ensure every Terraform-referenced chart archive is cached and digest-verified."""
    config.context.prepare_directories()
    for setting in config.charts.values():
        prepare_chart(
            setting,
            helm=tools.helm,
            paths=config.paths,
            env=env,
            retry_policy=config.terraform.apply_retry,
        )


def topology_variables(config: LocalDeploymentConfig) -> dict[str, str]:
    """The resolved topology, as the platform root's typed Terraform inputs.

    See `olf.deployment.context.topology_variables` - shared with the AWS
    and Azure roots (#114), which took on the same `stages` input.
    """
    return _topology_variables(config.context)


def identity_variables(config: LocalDeploymentConfig) -> dict[str, str]:
    """`spec.identity` as the local root's inputs. An `external` issuer carries
    its claim mapping with it; `keycloak` needs only the choice, because the
    adapter derives the rest from the canonical roles."""
    identity = config.context.topology.identity
    variables = {"identity_issuer": identity.issuer}
    if identity.issuer == "external":
        variables["identity_external"] = json.dumps(
            {
                "issuer_url": identity.issuer_url,
                "role_claim": identity.role_claim,
                "role_mapping": {role: list(values) for role, values in (identity.role_mapping or {}).items()},
                "client_ids": dict(identity.client_ids or {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    if identity.smtp is not None:
        # asdict already nests the reference; None when the endpoint takes no login.
        variables["identity_smtp"] = json.dumps(asdict(identity.smtp), sort_keys=True, separators=(",", ":"))
    return variables


def require_smtp_credentials(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    """The SMTP login is a Secret the operator creates before the deploy: the realm
    Job reads it through `secretKeyRef` and would otherwise sit in
    CreateContainerConfigError until its ten-minute timeout. Only key names are read."""
    smtp = config.context.topology.identity.smtp
    if smtp is None or smtp.credentials_secret_ref is None:
        return
    ref, namespace = smtp.credentials_secret_ref, config.context.shared_namespace
    command = (
        f"kubectl -n {namespace} create secret generic {ref.name} "
        f'--from-literal={ref.username_key}="$SMTP_USERNAME" --from-literal={ref.password_key}="$SMTP_PASSWORD"'
    )
    if not kube_ops.namespace_exists(
        tools.kubectl, namespace, context=config.kube_context, kubeconfig=config.paths.kubeconfig_path, env=env
    ):
        raise DeploymentPreconditionError(
            f"spec.identity.smtp needs Secret '{ref.name}' in namespace '{namespace}', which does not exist yet. "
            "Deploy once without spec.identity.smtp, create the Secret (set SMTP_USERNAME and SMTP_PASSWORD in "
            f"your shell first): {command}; then add smtp to the profile and re-run the deploy."
        )
    result = tools.kubectl.get(
        "secret",
        name=ref.name,
        namespace=namespace,
        output=r'go-template={{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}',
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
        check=False,
    )
    if not result.ok:
        raise DeploymentPreconditionError(
            f"spec.identity.smtp needs Secret '{ref.name}' in namespace '{namespace}' and kubectl could not read "
            f"it: {result.stderr.strip() or 'non-zero status'}. Create it (set SMTP_USERNAME and SMTP_PASSWORD in "
            f"your shell first): {command}"
        )
    missing = {ref.username_key, ref.password_key} - set(result.stdout.split())
    if missing:
        raise DeploymentPreconditionError(
            f"Secret '{ref.name}' in namespace '{namespace}' lacks key(s) {sorted(missing)}; "
            "spec.identity.smtp.credentials_secret_ref names them. Recreate it with the command shape above "
            f"(kubectl delete secret {ref.name} first): {command}"
        )


def platform_apply_variables(config: LocalDeploymentConfig) -> dict[str, str]:
    images = config.images
    return topology_variables(config) | {
        "kube_context": config.kube_context,
        "kubeconfig_path": str(config.paths.kubeconfig_path),
        # The Terraform helm provider (not the `helm` CLI olf's own tooling
        # wraps) writes its own repository cache/config from these two
        # variables. Its computed default lives beneath the Terraform root,
        # which for an installed distribution is the read-only payload -
        # always pass the writable paths olf already resolved.
        "helm_repository_cache_path": str(config.paths.helm_repository_cache),
        "helm_repository_config_path": str(config.paths.helm_repository_config),
        "foundation_state_path": str(config.paths.foundation_state_path),
        "project_code_image_repository": images.project_code_repository,
        "project_code_image_tag": images.project_code_tag,
        "project_code_image_pull_policy": images.project_code_pull_policy,
        "project_code_image_revision": images.project_code_revision,
        "superset_image_repository": images.superset_repository,
        "superset_image_tag": images.superset_tag,
        "superset_image_pull_policy": images.superset_pull_policy,
        "access_base_domain": config.context.topology.access.base_domain,
        "access_issuer": config.context.topology.access.issuer,
    } | identity_variables(config) | cached_chart_variables(config) | {
        TERRAFORM_VARIABLE_KEY[setting.name]: str(setting.package_path) for setting in config.charts.values()
    }


def cached_chart_variables(config: LocalDeploymentConfig) -> dict[str, str]:
    """Chart archives already in the local cache.

    A `helm_release` whose `chart` is a repository name makes the Terraform
    helm provider fetch that repository's index -- on destroy as well as on
    apply -- so a teardown would need the chart repos to be reachable to
    remove a release. Pointing it back at the archive the apply used keeps
    destroy working offline. Archives that are not cached are omitted rather
    than passed as missing paths, which the provider would reject outright.

    This walks every known chart, not only the ones this topology deploys.
    Disabling the last analytics or governance stage is exactly the apply that
    has to destroy that release, and by then the capability gate no longer
    selects its chart -- so an apply, too, is given the cached archives for the
    optional releases that may still be in state.
    """
    return {
        TERRAFORM_VARIABLE_KEY[setting.name]: str(setting.package_path)
        for setting in config.charts.all_values()
        if setting.package_path is not None and Path(setting.package_path).is_file()
    }


def platform_destroy_variables(config: LocalDeploymentConfig) -> dict[str, str]:
    return (
        topology_variables(config)
        | {
            "kube_context": config.kube_context,
            "kubeconfig_path": str(config.paths.kubeconfig_path),
            "helm_repository_cache_path": str(config.paths.helm_repository_cache),
            "helm_repository_config_path": str(config.paths.helm_repository_config),
            "foundation_state_path": str(config.paths.foundation_state_path),
        }
        | cached_chart_variables(config)
    )


# What `terraform output` says when the root has never been applied, or was
# applied before this output existed. Anything else is a real failure.
_NO_APPLIED_OUTPUT_MARKERS = (
    "no outputs found",
    "output variable requested could not be found",
    "not found in state",
)


def applied_stage_names(config: DeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> tuple[str, ...]:
    """Stages the platform root has already applied, empty before any apply.

    Only a positively identified "there is no such output" answer counts as
    empty. Any other `terraform output` failure -- an unreadable state file, a
    lock, a broken toolchain -- propagates: swallowing it would report a
    multi-stage deployment as having no stages, and `require_no_stage_removal`
    would then wave through the very apply it exists to stop.
    """
    try:
        applied = tools.terraform.output_json(config.paths.platform_terraform_dir, "stage_names", env=env)
    except CommandExecutionError as exc:
        detail = f"{exc}".lower()
        if any(marker in detail for marker in _NO_APPLIED_OUTPUT_MARKERS):
            return ()
        raise
    if not isinstance(applied, list):
        return ()
    return tuple(str(name) for name in applied)


def deployed_stages_the_topology_dropped(
    config: DeploymentConfig,
    tools: Toolkit,
    *,
    kube_context: str,
    env: Mapping[str, str],
) -> tuple[str, ...]:
    """Stages that are deployed but no longer in the resolved topology.

    Two independent signals, because either one alone has a blind spot. The
    `stage_names` output is authoritative while Terraform state is intact, and
    names stages whose namespace may already be gone. Namespaces still
    labelled as this deployment's cover the case the output cannot see at all:
    state missing or drifted, which is exactly when `platform_up` reaches for
    the destructive reset.

    A namespace is reported under its stage name where it has one, so the
    caller's message reads the same whichever signal found it.
    """
    enabled = {stage.value for stage in config.context.enabled_stages}
    owned = set(config.context.owned_namespaces)
    dropped = {stage for stage in applied_stage_names(config, tools, env=env) if stage not in enabled}
    for namespace in kube_ops.managed_namespaces(
        tools.kubectl,
        profile_name=config.context.topology.profile_name,
        context=kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    ):
        if namespace in owned:
            continue
        dropped.add(_stage_for_namespace(config, namespace) or namespace)
    return tuple(sorted(dropped))


def _stage_for_namespace(config: DeploymentConfig, namespace: str) -> str | None:
    """The stage a namespace belongs to, from the resolved topology -- which
    carries every stage the resolver knows, disabled ones included."""
    return next(
        (stage.name.value for stage in config.context.topology.stages if stage_namespace(stage.name) == namespace),
        None,
    )


def require_no_stage_removal(
    config: DeploymentConfig,
    tools: Toolkit,
    *,
    kube_context: str,
    env: Mapping[str, str],
) -> None:
    """Refuse an ordinary apply that would drop an already-deployed stage.

    Disabling a stage in the Deployment Profile is a destructive operation:
    the apply that follows deletes that stage's namespace, the services in it,
    and their credentials. Its databases on the shared PostgreSQL server are
    deliberately left behind -- dropping a stage's run and report history as a
    side effect of a profile edit is not something an apply should do -- so
    re-enabling the stage reconnects to that existing state. Terraform's own
    `prevent_destroy` cannot be made conditional, so the opt-in lives here.

    This runs before the drift reset, and deliberately covers what is in the
    cluster as well as what is in state: the reset tears the platform down by
    label, so with state missing it would otherwise delete a dropped stage's
    namespace under a guard that had nothing to read.
    """
    removed = deployed_stages_the_topology_dropped(config, tools, kube_context=kube_context, env=env)
    if not removed or config.context.allow_stage_removal:
        return
    raise DeploymentPreconditionError(
        f"applying would remove already-deployed stage(s) {', '.join(removed)}, deleting their "
        "namespaces, services, and credentials. Their databases stay on the shared PostgreSQL server, so "
        "re-enabling a stage reuses its existing run history. Re-run with --allow-stage-removal to proceed."
    )


def require_no_shared_namespace_replacement(
    config: DeploymentConfig,
    tools: Toolkit,
    *,
    kube_context: str,
    env: Mapping[str, str],
) -> None:
    """Refuse an apply that would silently replace the pre-v0.3 shared namespace.

    Every root's shared-services namespace was named `lakehouse` and addressed
    as `kubernetes_namespace_v1.lakehouse` before the stage-aware rewrite; it
    is now `kubernetes_namespace_v1.shared`, named `config.context.
    shared_namespace` (`olf-system` by default). A Kubernetes namespace's name
    is immutable and Terraform's own state still has the old resource
    address, so an apply against an already-deployed pre-v0.3 cluster plans
    to destroy `lakehouse` - and everything stateful inside it (SeaweedFS,
    PostgreSQL, Polaris) - and create an empty `olf-system` in its place.
    `import_namespace_if_missing_in_state` cannot rescue this on its own: it
    only imports a namespace already named `shared_namespace`, which does not
    exist yet on an unmigrated cluster.
    """
    shared_namespace = config.context.shared_namespace
    if shared_namespace == _LEGACY_SHARED_NAMESPACE or config.context.allow_stage_removal:
        return
    if not kube_ops.namespace_exists(
        tools.kubectl,
        _LEGACY_SHARED_NAMESPACE,
        context=kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    ):
        return
    raise DeploymentPreconditionError(
        f"a '{_LEGACY_SHARED_NAMESPACE}' namespace already exists from a pre-v0.3 deployment, but this "
        f"apply's shared namespace is now '{shared_namespace}'. Applying would destroy '{_LEGACY_SHARED_NAMESPACE}' "
        "- and the SeaweedFS, PostgreSQL, and Polaris state inside it - to create an empty "
        f"'{shared_namespace}' in its place. Migrate the shared services' data manually first, or re-run with "
        "--allow-stage-removal to accept the loss."
    )


def reset_drifted_platform_if_needed(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> bool:
    platform_dir = config.paths.platform_terraform_dir
    if not kube_ops.namespace_exists(
        tools.kubectl,
        config.context.shared_namespace,
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    ):
        return False
    if kube_ops.state_has_resource(tools.terraform, platform_dir, _SEAWEEDFS_RESOURCE_ADDR, env=env):
        return False

    log.warn("local platform resources exist in-cluster but Terraform state is missing core objects.")
    log.warn("resetting the local platform before apply to recover from state drift...")

    from olf.deployment.local import teardown

    teardown.platform_down(config, tools, env=env)

    log.step("Reinitializing Terraform after local platform reset...")
    tools.terraform.init(platform_dir, env=env)
    return True


def _retry_if_logging(description: str, policy: RetryPolicy):  # noqa: ANN202
    def _predicate(exc: CommandExecutionError, attempt: int) -> bool:  # noqa: ARG001
        if attempt < policy.max_attempts:
            delay = policy.delay_for_attempt(attempt)
            log.warn(f"{description} failed on attempt {attempt}/{policy.max_attempts}; retrying in {delay:g}s...")
        else:
            log.error(f"{description} failed after {attempt} attempt(s).")
        return True

    return _predicate


def platform_up(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    platform_dir = config.paths.platform_terraform_dir

    if not config.paths.foundation_state_path.is_file():
        raise DeploymentPreconditionError(
            f"local foundation Terraform state is missing: {config.paths.foundation_state_path}. "
            "Run the foundation phase before applying the local platform."
        )
    try:
        tools.kubectl.require_context_reachable(config.kube_context, kubeconfig=config.paths.kubeconfig_path, env=env)
    except KubeContextUnreachableError as exc:
        raise DeploymentPreconditionError(
            f"Kubernetes context '{config.kube_context}' is not reachable. "
            "Run the foundation phase before applying the local platform."
        ) from exc

    require_smtp_credentials(config, tools, env=env)

    if config.platform_features.analytics_enabled:
        from olf.deployment.local import images

        images.prepare_superset_image(config, tools, env=env)
    else:
        log.step("Skipping Superset image build: no enabled stage has analytics.")

    prepare_charts(config, tools, env=env)

    log.step("Initializing Terraform...")
    tools.terraform.init(platform_dir, env=env)

    require_no_stage_removal(config, tools, kube_context=config.kube_context, env=env)
    require_no_shared_namespace_replacement(config, tools, kube_context=config.kube_context, env=env)
    reset_drifted_platform_if_needed(config, tools, env=env)

    variables = platform_apply_variables(config)
    var_files = platform_var_files(config)

    namespace_resources = {_SHARED_NAMESPACE_RESOURCE_ADDR: config.context.shared_namespace} | {
        stage_namespace_resource_addr(stage.value): config.context.namespace_for(stage)
        for stage in config.context.enabled_stages
    }
    for resource_addr, namespace in namespace_resources.items():
        kube_ops.import_namespace_if_missing_in_state(
            tools.terraform,
            tools.kubectl,
            terraform_dir=platform_dir,
            resource_addr=resource_addr,
            namespace=namespace,
            var_files=var_files,
            variables=variables,
            context=config.kube_context,
            kubeconfig=config.paths.kubeconfig_path,
            env=env,
        )

    def _apply_once() -> None:
        for prefix in _POLARIS_JOB_PREFIXES:
            kube_ops.cleanup_failed_jobs_by_prefix(
                tools.kubectl,
                prefix,
                namespace=config.context.shared_namespace,
                context=config.kube_context,
                kubeconfig=config.paths.kubeconfig_path,
                env=env,
            )
        tools.terraform.apply(platform_dir, var_files=var_files, variables=variables, env=env)

    log.step("Applying Terraform local platform...")
    run_with_retry(
        _apply_once,
        policy=config.terraform.apply_retry,
        retry_if=_retry_if_logging("Terraform apply", config.terraform.apply_retry),
    )

    # Neither Helm nor Terraform waits on a Certificate's status, so a local CA
    # that cannot sign would otherwise surface only when the first route fails.
    log.step("Waiting for the local CA to issue its probe certificate...")
    tools.kubectl.wait(
        "certificate/local-ca-probe",
        for_condition="condition=Ready",
        namespace=config.context.shared_namespace,
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        timeout="180s",
        env=env,
    )

    log.step("Static OpenLakeForge local platform is applied.")
