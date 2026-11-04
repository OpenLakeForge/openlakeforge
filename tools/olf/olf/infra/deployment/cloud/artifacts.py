"""Dynamic cloud (AWS/Azure) artifact deployment: build and push the project, then activate it as a revision.

`deploy_revision` owns the order inside the activation -- catalog namespaces, the optional Superset and OpenMetadata
layers, then the Dagster rollout -- so a failed optional layer leaves Dagster on its previous image. An unmodified
example project deploys the release-published image as it is, with nothing to build or push.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING

from olf.common import log
from olf.infra.deployment.revision_build import activate_deploy_project

if TYPE_CHECKING:
    from olf.infra.deployment.engine import CloudDeploymentProvider


@contextmanager
def _applied_authentication_environment(provider: str, environ: Mapping[str, str]):
    """Expose selected auth state to in-process SDK clients for one deploy."""
    from olf.infra.auth import credential_selection_environment

    selected = credential_selection_environment(provider, environ)
    previous = {name: os.environ.get(name) for name in selected}
    os.environ.update(selected)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def artifacts_deploy(provider: CloudDeploymentProvider) -> None:
    with _applied_authentication_environment(provider.backend.scope, provider.env):
        activate_deploy_project(provider)
        log.step(f"Dynamic OpenLakeForge {provider.backend.scope} artifacts are deployed.")
