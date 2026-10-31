"""Deployment settings, read once at startup from the JSON file Terraform mounts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SettingsError(ValueError):
    """The settings file is missing a field or contradicts itself; the API refuses to start."""


@dataclass(frozen=True)
class Settings:
    """The identity.oidc fields the API validates tokens against (ADR 0014), the enabled stages, and service endpoints.

    `precedence` and `role_mapping` are the contract's, rendered from
    release/identity-roles.yaml and the profile; the API holds no copy of its own.
    Dagster runs one webserver per stage, so its GraphQL URL is keyed by stage;
    OpenMetadata is shared. A service with no endpoint here is answered as not
    configured rather than refusing the API's start.
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
