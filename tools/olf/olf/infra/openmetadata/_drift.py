"""The read-only drift report between a project's descriptors and a stage's OpenMetadata."""

from __future__ import annotations

from collections.abc import Mapping

from openlakeforge_services.drift import DriftItem
from openlakeforge_services.openmetadata import OpenMetadataClient

from olf.common import config
from olf.common.project import ProjectSpec
from olf.infra.openmetadata._config import OpenMetadataConfig
from olf.infra.openmetadata._orchestration import OpenMetadataDeployer


def drift_report(environ: Mapping[str, str], client: OpenMetadataClient) -> list[DriftItem]:
    """What differs between the project in `environ` and the stage its contract environment names. Never writes.

    The deployer is built exactly as `deploy-metadata` builds it, so both read the same descriptors and the same
    stage-scoped catalog root; the client carries the automation token, so no admin password is used.
    """
    project = ProjectSpec(
        root=config.project_root(environ=environ), distribution_root=config.distribution_root(environ=environ)
    )
    cfg = OpenMetadataConfig.from_environment(
        environ,
        base_url=client.base_url,
        admin_email="",
        admin_password="",
        metadata_root=config.env("OPENMETADATA_METADATA_ROOT", str(project.code_root), environ=environ),
        metadata_source_dir=environ.get("OPENMETADATA_METADATA_SOURCE_DIR", ""),
        allow_missing_assets=False,
        catalog_service=(
            config.env("OPENMETADATA_CATALOG_SERVICE", environ=environ)
            or config.env("OPENLAKEFORGE_CATALOG_PROVIDER", environ=environ)
        ),
        catalog_database=(
            config.env("OPENMETADATA_CATALOG_DATABASE", environ=environ)
            or config.env("OPENLAKEFORGE_CATALOG_NAME", environ=environ)
        ),
    )
    return OpenMetadataDeployer(cfg, client).drift()
