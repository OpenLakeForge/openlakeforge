"""Stage activation of a verified ProjectRevision (#115)."""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from olf import contracts
from olf.artifact_store import RevisionStore
from olf.deployment import contract_env
from olf.deployment.artifact_steps import (
    activate_runtime_revision,
    deploy_optional_layer_artifacts,
    sync_catalog_namespaces,
)
from olf.deployment.charts import prepare_chart
from olf.deployment.context import DeploymentContext, Provider
from olf.deployment.engine import DeploymentProvider, is_cloud_provider, is_local_provider
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.floe_manifests import generate_aws_manifests, generate_local_manifests
from olf.distribution import distribution_version_at
from olf.profile import StageName
from olf.project_activation import ProjectActivation, ProjectActivationError, commit_active
from olf.project_activation import active as active_activation
from olf.project_activation import publish as publish_activation
from olf.project_revision import ProjectRevisionError, materialize, verify
from olf.provider_contracts import CodeLocation, ProviderContractError, StageContract, parse_provider_contracts
from olf.tooling import docker as docker_tooling

_RELEASE = "openlakeforge-project"
_LOG_ARCHIVE = "openlakeforge-k8s-log-archive"
_PLATFORM_RELEASE = "dagster"
_ACTIVATION_LABEL = "openlakeforge.io/activation-revision"
_RENDERER_ANNOTATION = "openlakeforge.io/floe-renderer"
# Stamped when a rollback restores the previous activation: the renderer that
# produced its manifests is not known here, and stamping the current one would
# make the next attempt mistake the rolled-back release for an up-to-date one
# and skip the regeneration the retry exists to perform.
_RENDERER_UNRECONCILED = "unreconciled"
# Everything `libs.k8s_log_archive` reads, directly or through
# `libs.s3_artifacts.s3_client`, and nothing else. Handing it the code server's
# environment would give a log-processing container the governance JWT and the
# catalog settings it has no use for.
_ARCHIVE_ENV = (
    "OPENLAKEFORGE_LOG_BASE_URI",
    "AWS_ENDPOINT_URL_S3",
    "AWS_ENDPOINT_URL",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)
# Contract-derived runtime settings that deliberately carry no OPENLAKEFORGE_
# prefix because the libraries reading them are not ours: libs.s3_artifacts
# resolves the object store through AWS_ENDPOINT_URL_S3, so a local or Azure
# stage that loses these reaches public S3 instead of its contracted endpoint.
# Terraform passed exactly this set to the code server. Credential *values*
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, OPENLINEAGE_API_KEY) stay out --
# those arrive by Secret reference.
_RUNTIME_ALIASES = (
    "AWS_ALLOW_HTTP",
    "AWS_DEFAULT_REGION",
    "AWS_ENDPOINT_URL_S3",
    "AWS_REGION",
    "AWS_S3_FORCE_PATH_STYLE",
    "OPENLINEAGE_ENDPOINT",
    "OPENLINEAGE_NAMESPACE",
    "OPENLINEAGE_URL",
)
_LOG_ARCHIVE_SCHEDULE = "*/15 * * * *"
# The gRPC port the platform's workspace entries expect on every code-location
# host (modules/orchestration/dagster/main.tf, local.workspace_servers).
_CODE_SERVER_PORT = 3030


class ActivationError(DeploymentPreconditionError):
    """A revision cannot be activated while preserving the current release."""


def _plain(value: Any) -> Any:
    """Convert a parsed contract into plain JSON types, by value.

    Parsed contracts freeze nested objects as `MappingProxyType`, which `json`
    does not recognise as a mapping. Serializing with `default=list` therefore
    reduced every nested binding to a list of its field *names*: renaming a
    bucket, moving an endpoint, or repointing a catalog left this digest
    byte-identical, so the no-op gate skipped a rollout the platform needed and
    code servers kept reading the previous storage binding.

    No `default` fallback: an unexpected type must fail loudly here rather than
    hash to something that silently does not distinguish two bindings.
    """
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _selected_stage(  # noqa: ANN001
    raw_contract: Mapping[str, Any], *, topology, stage: StageName
) -> tuple[Mapping[str, Any], StageContract]:
    """The stage's parsed bindings, refusing a platform activation cannot serve."""
    parsed = parse_provider_contracts(raw_contract, topology)
    if parsed.compatibility_v2 or parsed.schema_version != "3.0.0":
        raise ActivationError(
            "olf project deploy requires a native provider-contract v3 platform; v2 is DEV compatibility only."
        )
    return parsed.deployment, parsed.for_stage(stage)


