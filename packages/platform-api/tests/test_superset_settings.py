"""Superset settings: each enabled stage's URL, the service user, and the variable its password is read from."""

from __future__ import annotations

from typing import Any

import pytest

from openlakeforge_platform_api.settings import Settings, SettingsError, superset_password_env


def _settings(settings: Settings, **extra: Any) -> Settings:
    raw = {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": list(settings.stages),
    }
    return Settings.from_mapping({**raw, **extra})


def test_superset_settings_name_only_enabled_stages_and_need_a_username(settings: Settings) -> None:
    configured = _settings(settings, superset_urls={"dev": "http://superset.olf-dev:8088"}, superset_username="u")
    assert configured.superset_urls == {"dev": "http://superset.olf-dev:8088"}
    with pytest.raises(SettingsError, match="not enabled: qa"):
        _settings(settings, superset_urls={"qa": "http://x"}, superset_username="u")
    with pytest.raises(SettingsError, match="needs superset_username"):
        _settings(settings, superset_urls={"dev": "http://x"})


@pytest.mark.parametrize(("stage", "name"), [("dev", "DEV"), ("prod-eu", "PROD_EU"), ("uat.2", "UAT_2")])
def test_a_stages_superset_password_variable_is_named_as_terraform_names_it(stage: str, name: str) -> None:
    assert superset_password_env(stage) == f"OPENLAKEFORGE_SUPERSET_PASSWORD_{name}"
