"""Dynamic cloud (AWS/Azure) artifact deployment: build and push the project, then activate it as a revision.

`deploy_revision` owns the order inside the activation -- catalog namespaces, the optional Superset and OpenMetadata
layers, then the Dagster rollout -- so a failed optional layer leaves Dagster on its previous image. An unmodified
example project deploys the release-published image as it is, with nothing to build or push.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from olf.common import log
from olf.infra.deployment.revision_build import activate_deploy_project

if TYPE_CHECKING:
    from olf.infra.deployment.engine import CloudDeploymentProvider


def artifacts_deploy(provider: CloudDeploymentProvider) -> None:
    activate_deploy_project(provider)
    log.step(f"Dynamic OpenLakeForge {provider.backend.scope} artifacts are deployed.")
