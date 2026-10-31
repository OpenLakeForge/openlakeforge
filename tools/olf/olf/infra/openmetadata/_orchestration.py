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

# The local overrides have to be repeated: `deployment_context` applies only
# explicit option values, so a retry without them silently targets the
# default cluster and kubeconfig.
_RERUN_HINT = (
    "then rerun 'olf infra openmetadata deploy-metadata "
    '--cluster-name "${CLUSTER_NAME:-openlakeforge-local}" '
    '--kubeconfig-path "${LOCAL_KUBECONFIG_PATH:-.tmp/kubeconfigs/local.yaml}"\'.'
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
