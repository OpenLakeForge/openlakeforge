"""Deployment settings, read once at startup from the JSON file Terraform mounts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SettingsError(ValueError):
    """The settings file is missing a field or contradicts itself; the API refuses to start."""


@dataclass(frozen=True)
class LogArchiveSetting:
    """Where one stage's archiver writes, and the namespace it archives.

    The namespace is fixed here, never taken from a request: the stage in the
    path is what the permission matrix checks, so it alone decides what is read.
    """

    base_uri: str
    namespace: str


@dataclass(frozen=True)
class Settings:
    """The identity.oidc fields the API validates tokens against (ADR 0014), the enabled stages, and service endpoints.

    `precedence` and `role_mapping` are the contract's, rendered from
    release/identity-roles.yaml and the profile; the API holds no copy of its own.
    Dagster runs one webserver per stage, so its GraphQL URL is keyed by stage,
    as is each stage's log archive; OpenMetadata is shared. A service with no
    endpoint here is answered as not configured rather than refusing the API's
    start. The object-store key comes from the environment (AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY), mounted from a Secret, never from this file.
    """

    issuer_url: str
    audiences: tuple[str, ...]
    role_claim: str
    role_mapping: Mapping[str, tuple[str, ...]]
    precedence: tuple[str, ...]
    stages: tuple[str, ...]
    ca_file: str | None = None
    dagster_graphql_urls: Mapping[str, str] = field(default_factory=dict)
    openmetadata_url: str | None = None
    log_archives: Mapping[str, LogArchiveSetting] = field(default_factory=dict)
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    # Where audit records are stored, s3://<bucket>/<prefix>/. The write-only key comes from the environment
    # (OPENLAKEFORGE_AUDIT_ACCESS_KEY_ID, OPENLAKEFORGE_AUDIT_SECRET_ACCESS_KEY). Without both, every operation
    # that changes state answers 503: it is never run unaudited.
    audit_base_uri: str | None = None
    # Each stage's Superset, and the service user the API signs in as there (ADR 0021, open item 3). The password
    # is read per stage from the environment (superset_password_env), mounted from the Secret its Job mints.
    superset_urls: Mapping[str, str] = field(default_factory=dict)
    superset_username: str | None = None
    # A stage whose catalog shares schemas with other stages prefixes its own (AWS Glue); none locally.
    superset_schema_prefixes: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Settings:
        try:
            settings = cls(
                issuer_url=str(data["issuer_url"]),
                audiences=tuple(str(audience) for audience in data["audiences"]),
                role_claim=str(data["role_claim"]),
                role_mapping={
                    str(role): tuple(str(value) for value in values) for role, values in data["role_mapping"].items()
                },
                precedence=tuple(str(role) for role in data["precedence"]),
                stages=tuple(str(stage) for stage in data.get("stages", ())),
                ca_file=data.get("ca_file"),
                dagster_graphql_urls={
                    str(stage): str(url) for stage, url in (data.get("dagster_graphql_urls") or {}).items()
                },
                openmetadata_url=data.get("openmetadata_url") or None,
                log_archives={
                    str(stage): LogArchiveSetting(str(archive["base_uri"]), str(archive["namespace"]))
                    for stage, archive in (data.get("log_archives") or {}).items()
                },
                s3_endpoint_url=data.get("s3_endpoint_url") or None,
                s3_region=str(data.get("s3_region") or "us-east-1"),
                audit_base_uri=data.get("audit_base_uri") or None,
                superset_urls={str(stage): str(url) for stage, url in (data.get("superset_urls") or {}).items()},
                superset_username=data.get("superset_username") or None,
                superset_schema_prefixes={
                    str(stage): str(prefix) for stage, prefix in (data.get("superset_schema_prefixes") or {}).items()
                },
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise SettingsError(f"platform API settings are incomplete: {exc!r}") from exc
        settings._validate()
        return settings

    @classmethod
    def from_file(cls, path: str | Path) -> Settings:
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def _validate(self) -> None:
        if not self.issuer_url:
            raise SettingsError("issuer_url is empty")
        # PyJWT treats an empty audience list as "accept any audience".
        if not self.audiences or not all(self.audiences):
            raise SettingsError("audiences must list at least one non-empty client id")
        if not self.precedence or len(set(self.precedence)) != len(self.precedence):
            raise SettingsError("precedence must list each canonical role once")
        unknown = sorted(set(self.role_mapping) - set(self.precedence))
        if unknown:
            raise SettingsError(f"role_mapping names roles outside precedence: {', '.join(unknown)}")
        # A Dagster URL for a stage that is not enabled would be reachable by no
        # route, which means the rendering is wrong, not that the stage is off.
        stray = sorted(set(self.dagster_graphql_urls) - set(self.stages))
        if stray:
            raise SettingsError(f"dagster_graphql_urls names stages that are not enabled: {', '.join(stray)}")
        stray = sorted(set(self.log_archives) - set(self.stages))
        if stray:
            raise SettingsError(f"log_archives names stages that are not enabled: {', '.join(stray)}")
        if self.audit_base_uri is not None and not (
            self.audit_base_uri.startswith("s3://")
            and self.audit_base_uri.endswith("/")
            and urlsplit(self.audit_base_uri).netloc
        ):
            raise SettingsError("audit_base_uri must be s3://<bucket>/<prefix>/, ending in /")
        stray = sorted(set(self.superset_urls) - set(self.stages))
        if stray:
            raise SettingsError(f"superset_urls names stages that are not enabled: {', '.join(stray)}")
        if self.superset_urls and not self.superset_username:
            raise SettingsError("superset_urls needs superset_username, the service user the API signs in as")
        for stage, archive in self.log_archives.items():
            if not archive.base_uri.startswith("s3://") or not archive.namespace or "/" in archive.namespace:
                raise SettingsError(f"log_archives[{stage!r}] needs an s3:// base_uri and a namespace")


def superset_password_env(stage: str) -> str:
    """The variable holding `stage`'s Superset service-user password; Terraform names it the same way."""
    return "OPENLAKEFORGE_SUPERSET_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", stage).upper()
