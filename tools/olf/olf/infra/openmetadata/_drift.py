"""The read-only drift report between a project's descriptors and a stage's OpenMetadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from openlakeforge_services.drift import DriftItem
from openlakeforge_services.openmetadata import OpenMetadataClient

from olf.common import config
from olf.common.project import ProjectSpec
from olf.infra.openmetadata._config import OpenMetadataConfig
from olf.infra.openmetadata._orchestration import OpenMetadataDeployer

_LOGIN_PATH = "/api/v1/users/login"


@dataclass
class _SpyClient(OpenMetadataClient):
    """An OpenMetadataClient that records every request and the answer it got."""

    exchanges: list[tuple[str, str, Any]] = field(default_factory=list)

    def request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        response = super().request(method, path, **kwargs)
        self.exchanges.append((method, path, response))
        return response


def _deployer(
    environ: Mapping[str, str],
    client: OpenMetadataClient,
    *,
    revision: str | None = None,
    admin_email: str = "",
    admin_password: str = "",
    project: ProjectSpec | None = None,
) -> OpenMetadataDeployer:
    """The deployer `deploy-metadata` builds, from the project and contract environment in `environ`.

    A client without a token logs in as the admin (SSO off), so the caller passes the credentials then.
    """
    project = project or ProjectSpec(
        root=config.project_root(environ=environ), distribution_root=config.distribution_root(environ=environ)
    )
    cfg = OpenMetadataConfig.from_environment(
        environ,
        base_url=client.base_url,
        admin_email=admin_email,
        admin_password=admin_password,
        metadata_root=config.env("OPENMETADATA_METADATA_ROOT", str(project.code_root), environ=environ),
        metadata_source_dir=environ.get("OPENMETADATA_METADATA_SOURCE_DIR", ""),
        allow_missing_assets=True,
        catalog_service=(
            config.env("OPENMETADATA_CATALOG_SERVICE", environ=environ)
            or config.env("OPENLAKEFORGE_CATALOG_PROVIDER", environ=environ)
        ),
        catalog_database=(
            config.env("OPENMETADATA_CATALOG_DATABASE", environ=environ)
            or config.env("OPENLAKEFORGE_CATALOG_NAME", environ=environ)
        ),
    )
    return OpenMetadataDeployer(cfg, client, revision=revision)


def drift_report(
    environ: Mapping[str, str],
    client: OpenMetadataClient,
    *,
    admin_email: str = "",
    admin_password: str = "",
    project: ProjectSpec | None = None,
) -> list[DriftItem]:
    """What differs between the project in `environ` and the stage its contract environment names. Never writes."""
    return _deployer(
        environ, client, admin_email=admin_email, admin_password=admin_password, project=project
    ).drift()


def redeploy_writes(
    environ: Mapping[str, str],
    client: OpenMetadataClient,
    *,
    admin_email: str = "",
    admin_password: str = "",
    project: ProjectSpec | None = None,
) -> list[str]:
    """Deploy the project again and return the write requests it made: none when the stage already matches Git.

    The marker revision is the one the previous deploy of this checkout stamped, so a clean stage needs no write.
    """
    spy = _SpyClient(client.base_url, token=client.token, session=client.session)
    _deployer(
        environ, spy, admin_email=admin_email, admin_password=admin_password, project=project
    ).deploy()
    # The login POST creates a session, not metadata.
    return [f"{method} {path}" for method, path, _ in spy.exchanges if method != "GET" and path != _LOGIN_PATH]
