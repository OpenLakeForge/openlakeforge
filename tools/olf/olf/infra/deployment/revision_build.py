"""Build and publish the ProjectRevision for a project (#558).

Shared by `olf infra project build` and the deploy phases, so neither depends on the CLI layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from olf.common.project import ProjectSpec

if TYPE_CHECKING:
    from olf.common.deployment.context import DeploymentContext
    from olf.infra.artifact_store import RevisionStore
    from olf.infra.deployment.engine import DeploymentProvider
    from olf.infra.project_activation import ProjectActivation
    from olf.infra.project_revision import ProjectRevisionManifest

LOCAL_TRANSPORT = "port-forward"


@contextmanager
def revision_store(*, via: str, output: str, environ: Mapping[str, str] | None = None) -> Iterator[RevisionStore]:
    from olf.infra.artifact_store import (
        FilesystemRevisionStore,
        S3RevisionStore,
        artifact_bucket,
        artifact_storage_client,
    )

    if output:
        yield FilesystemRevisionStore(Path(output))
        return
    bucket = artifact_bucket(environ=environ)
    with artifact_storage_client(via, bucket, environ=environ) as client:
        yield S3RevisionStore(client, bucket)


def artifact_transport(provider: object) -> str:
    """The `--via` mode this provider's backend prescribes.

    AWS deliberately blanks the S3 service fields in its contract because its
    store requires direct S3, so a blanket `port-forward` default fails while
    resolving an empty service port instead of publishing the revision.
    """
    backend = getattr(provider, "backend", None)
    return backend.artifact_transport() if backend is not None else LOCAL_TRANSPORT


@contextmanager
def provider_revision_store(
    provider: DeploymentProvider, context: DeploymentContext, project_root: Path, *, via: str = "", output: str = ""
) -> Iterator[RevisionStore]:
    """Open the revision store under the platform contract of `context`'s stage."""
    from olf.common.deployment.errors import DeploymentError
    from olf.common.provider_contracts import ProviderContractError
    from olf.infra.artifact_store import ArtifactStoreError
    from olf.infra.deployment.contract_env import applied_contract_environment
    from olf.infra.k8s import KubectlError

    contract_dir = Path(
        provider.env.get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", context.paths.platform_terraform_dir)
    ).resolve()
    with ExitStack() as stack:
        try:
            # Building against an unreachable platform is the same outcome as
            # building with no bucket resolved, and `olf infra project build` already
            # fails closed on that. Reaching the caller as a raw kubectl or
            # Terraform error would instead surface a traceback. Only opening
            # the store is wrapped: a failure in the body belongs to whatever
            # the caller is doing with it, and reporting that as an artifact
            # store problem would misname it.
            contract = stack.enter_context(
                applied_contract_environment(
                    contract_terraform_dir=contract_dir,
                    repo_root=project_root,
                    namespace=context.namespace,
                    kube_context=provider.env.get("KUBE_CONTEXT") or context.kube_context,
                    kubeconfig_path=context.paths.kubeconfig_path,
                    port_forward_log_prefix=context.paths.port_forward_log_prefix,
                    environ=provider.env,
                    topology=context.topology,
                    stage=context.stage,
                )
            )
            store = stack.enter_context(
                revision_store(via=via or artifact_transport(provider), output=output, environ=contract)
            )
        except (DeploymentError, KubectlError, ProviderContractError) as exc:
            raise ArtifactStoreError(
                f"could not resolve the artifact store from the {context.provider.value} platform contract: {exc}"
            ) from exc
        yield store


def publish_project_revision(
    project: str, *, image: str, open_store: Callable[[Path], AbstractContextManager[RevisionStore]]
) -> ProjectRevisionManifest:
    """Build the ProjectRevision for `project` around `image` and publish it into the store `open_store` opens.

    Raises `ProjectRevisionError` or `ArtifactStoreError` rather than exiting.
    """
    from olf.common.commands._project import writable_project_layout
    from olf.infra.project_revision import build_project_revision, publish

    layout = writable_project_layout(project)
    spec = ProjectSpec(root=layout.project_root, distribution_root=layout.distribution_root)
    manifest = build_project_revision(spec, image=image, distribution_version=layout.distribution_version)
    with open_store(layout.project_root) as store:
        publish(store, manifest, spec)
    return manifest


def build_stage_revision(
    provider: DeploymentProvider, context: DeploymentContext, project: str, *, push: bool = True
) -> ProjectRevisionManifest:
    """Build the project-code image through `provider`, then publish a revision pinned to its digest.

    `push=False` is the local provider's kind-only loop; its revision names a local image and activates on that
    cluster alone.
    """
    from olf.infra.project_revision import resolve_image_digest

    image = resolve_image_digest(provider.build_project_image(push=push))
    return publish_project_revision(
        project, image=image, open_store=lambda root: provider_revision_store(provider, context, root)
    )


def activate_stage_project(
    provider: DeploymentProvider,
    project: str,
    *,
    profile_name: str,
    pull_secret: str | None = None,
    push: bool = True,
) -> ProjectActivation:
    """Build `project`'s revision through `provider` and make it the stage's active one.

    The one path from project code to a running stage: `deploy_revision` owns
    the order of the optional layers and the Dagster rollout, so a caller needs
    no step of its own for either.
    """
    from olf.common.commands._project import writable_project_layout
    from olf.infra.deployment.activation import deploy_revision

    context = provider.context
    manifest = build_stage_revision(provider, context, project, push=push)
    # The same root publish_project_revision opened the store at, not the raw `project` argument.
    with provider_revision_store(provider, context, writable_project_layout(project).project_root) as store:
        return deploy_revision(
            provider, revision=manifest.revision, store=store, profile_name=profile_name, pull_secret=pull_secret
        )


def activate_deploy_project(provider: DeploymentProvider, *, push: bool = True) -> ProjectActivation:
    """`activate_stage_project` for the project at the context's repo root, as the artifacts phase runs it.

    There is no profile file on this path, so the profile name and the image pull Secret come from the topology
    the context already resolved (`legacy` for the `--profile` shorthand).
    """
    from olf.common.profile import resolve_declared_project

    topology = provider.context.topology
    pull_secret = resolve_declared_project(topology.project, topology.provider, provider.env).pull_secret_name
    return activate_stage_project(
        provider,
        str(provider.context.paths.repo_root),
        profile_name=topology.profile_name,
        pull_secret=pull_secret,
        push=push,
    )
