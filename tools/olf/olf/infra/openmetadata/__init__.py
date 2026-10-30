"""OpenMetadata governance metadata seeding for `olf infra`.

The payloads, upserts and deploy sequence are `openlakeforge_services.governance`.
`_config` resolves the stage's target from the applied contract and the
project's descriptor sources; `_orchestration` binds the library deployer to
them. The Secrets, ingestion Jobs, SSO and role sync stay here as deployment
configuration.
"""

from __future__ import annotations

from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra.openmetadata._config import OpenMetadataConfig
from olf.infra.openmetadata._orchestration import OpenMetadataDeployer

__all__ = ["OpenMetadataClient", "OpenMetadataError", "OpenMetadataConfig", "OpenMetadataDeployer"]
