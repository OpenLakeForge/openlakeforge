"""`olf`'s OpenMetadata deployer: the library's, reading the project's own descriptors.

The seeding itself is `openlakeforge_services.governance.MetadataDeployer`.
This subclass resolves which descriptors to deploy from the project layout at
deploy time and reports through `olf`'s log, with retry advice that names the
`olf infra` command.
"""

from __future__ import annotations

from pathlib import Path

from openlakeforge_services.governance import MetadataDeployer
from openlakeforge_services.openmetadata import OpenMetadataClient

from olf.common import log
from olf.infra.openmetadata._config import OpenMetadataConfig, resolve_metadata_descriptor_paths

# Stage-neutral: the rerun must repeat the deployment and provider options the
# original run used (local, AWS or Azure), which this tool cannot know here.
_RERUN_HINT = (
    "then rerun the 'olf infra openmetadata deploy-metadata' command with the same "
    "deployment and provider options as the original run."
)


class OpenMetadataDeployer(MetadataDeployer):
    config: OpenMetadataConfig

    def __init__(self, config: OpenMetadataConfig, client: OpenMetadataClient):
        super().__init__(config, client, rerun_hint=_RERUN_HINT, progress=log)

    def domain_files(self) -> list[Path]:
        descriptor_paths, _source_label, _require_directory_match = resolve_metadata_descriptor_paths(
            self.config.metadata_root, self.config.metadata_source_dir
        )
        return descriptor_paths
