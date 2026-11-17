"""The evaluator's own settings JSON. It shares nothing with the API's `Settings`: it validates no tokens, so it has
no issuer, and it must start with only the fields it uses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openlakeforge_platform_api.settings import SettingsError


@dataclass(frozen=True)
class ReceiverSetting:
    """A webhook receiver. The URL is the credential, so the settings name the environment variable that holds it."""

    name: str
    url_env: str
    # Empty means every stage.
    stages: tuple[str, ...] = ()
    # Whether platform conditions (ADR 0023, decision 11) go to this receiver.
    platform: bool = True

    def routes(self, stage: str) -> bool:
        return not self.stages or stage in self.stages


@dataclass(frozen=True)
class LinkSettings:
    # `http` when `spec.access.tls` is `none`, `https` otherwise (ADR 0023, decision 5); the evaluator never guesses.
    dagster_scheme: str = "https"
    # The Dagster host with a `{stage}` placeholder, e.g. `dagster-{stage}.example.com`.
    dagster_host_template: str | None = None
    runbook_base_url: str | None = None


@dataclass(frozen=True)
class EvaluatorSettings:
    stages: tuple[str, ...]
    dagster_graphql_urls: Mapping[str, str]
    # s3://<ops bucket>/<prefix>/, e.g. s3://ops/alerts/
    state_base_uri: str
    # Each stage's activation prefix, s3://<ops bucket>/activations/<stage>/, where its quality manifests are written
    # under run-artifacts/quality/. A stage without an entry raises no quality alerts.
    quality_manifests: Mapping[str, str] = field(default_factory=dict)
    receivers: tuple[ReceiverSetting, ...] = ()
    links: LinkSettings = field(default_factory=LinkSettings)
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    ca_file: str | None = None
    # The platform API's base URL and the file holding the projected ServiceAccount token the evaluator presents to it
    # (ADR 0023, decision 11). Both or neither: without them no service signal is read.
    platform_api_url: str | None = None
    platform_api_token_file: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvaluatorSettings:
        try:
            links = data.get("links") or {}
            settings = cls(
                stages=tuple(str(stage) for stage in data["stages"]),
                dagster_graphql_urls={str(s): str(url) for s, url in data["dagster_graphql_urls"].items()},
                state_base_uri=str(data["state_base_uri"]),
                quality_manifests={str(s): str(uri) for s, uri in (data.get("quality_manifests") or {}).items()},
                receivers=tuple(
                    ReceiverSetting(
                        str(item["name"]),
                        str(item["url_env"]),
                        tuple(str(stage) for stage in item.get("stages") or ()),
                        bool(item.get("platform", True)),
                    )
                    for item in data.get("receivers") or ()
                ),
                links=LinkSettings(
                    dagster_scheme=str(links.get("dagster_scheme", "https")),
                    dagster_host_template=links.get("dagster_host_template") or None,
                    runbook_base_url=links.get("runbook_base_url") or None,
                ),
                s3_endpoint_url=data.get("s3_endpoint_url") or None,
                s3_region=str(data.get("s3_region") or "us-east-1"),
                ca_file=data.get("ca_file") or None,
                platform_api_url=data.get("platform_api_url") or None,
                platform_api_token_file=data.get("platform_api_token_file") or None,
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise SettingsError(f"alert evaluator settings are incomplete: {exc!r}") from exc
        settings._validate()
        return settings

    @classmethod
    def from_file(cls, path: str | Path) -> EvaluatorSettings:
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def _validate(self) -> None:
        if not self.stages or not all(self.stages) or len(set(self.stages)) != len(self.stages):
            raise SettingsError("stages must list each enabled stage once")
        # Every enabled stage sends one Dagster read signal per evaluation; a stage with no URL could never send one.
        if set(self.dagster_graphql_urls) != set(self.stages):
            raise SettingsError("dagster_graphql_urls must name exactly the enabled stages")
        parts = urlsplit(self.state_base_uri)
        if parts.scheme != "s3" or not parts.netloc or not self.state_base_uri.endswith("/"):
            raise SettingsError("state_base_uri must be s3://<bucket>/<prefix>/, ending in /")
        stray_quality = sorted(set(self.quality_manifests) - set(self.stages))
        if stray_quality:
            raise SettingsError(f"quality_manifests names stages that are not enabled: {', '.join(stray_quality)}")
        for stage, uri in self.quality_manifests.items():
            parts = urlsplit(uri)
            if parts.scheme != "s3" or not parts.netloc or not uri.endswith("/"):
                raise SettingsError(f"quality_manifests[{stage!r}] must be s3://<bucket>/<prefix>/, ending in /")
        names = [receiver.name for receiver in self.receivers]
        if not all(names) or len(set(names)) != len(names):
            raise SettingsError("receivers must have distinct, non-empty names")
        for receiver in self.receivers:
            if not receiver.url_env:
                raise SettingsError(f"receiver {receiver.name!r} needs url_env, the variable holding its URL")
            stray = sorted(set(receiver.stages) - set(self.stages))
            if stray:
                raise SettingsError(f"receiver {receiver.name!r} names stages that are not enabled: {', '.join(stray)}")
        if (self.platform_api_url is None) != (self.platform_api_token_file is None):
            raise SettingsError("platform_api_url and platform_api_token_file must be set together")
        if self.platform_api_url is not None and urlsplit(self.platform_api_url).scheme not in ("http", "https"):
            raise SettingsError("platform_api_url must be an http(s) base URL")
        if self.links.dagster_scheme not in ("http", "https"):
            raise SettingsError("links.dagster_scheme must be http or https")
        template = self.links.dagster_host_template
        if template is not None and "{stage}" not in template:
            raise SettingsError("links.dagster_host_template must contain {stage}")
        runbook = self.links.runbook_base_url
        if runbook is not None and urlsplit(runbook).scheme not in ("http", "https"):
            raise SettingsError("links.runbook_base_url must be an http(s) URL")