def _binding_digest(deployment: Mapping[str, Any], selected: StageContract) -> str:
    payload = {
        "deployment": dict(deployment),
        "shared": {
            "ops_storage": dict(selected.shared.values["ops_storage"]),
            "identity": dict(selected.shared.values["identity"]),
        },
        "stage": {
            "name": selected.name.value,
            "namespace": selected.namespace,
            "storage": dict(selected.storage),
            "catalog": dict(selected.catalog),
            "query": dict(selected.query),
            "orchestration": dict(selected.orchestration),
            "activation": dict(selected.activation),
            "endpoints": dict(selected.endpoints),
            "runtime_identity": dict(selected.runtime_identity),
            "reporting": None if selected.reporting is None else dict(selected.reporting),
            "governance": None if selected.governance is None else dict(selected.governance),
        },
    }
    rendered = json.dumps(_plain(payload), sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(rendered).hexdigest()


def stage_code_locations(
    raw_contract: Mapping[str, Any], *, topology, stage: StageName  # noqa: ANN001
) -> tuple[CodeLocation, ...]:
    """The stage's contracted code locations, for callers comparing a release
    against the set it is supposed to be running."""
    return tuple(_selected_stage(raw_contract, topology=topology, stage=stage)[1].code_locations)


def provider_binding_digest(raw_contract: Mapping[str, Any], *, topology, stage: StageName) -> str:  # noqa: ANN001
    """Hash the selected non-secret provider contract binding deterministically."""
    return _binding_digest(*_selected_stage(raw_contract, topology=topology, stage=stage))


def _contract_dir(context: DeploymentContext, environ: Mapping[str, str]) -> Path:
    return Path(environ.get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", context.paths.platform_terraform_dir)).resolve()


def _image_parts(image: str) -> tuple[str, str]:
    repository, digest = image.split("@", 1)
    return repository, digest


def _archive_env(contract_environ: Mapping[str, str], *, namespace: str) -> list[dict[str, str]]:
    env = [{"name": name, "value": contract_environ[name]} for name in _ARCHIVE_ENV if contract_environ.get(name)]
    env.append({"name": "OPENLAKEFORGE_KUBE_NAMESPACE", "value": namespace})
    env.append({"name": "OPENLAKEFORGE_LOG_ARCHIVE_SINCE_SECONDS", "value": "3600"})
    return env


def _log_archive_manifest(
    activation: ProjectActivation,
    *,
    namespace: str,
    env: list[dict[str, str]],
    secrets: list[str],
) -> dict[str, object]:
    """The compute-log archiver, rendered into the activation release.

    It runs `libs.k8s_log_archive`, which only exists inside the project-code
    image, so ADR 0002 puts it here rather than in a platform apply. Riding the
    activation release also keeps it on the digest the stage actually runs
    instead of whatever tag a platform apply last captured.
    """
    labels = {
        "app.kubernetes.io/name": _LOG_ARCHIVE,
        "openlakeforge.io/component": "observability",
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": _LOG_ARCHIVE, "namespace": namespace, "labels": labels},
        "spec": {
            "schedule": _LOG_ARCHIVE_SCHEDULE,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 1,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "metadata": {"labels": labels},
                "spec": {
                    "backoffLimit": 1,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "serviceAccountName": "dagster",
                            "automountServiceAccountToken": True,
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "archive-k8s-logs",
                                    "image": activation.project_code_image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["python", "-m", "libs.k8s_log_archive"],
                                    "env": env,
                                    "envFrom": [{"secretRef": {"name": secret}} for secret in secrets],
                                }
                            ],
                        },
                    },
                },
            },
        },
    }


