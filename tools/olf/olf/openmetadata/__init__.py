"""OpenMetadata governance metadata seeding over the REST API.

Split by capability into `_config` (deploy configuration and descriptor
source resolution), `_payloads` (domain/data-product payload construction),
`_reconciliation` (REST upsert/resolve primitives), and `_orchestration`
(spec building and `deploy()` sequencing). Behavior is preserved exactly --
only the module boundary moved.
"""

from __future__ import annotations

from olf.clients.openmetadata import OpenMetadataClient, OpenMetadataError
from olf.openmetadata._config import OpenMetadataConfig
from olf.openmetadata._orchestration import OpenMetadataDeployer

__all__ = ["OpenMetadataClient", "OpenMetadataError", "OpenMetadataConfig", "OpenMetadataDeployer"]
