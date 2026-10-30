"""Local static platform Terraform orchestration.

Port of `scripts/local/stack/platform-up.sh`. Preserves the exact drift
recovery, namespace adoption, Polaris job cleanup, and bounded-retry
semantics of the shell script.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path

from olf import log
from olf.deployment import kube_ops
from olf.deployment.charts import TERRAFORM_VARIABLE_KEY, prepare_chart
from olf.deployment.context import stage_namespace
from olf.deployment.context import topology_variables as _topology_variables
from olf.deployment.engine import DeploymentConfig
from olf.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.deployment.local.config import LocalDeploymentConfig
from olf.github_app import GitHubApp, store_app
from olf.tooling.kubectl import KubeContextUnreachableError
from olf.tooling.retry import RetryPolicy, run_with_retry
from olf.tooling.toolkit import Toolkit

_APP_SECRET_KEYS = ("app-id", "app-slug", "private-key")
_SEAWEEDFS_RESOURCE_ADDR = "module.seaweedfs.helm_release.seaweedfs"
_SHARED_NAMESPACE_RESOURCE_ADDR = "kubernetes_namespace_v1.shared"
_POLARIS_JOB_PREFIXES = (
    "polaris-bootstrap-",
    "polaris-metastore-bootstrap-",
    "dex-credentials-",
    "oauth2-proxy-cookie-",
    "trino-credentials-",
    "postgresql-credentials-",
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


def access_variables(config: LocalDeploymentConfig) -> dict[str, str]:
    """`spec.access` as the local root's inputs; absent, the root deploys no ingress."""
    access = config.context.topology.access
    return {} if access is None else {"access_base_domain": access.base_domain, "access_issuer": access.issuer}