def _user_values(
    activation: ProjectActivation,
    *,
    contract_environ: Mapping[str, str],
    namespace: str,
    platform_globals: Mapping[str, Any],
    floe_renderer: str,
    code_locations: Sequence[CodeLocation],
    image: str | None = None,
) -> dict[str, object]:
    # `image` overrides the activation's own only on the rollback path, where
    # the target is the release that was running rather than the one the
    # pointer names. Image and modules must come from the same place: a
    # digest that never shipped a module cannot load it.
    repository, digest = _image_parts(image or activation.project_code_image)
    # Annotated rather than inferred: the initializer is all plain string
    # values, but the OPENLINEAGE_API_KEY entry below carries a nested
    # `valueFrom.secretKeyRef`, which a `dict[str, str]` inference rejects.
    env: list[dict[str, Any]] = [
        {"name": "OPENLAKEFORGE_PROJECT_REVISION", "value": activation.project_revision},
        {"name": "OPENLAKEFORGE_FLOE_MANIFEST_REVISION", "value": activation.floe_manifest_revision},
    ]
    for name in sorted(key for key in contract_environ if key.startswith("OPENLAKEFORGE_")):
        if "SECRET" not in name and "ACCESS_KEY" not in name:
            env.append({"name": name, "value": contract_environ[name]})
    for name in _RUNTIME_ALIASES:
        value = contract_environ.get(name)
        if value:
            env.append({"name": name, "value": value})
    # floe_dagster.kubernetes_runner submits its ephemeral Jobs into NAMESPACE
    # and falls back to "lakehouse", the pre-#133 single-stage namespace, when
    # it is unset -- every stage's Floe run would be RBAC-denied against a
    # namespace that no longer exists. Taken from the activation's own stage
    # rather than the environment, which is what makes it true per stage.
    env.append({"name": "NAMESPACE", "value": namespace})
    # The chart puts a list-form `env` straight onto the container, so a
    # secretKeyRef survives; a map would be flattened into a ConfigMap. The
    # ingestion-bot Secret stores its JWT under its own key name, and mounting
    # the whole Secret would expose only that name -- OpenLineage reads
    # OPENLINEAGE_API_KEY, so Terraform mapped the two explicitly and so must
    # this, or authenticated lineage silently stops working.
    jwt_secret = contract_environ.get("OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME", "")
    jwt_key = contract_environ.get("OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY", "")
    if activation.capabilities.get("governance") and jwt_secret and jwt_key:
        env.append(
            {"name": "OPENLINEAGE_API_KEY", "valueFrom": {"secretKeyRef": {"name": jwt_secret, "key": jwt_key}}}
        )
    storage_secret = contract_environ.get("OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME", "")
    secrets = sorted(
        {
            storage_secret,
            contract_environ.get("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", ""),
            jwt_secret,
        }
        - {""}
    )
    return {
        "global": dict(platform_globals),
        "extraManifests": [_log_archive_manifest(
                activation,
                namespace=namespace,
                env=_archive_env(contract_environ, namespace=namespace),
                # `libs.k8s_log_archive` reads the object store and nothing
                # else, and the Terraform CronJob it replaces mounted only
                # this Secret. Handing it catalog and governance credentials
                # would widen what a compromised log container reaches.
                secrets=[storage_secret] if storage_secret else [],
            )],
        "serviceAccount": {"create": False, "name": "dagster"},
        # One deployment per contracted location, never a literal: the platform
        # renders the webserver's workspace from the same list, and
        # dagster-user-deployments names each Service after its deployment, so
        # a second copy of the set here would point the workspace at hosts
        # nothing creates. Every location runs the same revision on the same
        # port -- each is its own Service, so the shared port does not collide.
        "deployments": [
            {
                "name": location.name,
                "image": {"repository": repository, "digest": digest, "pullPolicy": "IfNotPresent"},
                "dagsterApiGrpcArgs": ["--module-name", location.definitions_module],
                "port": _CODE_SERVER_PORT,
                "includeConfigInLaunchedRuns": {"enabled": True},
                "env": env,
                "envSecrets": [{"name": secret} for secret in secrets],
                "deploymentLabels": {
                    "openlakeforge.io/project-revision": activation.project_revision,
                    _ACTIVATION_LABEL: activation.activation_revision,
                },
                "deploymentAnnotations": {
                    "openlakeforge.io/floe-manifest-revision": activation.floe_manifest_revision,
                    _RENDERER_ANNOTATION: floe_renderer,
                },
            }
            for location in code_locations
        ],
    }


_USER_SUBCHART = "charts/dagster-user-deployments/"


