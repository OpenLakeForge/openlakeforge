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
class QueryEndpointSetting:
    """One stage's Trino: the statement endpoint and the catalog the API names in every query.

    The catalog is fixed here, never taken from a request: the stage in the path is what the permission matrix
    checks, so it alone decides which catalog a person's SQL reaches.
    """

    statement_url: str
    catalog: str


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
    # Where the issuer's keys are fetched from, in place of OIDC discovery at issuer_url. `iss` is still compared
    # with issuer_url as a string, so the issuer's public URL need not be reachable from the pod.
    jwks_url: str | None = None
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
    # The portal's origin (scheme://host[:port]), the only one the CSRF guard lets write. Unset, every request with an
    # Origin header is refused on a POST, PUT, PATCH or DELETE; a client that sends none (the CLI) is unaffected.
    public_origin: str | None = None
    # True only where Dex-backed identity runs (ADR 0019). False refuses every per-person query.
    person_query: bool = False
    # Each stage's Trino. A stage with no entry offers no query, and with none at all capabilities list no stage.
    query_endpoints: Mapping[str, QueryEndpointSetting] = field(default_factory=dict)
    # Bounds on a person's SQL. A response never carries more than `query_page_rows` rows; an export stops, and is
    # refused with 413, past `query_export_max_rows`.
    # `query_timeout_seconds` bounds each call to Trino, and `query_wait_seconds` how long one request keeps
    # following a query that has not produced rows yet.
    query_page_rows: int = 1000
    query_export_max_rows: int = 100_000
    query_timeout_seconds: float = 30.0
    query_wait_seconds: float = 5.0

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
                jwks_url=data.get("jwks_url") or None,
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
                public_origin=data.get("public_origin") or None,
                person_query=data.get("person_query") is True,
                query_endpoints={
                    str(stage): QueryEndpointSetting(str(endpoint["statement_url"]), str(endpoint["catalog"]))
                    for stage, endpoint in (data.get("query_endpoints") or {}).items()
                },
                query_page_rows=int(data.get("query_page_rows", 1000)),
                query_export_max_rows=int(data.get("query_export_max_rows", 100_000)),
                query_timeout_seconds=float(data.get("query_timeout_seconds", 30.0)),
                query_wait_seconds=float(data.get("query_wait_seconds", 5.0)),
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
        if self.public_origin is not None:
            parts = urlsplit(self.public_origin)
            if (
                parts.scheme not in ("http", "https")
                or not parts.netloc
                or self.public_origin != f"{parts.scheme}://{parts.netloc}"
            ):
                raise SettingsError("public_origin must be scheme://host[:port] with no path or trailing slash")
        stray = sorted(set(self.query_endpoints) - set(self.stages))
        if stray:
            raise SettingsError(f"query_endpoints names stages that are not enabled: {', '.join(stray)}")
        for stage, endpoint in self.query_endpoints.items():
            if urlsplit(endpoint.statement_url).scheme not in ("http", "https") or not endpoint.catalog:
                raise SettingsError(f"query_endpoints[{stage!r}] needs an http(s) statement_url and a catalog")
        if min(self.query_page_rows, self.query_export_max_rows) < 1 or self.query_timeout_seconds <= 0:
            raise SettingsError("query_page_rows, query_export_max_rows and query_timeout_seconds must be positive")
        if self.query_wait_seconds < 0:
            raise SettingsError("query_wait_seconds cannot be negative")
        for stage, archive in self.log_archives.items():
            if not archive.base_uri.startswith("s3://") or not archive.namespace or "/" in archive.namespace:
                raise SettingsError(f"log_archives[{stage!r}] needs an s3:// base_uri and a namespace")


# The key that signs query ids and cursors, mounted from a Secret like the audit key and never read from the settings
# file. Unset or under 32 characters, query execution answers 503: an unsigned id would let one person name another's.
QUERY_SIGNING_KEY_ENV = "OPENLAKEFORGE_QUERY_SIGNING_KEY"


def superset_password_env(stage: str) -> str:
    """The variable holding `stage`'s Superset service-user password; Terraform names it the same way."""
    return "OPENLAKEFORGE_SUPERSET_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", stage).upper()