def identity_variables(config: LocalDeploymentConfig) -> dict[str, str]:
    """`spec.identity` as the local root's inputs. An `external` issuer carries
    its claim mapping with it; `github` carries its organisation and teams."""
    identity = config.context.topology.identity
    if identity is None:
        return {}
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
    if identity.github is not None:
        variables["identity_github"] = json.dumps(
            {
                "organization": identity.github.organization,
                "teams": {role: list(slugs) for role, slugs in identity.github.teams.items()},
                "oauth_app_secret_ref": asdict(identity.github.oauth_app_secret_ref),
                **({"host": identity.github.host} if identity.github.host else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return variables


def _read_github_secret(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str], output: str):  # noqa: ANN202
    identity = config.context.topology.identity
    assert identity is not None and identity.github is not None
    return tools.kubectl.get(
        "secret",
        name=identity.github.oauth_app_secret_ref.name,
        namespace=config.context.shared_namespace,
        output=output,
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
        check=False,
    )


def read_github_app_secret(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> dict[str, str]:
    """The decoded `app-id`, `app-slug` and `private-key` of a GitHub App Secret; empty when any is absent (a
    hand-made OAuth App, or a Secret from before they were stored) or unreadable."""
    result = _read_github_secret(config, tools, env=env, output="json")
    try:
        data = {key: base64.b64decode(json.loads(result.stdout)["data"][key]).decode() for key in _APP_SECRET_KEYS}
    except (ValueError, KeyError, TypeError):
        return {}
    return data if result.ok else {}


def github_connect_needed(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> bool:
    """Whether `olf deploy` must run the GitHub manifest flow: interactive, enabled, and no Secret yet. A cluster
    that does not exist yet cannot hold one."""
    identity = config.context.topology.identity
    if identity is None or identity.github is None or not config.context.github_connect:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("CI")):
        return False
    if config.cluster.name not in tools.kind.get_clusters(env=env):
        return True
    result = _read_github_secret(config, tools, env=env, output="name")
    return not result.ok and "NotFound" in result.stderr


def require_github_oauth_secret(
    config: LocalDeploymentConfig,
    tools: Toolkit,
    *,
    env: Mapping[str, str],
    create_namespace: Callable[[], None],
    github_app: GitHubApp | None = None,
) -> None:
    """Dex reads the GitHub OAuth App through `secretKeyRef` and would otherwise sit in
    CreateContainerConfigError. Only key names are read. The shared namespace is made first when absent,
    so the operator has somewhere to create the Secret. A missing Secret is written from `github_app`, the App
    the manifest flow created (in memory) before the foundation apply."""
    identity = config.context.topology.identity
    github = identity.github if identity else None
    if github is None:
        return
    ref, namespace = github.oauth_app_secret_ref, config.context.shared_namespace
    command = (
        f"kubectl -n {namespace} create secret generic {ref.name} "
        f'--from-literal={ref.client_id_key}="$GITHUB_CLIENT_ID" '
        f'--from-literal={ref.client_secret_key}="$GITHUB_CLIENT_SECRET"'
    )
    if not kube_ops.namespace_exists(
        tools.kubectl, namespace, context=config.kube_context, kubeconfig=config.paths.kubeconfig_path, env=env
    ):
        create_namespace()

    keys = r'go-template={{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}'
    result = _read_github_secret(config, tools, env=env, output=keys)
    if not result.ok and "NotFound" in result.stderr and github_app is not None:
        store_app(config.context, github_app, tools.kubectl)
        result = _read_github_secret(config, tools, env=env, output=keys)
    if not result.ok:
        raise DeploymentPreconditionError(
            f"spec.identity.github needs the GitHub OAuth App Secret '{ref.name}' in namespace '{namespace}' "
            f"(docs/guides/identity/connect-github.md); kubectl could not read it: "
            f"{result.stderr.strip() or 'non-zero status'}. Run `olf github connect` to create the GitHub App "
            f"and store it, or create a hand-made OAuth App's Secret (set GITHUB_CLIENT_ID and "
            f"GITHUB_CLIENT_SECRET first): {command}. In a terminal, `olf deploy` does this itself unless "
            f"--no-github-connect is set."
        )
    missing = {ref.client_id_key, ref.client_secret_key} - set(result.stdout.split())
    if missing:
        raise DeploymentPreconditionError(
            f"Secret '{ref.name}' in namespace '{namespace}' lacks key(s) {sorted(missing)}; "
            f"spec.identity.github.oauth_app_secret_ref names them. "
            f"Re-run `olf github connect`, or recreate it (kubectl delete secret {ref.name} first): {command}"
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
    } | access_variables(config) | identity_variables(config) | cached_chart_variables(config) | {
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


def require_foundation_state(config: LocalDeploymentConfig) -> None:
    if not config.paths.foundation_state_path.is_file():
        raise DeploymentPreconditionError(
            f"local foundation Terraform state is missing: {config.paths.foundation_state_path}. "
            "Run the foundation phase before applying the local platform."
        )


def platform_state_holds_core(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> bool:
    """Whether the platform state holds the core objects. One that holds the shared namespace is not lost, only
    behind: a previous apply stopped after creating it (the GitHub identity Secret preflight), and resetting would
    delete the namespace and the Secret the operator just added."""
    platform_dir = config.paths.platform_terraform_dir
    return any(
        kube_ops.state_has_resource(tools.terraform, platform_dir, addr, env=env)
        for addr in (_SEAWEEDFS_RESOURCE_ADDR, _SHARED_NAMESPACE_RESOURCE_ADDR)
    )


def prepare_targets(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    """The fallible setup of `apply_targets`, so a rotation runs it before it deletes anything."""
    prepare_charts(config, tools, env=env)
    tools.terraform.init(config.paths.platform_terraform_dir, env=env)
    require_no_stage_removal(config, tools, kube_context=config.kube_context, env=env)


def apply_targets(
    config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str], extra_args: list[str]
) -> None:
    """Apply only what `extra_args` (-target, -replace) name, for rotations, after `prepare_targets`. It skips the
    image build, the drift reset (which can tear down namespaces) and everything else `platform_up` does."""
    if not platform_state_holds_core(config, tools, env=env):
        raise DeploymentPreconditionError("platform state has drifted; run olf deploy first")
    run_with_retry(
        lambda: tools.terraform.apply(
            config.paths.platform_terraform_dir,
            var_files=platform_var_files(config),
            variables=platform_apply_variables(config),
            extra_args=extra_args,
            env=env,
        ),
        policy=config.terraform.apply_retry,
        retry_if=_retry_if_logging("Terraform apply", config.terraform.apply_retry),
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
    if platform_state_holds_core(config, tools, env=env):
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


# The kubeadm Corefile the foundation's CoreDNS starts with: the same as
# modules/access/in-cluster-resolution minus its `rewrite` line. Destroying that
# module removes the whole key, so a platform-only teardown, and a
# platform apply that drops spec.access, put this back.
_STOCK_COREFILE = """.:53 {
    errors
    health {
       lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
       ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf {
       max_concurrent 1000
    }
    cache 30 {
       disable success cluster.local
       disable denial cluster.local
    }
    loop
    reload
    loadbalance
}
"""


def restore_stock_corefile(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    log.step("Restoring the stock CoreDNS Corefile...")
    tools.kubectl.patch(
        "configmap",
        "coredns",
        json.dumps({"data": {"Corefile": _STOCK_COREFILE}}),
        namespace="kube-system",
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    )

def platform_up(
    config: LocalDeploymentConfig,
    tools: Toolkit,
    *,
    env: Mapping[str, str],
    github_app: GitHubApp | None = None,
) -> None:
    """Apply the static platform. `github_app` is the App `olf deploy` created before the foundation apply; it is
    written as the identity Secret once the shared namespace exists."""
    platform_dir = config.paths.platform_terraform_dir

    require_foundation_state(config)
    try:
        tools.kubectl.require_context_reachable(config.kube_context, kubeconfig=config.paths.kubeconfig_path, env=env)
    except KubeContextUnreachableError as exc:
        raise DeploymentPreconditionError(
            f"Kubernetes context '{config.kube_context}' is not reachable. "
            "Run the foundation phase before applying the local platform."
        ) from exc

    superset_tag = None
    if config.platform_features.analytics_enabled:
        from olf.deployment.local import images

        superset_tag = images.prepare_superset_image(config, tools, env=env)

    prepare_charts(config, tools, env=env)

    log.step("Initializing Terraform...")
    tools.terraform.init(platform_dir, env=env)

    require_no_stage_removal(config, tools, kube_context=config.kube_context, env=env)
    require_no_shared_namespace_replacement(config, tools, kube_context=config.kube_context, env=env)
    reset_drifted_platform_if_needed(config, tools, env=env)

    variables = platform_apply_variables(config)
    if superset_tag:
        variables |= {
            "superset_image_repository": config.images.superset_repository,
            "superset_image_tag": superset_tag,
            "superset_image_pull_policy": "Never",
        }
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

    def _create_shared_namespace() -> None:
        # Targeted, so Terraform owns the namespace from the start: importing a hand-made one into a state that
        # holds nothing else fails, and the Secret has to exist before the Dex pod the full apply creates.
        log.step("Creating the shared namespace first, so the identity Secret exists before the issuer starts...")
        tools.terraform.apply(
            platform_dir,
            var_files=var_files,
            variables=variables,
            extra_args=[f"-target={_SHARED_NAMESPACE_RESOURCE_ADDR}"],
            env=env,
        )

    require_github_oauth_secret(
        config, tools, env=env, create_namespace=_create_shared_namespace, github_app=github_app
    )

    def _apply_once() -> None:
        for namespace in config.context.owned_namespaces:
            for release in tools.helm.list_unhealthy_releases(
                namespace=namespace, kube_context=config.kube_context, env=env
            ):
                if release == "openlakeforge-project":  # activation's release, not the platform's
                    continue
                log.step(f"Uninstalling stuck Helm release {namespace}/{release} before the apply...")
                tools.helm.uninstall(release, namespace=namespace, kube_context=config.kube_context, env=env)
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
    # A quickstart (no spec.access) deploys no local CA at all.
    if config.context.topology.access is None:
        # Dropping spec.access destroys the in-cluster resolution module, which
        # deletes the whole Corefile key; CoreDNS would fail on its next reload.
        restore_stock_corefile(config, tools, env=env)
    else:
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