def _user_chart(chart: Path, work_root: Path) -> Path:
    """Extract the pinned user-deployments subchart from the cached Dagster chart.

    The subchart travels unpacked inside the parent archive -- `prepare_chart`
    repackages a `helm pull --untar` tree to strip values schemas, so there is
    no nested `dagster-user-deployments-<version>.tgz` to lift out. Helm
    installs a chart directory as readily as an archive, so rebuild the
    subtree rather than re-tarring it.
    """
    destination = work_root / "dagster-user-deployments"
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(chart) as parent:
        for member in parent.getmembers():
            head, separator, relative = member.name.partition(_USER_SUBCHART)
            if not separator or "/" in head.rstrip("/") or not relative:
                continue
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ActivationError(f"pinned Dagster chart {chart} contains an unsafe member {member.name!r}.")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            handle = parent.extractfile(member)
            if handle is None:
                raise ActivationError(f"could not read {member.name} from {chart}.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())
    if not (destination / "Chart.yaml").is_file():
        raise ActivationError(f"pinned Dagster chart {chart} does not contain dagster-user-deployments.")
    return destination


def _generate_floe(
    provider: DeploymentProvider, *, repo_root: Path, contract_environ: Mapping[str, str], env: Mapping[str, str]
) -> str:
    config = provider.config
    context = provider.context
    if context.provider is Provider.AWS:
        generate_aws_manifests(config.floe, provider.tools, repo_root=repo_root, environ=contract_environ, env=env)
        transport = "direct"
    else:
        generate_local_manifests(
            config.floe,
            provider.tools,
            repo_root=repo_root,
            distribution_root=context.paths.distribution_root,
            namespace=context.namespace,
            governance_enabled=context.features.governance_enabled,
            environ=contract_environ,
            env=env,
        )
        transport = "port-forward"
    return activate_runtime_revision(config.floe.runtime_artifact_dir, via=transport)


def _registry_host(repository: str) -> str:
    """The registry a repository reference points at, or "" for Docker Hub."""
    head = repository.split("/", 1)[0]
    return head if ("." in head or ":" in head or head == "localhost") else ""


def _ensure_image(provider: DeploymentProvider, image: str, *, env: Mapping[str, str]) -> None:
    platform = None
    # A revision can legitimately live outside the provider's registry -- GHCR
    # or Docker Hub, which is what the local workflow documents, or another
    # provider's registry after a move. Only the provider's own registry can be
    # authenticated here; for anything else the scoped Docker config holds no
    # credentials at all, so the pull has to use the caller's own.
    pull_env = docker_tooling.ambient_registry_env(env)
    if is_cloud_provider(provider):
        facts = provider.foundation_facts
        repository = _image_parts(image)[0]
        if _registry_host(repository) == _registry_host(facts.project_code_repository):
            # Cloud command environments deliberately use a scoped Docker
            # config, so authenticate it rather than relying on ambient
            # credentials. Logging into a foreign host with an ECR password,
            # or as if it were an ACR, fails before the pull.
            provider.backend.registry_login(provider.tools, facts, repository=repository, env=env)
            pull_env = dict(env)
        platform = provider.config.images.image_platform
    provider.tools.docker.pull(image, platform=platform, env=pull_env)
    if is_local_provider(provider):
        from olf.deployment.local.images import load_digest_image_into_kind

        load_digest_image_into_kind(image, provider.config, provider.tools, env=env)


def read_platform_globals(
    provider: DeploymentProvider, *, kube_context: str, env: Mapping[str, str]
) -> dict[str, Any] | None:
    """The `global` values the platform's Dagster release runs with, or None if absent.

    The activation release installs the user-deployments subchart standalone,
    so it inherits none of the parent's globals and falls back to the
    subchart's own defaults -- notably `postgresqlSecretName:
    dagster-postgresql-secret`, which is not what any stage's Terraform names
    its credentials. Reading the parent release keeps the two in agreement by
    construction instead of by a second copy of the naming rule.
    """
    result = provider.tools.helm.get_values(
        _PLATFORM_RELEASE, namespace=provider.context.namespace, kube_context=kube_context, env=env
    )
    if not result.ok:
        return None
    try:
        values = json.loads(result.stdout or "{}") or {}
    except json.JSONDecodeError:
        return None
    return dict(values.get("global") or {})


def _platform_globals(provider: DeploymentProvider, *, kube_context: str, env: Mapping[str, str]) -> dict[str, Any]:
    values = read_platform_globals(provider, kube_context=kube_context, env=env)
    if values is None:
        raise ActivationError(
            f"the {_PLATFORM_RELEASE!r} release is not installed in {provider.context.namespace}; "
            "run olf platform apply before activating a revision."
        )
    return values


def _kube_context(provider: DeploymentProvider) -> str:
    """The provider's resolved kube context.

    A cloud `DeploymentContext` carries an empty `kube_context` until the
    foundation outputs are read, and only the provider's command environment
    holds the resolved value. Reading the context directly leaves every
    kubectl-backed step of an activation without a cluster to talk to.
    """
    env = provider.env
    return env.get("KUBE_CONTEXT") or provider.context.kube_context


class _RollbackUncapturable(ActivationError):
    """The live release's rollback target could not be read before an upgrade."""


@dataclass(frozen=True)
class _RollbackTarget:
    """One coherent executable state: an image and the modules it shipped."""

    image: str
    locations: tuple[CodeLocation, ...]


def _rollback_target(
    provider: DeploymentProvider, *, namespace: str, env: Mapping[str, str]
) -> _RollbackTarget | None:
    """What the live release is running, or None when there is no release.

    A rollback may only redeploy a state that existed. Taking the modules from
    the live release but the image from `ACTIVE.json` -- or the modules from
    the current contract -- can name a module that digest never shipped, and
    the rollback then fails readiness. Helm restores the uncommitted new
    release when it does, leaving the pointer naming an activation the cluster
    is not running: the silent split brain this path exists to prevent. So
    both halves come from the same observation.

    None means no release was installed, so there is nothing to restore and a
    failed activation is undone by removing what was just installed. That is
    the honest answer even when `ACTIVE.json` still names an activation --
    a pointer is not a running deployment.

    Raises when an installed release cannot be read. This runs before the
    upgrade, so refusing costs nothing.

    The cost of restoring exactly what ran is that Terraform has already
    rendered the webserver's workspace from the current contract, so after a
    rollback that dropped or renamed a location the workspace names a Service
    that is not there. That is the lesser failure and it heals: the pods run,
    the release agrees with the pointer, and the next successful deploy
    re-renders the workspace.
    """
    helm = provider.tools.helm
    kube_context = _kube_context(provider)
    if not helm.status(_RELEASE, namespace=namespace, kube_context=kube_context, env=env).ok:
        return None
    result = helm.get_values(_RELEASE, namespace=namespace, kube_context=kube_context, env=env)
    if not result.ok:
        raise _RollbackUncapturable(
            f"cannot read the current {_RELEASE!r} release's values in {namespace}, so a failed activation "
            "could not be rolled back onto what it is running. Refusing to upgrade."
        )
    try:
        values = json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise _RollbackUncapturable(
            f"the current {_RELEASE!r} release in {namespace} reported unreadable values, so a failed "
            "activation could not be rolled back onto what it is running. Refusing to upgrade."
        ) from exc
    locations: list[CodeLocation] = []
    images: set[str] = set()
    for deployment in (values or {}).get("deployments") or []:
        args = list(deployment.get("dagsterApiGrpcArgs") or [])
        image = deployment.get("image") or {}
        repository, digest = image.get("repository"), image.get("digest")
        if "--module-name" not in args or args.index("--module-name") + 1 >= len(args):
            continue
        if not repository or not digest:
            continue
        locations.append(
            CodeLocation(
                name=str(deployment.get("name")),
                definitions_module=args[args.index("--module-name") + 1],
            )
        )
        images.add(f"{repository}@{digest}")
    if not locations or len(images) != 1:
        # One release runs one project image across however many locations it
        # has. No location, or more than one image, is not a state this can
        # put back coherently.
        raise _RollbackUncapturable(
            f"the current {_RELEASE!r} release in {namespace} does not declare one image and a readable module "
            "per code location, so a failed activation could not be rolled back onto it. Refusing to upgrade."
        )
    return _RollbackTarget(image=images.pop(), locations=tuple(locations))


def _floe_renderer(provider: DeploymentProvider) -> str:
    """What renders this stage's Floe manifests, as one comparable string.

    `FLOE_IMAGE`, `FLOE_VERSION` and `FLOE_RUNTIME` change the generated
    manifests without moving any activation input, so an inputs-only gate
    would keep manifests the previous renderer produced. Recorded on the
    release rather than in the activation record: it describes how the stage
    was rendered, not what was activated, and the record's schema stays put.
    """
    floe = provider.config.floe
    return f"{floe.image}|{floe.version}|{floe.runtime}"


def release_runs_activation(
    provider: DeploymentProvider,
    activation_revision: str,
    *,
    code_locations: Sequence[CodeLocation],
    platform_globals: Mapping[str, Any] | None = None,
    env: Mapping[str, str],
) -> bool:
    """Whether the installed release is healthy *and* actually runs this activation.

    `helm status` alone reports a release that was manually rolled back, or
    restored from stale state, as perfectly healthy. Gating the idempotent
    skip on that would let reapplying the recorded revision decline to repair
    the very drift it should fix. The release renders the activation revision
    onto its deployments, so ask the release what it is running.
    """
    namespace = provider.context.namespace
    kube_context = _kube_context(provider)
    helm = provider.tools.helm
    if not helm.status(_RELEASE, namespace=namespace, kube_context=kube_context, env=env).ok:
        return False
    result = helm.get_values(_RELEASE, namespace=namespace, kube_context=kube_context, env=env)
    if not result.ok:
        return False
    try:
        values = json.loads(result.stdout or "{}") or {}
    except json.JSONDecodeError:
        return False
    # Every contracted location, not merely one of them: the contract can name
    # several, and Terraform renders the webserver's workspace from the same
    # set. A release carrying one correctly labelled deployment while another
    # is missing or stale would otherwise be accepted, and the redeploy that
    # would have repaired it skipped -- leaving the workspace pointing at a
    # Service that does not exist.
    deployed = {
        deployment.get("name"): (deployment.get("deploymentLabels") or {}).get(_ACTIVATION_LABEL)
        for deployment in values.get("deployments") or []
    }
    if set(deployed) != {location.name for location in code_locations}:
        return False
    if any(revision != activation_revision for revision in deployed.values()):
        return False
    # A platform apply can re-bind something the activation renders -- renaming
    # the Dagster credentials Secret, say -- without moving any input the
    # activation digest covers. Skipping on the label alone would leave the
    # deployment pointing at a Secret that no longer exists.
    if platform_globals is not None and dict(values.get("global") or {}) != dict(platform_globals):
        return False
    return all(
        (deployment.get("deploymentAnnotations") or {}).get(_RENDERER_ANNOTATION) == _floe_renderer(provider)
        for deployment in values.get("deployments") or []
    )


def deploy_revision(
    provider: DeploymentProvider, *, revision: str, store: RevisionStore, profile_name: str
) -> ProjectActivation:
    """Verify, render, roll out, then atomically make a revision active."""
    context = provider.context
    env = provider.env
    contract_dir = _contract_dir(context, env)
    kube_context = _kube_context(provider)
    platform_globals = _platform_globals(provider, kube_context=kube_context, env=env)
    raw_contract = contracts.load_provider_contracts(str(contract_dir), environ=env)
    if raw_contract is None:
        raise ActivationError(f"provider contracts are unavailable from {contract_dir}; run olf platform apply first.")
    try:
        deployment, selected = _selected_stage(raw_contract, topology=context.topology, stage=context.stage)
        binding = _binding_digest(deployment, selected)
        code_locations = selected.code_locations
        manifest = verify(
            store,
            revision,
            running_distribution_version=distribution_version_at(context.paths.distribution_root),
        )
    except (ProjectRevisionError, ProviderContractError) as exc:
        raise ActivationError(str(exc)) from exc

    capabilities = {
        "analytics": context.features.analytics_enabled,
        "governance": context.features.governance_enabled,
    }
    previous = active_activation(store, stage=context.stage)
    if (
        previous is not None
        and previous.matches_inputs(
            ProjectActivation(
                deployment_profile=profile_name,
                provider=context.provider.value,
                stage=context.stage,
                project_name=manifest.project_name,
                project_revision=manifest.revision,
                distribution_version=manifest.distribution_version,
                project_code_image=manifest.project_code_image,
                floe_manifest_revision=previous.floe_manifest_revision,
                provider_binding_digest=binding,
                capabilities=capabilities,
            )
        )
        and release_runs_activation(
            provider,
            previous.activation_revision,
            code_locations=code_locations,
            platform_globals=platform_globals,
            env=env,
        )
    ):
        # Reapplying the active revision must not touch the cluster or the ops
        # bucket at all. Deciding this only after Floe has been regenerated
        # would already have performed the work idempotency exists to skip.
        return previous
    # Only once a rollout is actually required: pulling here, and on local
    # reloading into kind, is exactly the cluster mutation the no-op path
    # promises not to perform, and it would make an idempotent redeploy fail
    # on a transient registry outage.
    _ensure_image(provider, manifest.project_code_image, env=env)
    with tempfile.TemporaryDirectory(prefix="project-activation.", dir=context.paths.work_root) as temporary:
        root = materialize(store, manifest, Path(temporary) / "project")
        with contract_env.applied_contract_environment(
            contract_terraform_dir=contract_dir,
            repo_root=root,
            namespace=context.namespace,
            kube_context=kube_context,
            kubeconfig_path=context.paths.kubeconfig_path,
            port_forward_log_prefix=context.paths.port_forward_log_prefix,
            environ=env,
            topology=context.topology,
            stage=context.stage,
        ) as contract_environ:
            sync_catalog_namespaces()
            floe_revision = _generate_floe(provider, repo_root=root, contract_environ=contract_environ, env=env)
            activation = ProjectActivation(
                deployment_profile=profile_name,
                provider=context.provider.value,
                stage=context.stage,
                project_name=manifest.project_name,
                project_revision=manifest.revision,
                distribution_version=manifest.distribution_version,
                project_code_image=manifest.project_code_image,
                floe_manifest_revision=floe_revision,
                provider_binding_digest=binding,
                capabilities=capabilities,
            ).resolved()
            if previous == activation and release_runs_activation(
                provider,
                activation.activation_revision,
                code_locations=code_locations,
                platform_globals=platform_globals,
                env=env,
            ):
                return activation
            # Report import (#130) must run inside this stage-scoped contract
            # environment: it is what makes `deploy_optional_layer_artifacts`
            # below read the revision `materialize()`d above, under `root`,
            # rather than the operator's checkout -- move this call outside
            # the `with` block and it silently imports the checkout again.
            if activation.capabilities["analytics"] or activation.capabilities["governance"]:
                deploy_optional_layer_artifacts(contract_environ)
            config = provider.config
            prepare_chart(
                config.charts["dagster"],
                helm=provider.tools.helm,
                paths=config.paths,
                env=env,
                retry_policy=config.terraform.apply_retry,
            )
            chart = _user_chart(config.charts["dagster"].package_path, Path(temporary))
            values = Path(temporary) / "values.yaml"
            values.write_text(
                yaml.safe_dump(
                    _user_values(
                        activation,
                        contract_environ=contract_environ,
                        namespace=context.namespace,
                        platform_globals=platform_globals,
                        floe_renderer=_floe_renderer(provider),
                        code_locations=code_locations,
                    ),
                    sort_keys=False,
                )
            )
            publish_activation(store, activation)
            # Captured before the upgrade overwrites it: a rollback has to
            # restore the image and the code locations that were deployed
            # together. Re-rendering the previous activation against the
            # current contract pairs the old image with a module that may
            # exist only in the new one, and the failed readiness that
            # follows lets Helm restore the uncommitted new release - leaving
            # ACTIVE.json naming the old activation while the cluster runs
            # the new one.
            rollback = _rollback_target(provider, namespace=context.namespace, env=env) if previous else None
            provider.tools.helm.upgrade_install(
                _RELEASE, chart, namespace=context.namespace, values=values, kube_context=kube_context, env=env
            )
            try:
                commit_active(store, activation)
            except ProjectActivationError:
                # `previous` is redundant with `rollback` -- the target is only
                # read when a pointer exists -- but stating it lets the type
                # checker see that the branch below has an activation.
                if rollback is None or previous is None:
                    # Either nothing was active, or nothing was installed. In
                    # both cases undoing this activation means removing what
                    # it just installed, not restoring a state that never ran.
                    provider.tools.helm.uninstall(
                        _RELEASE, namespace=context.namespace, kube_context=kube_context, env=env
                    )
                else:
                    rollback_values = Path(temporary) / "rollback-values.yaml"
                    rollback_values.write_text(
                        yaml.safe_dump(
                            _user_values(
                                previous,
                                contract_environ=contract_environ,
                                namespace=context.namespace,
                                platform_globals=platform_globals,
                                floe_renderer=_RENDERER_UNRECONCILED,
                                code_locations=rollback.locations,
                                image=rollback.image,
                            ),
                            sort_keys=False,
                        )
                    )
                    provider.tools.helm.upgrade_install(
                        _RELEASE,
                        chart,
                        namespace=context.namespace,
                        values=rollback_values,
                        kube_context=kube_context,
                        env=env,
                    )
                raise
    return activation
