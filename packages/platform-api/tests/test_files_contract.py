"""The files explorer's settings and the S3 client the API builds for it."""

from __future__ import annotations

from typing import Any

import pytest

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings, SettingsError

BUCKETS = {"dev": ["olf-dev-bronze", "olf-dev-silver"], "uat": ["olf-uat-bronze"], "prod": ["olf-prod-bronze"]}


def _settings(settings: Settings, **extra: Any) -> Settings:
    raw = {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": ["dev", "uat", "prod"],
        "file_buckets": BUCKETS,
        "ops_bucket": "olf-ops",
    }
    return Settings.from_mapping({**raw, **extra})


def test_settings_parse_the_files_fields(settings: Settings) -> None:
    parsed = _settings(settings, files_max_upload_bytes=5)

    assert parsed.file_buckets == {
        "dev": ("olf-dev-bronze", "olf-dev-silver"),
        "uat": ("olf-uat-bronze",),
        "prod": ("olf-prod-bronze",),
    }
    assert parsed.ops_bucket == "olf-ops"
    assert parsed.files_max_upload_bytes == 5


def test_settings_default_to_no_files_and_a_gibibyte_cap(settings: Settings) -> None:
    assert settings.file_buckets == {}
    assert settings.ops_bucket is None
    assert settings.files_max_upload_bytes == 1_073_741_824


def test_file_buckets_for_a_stage_that_is_not_enabled_are_refused(settings: Settings) -> None:
    with pytest.raises(SettingsError, match="file_buckets names stages that are not enabled: qa"):
        _settings(settings, file_buckets={"qa": ["b"]})


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_upload_cap_is_refused(settings: Settings, value: int) -> None:
    with pytest.raises(SettingsError, match="files_max_upload_bytes must be positive"):
        _settings(settings, files_max_upload_bytes=value)


def test_the_store_is_built_only_from_the_files_key(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "log-reader")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "log-secret")
    monkeypatch.delenv("OPENLAKEFORGE_FILES_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("OPENLAKEFORGE_FILES_SECRET_ACCESS_KEY", raising=False)

    assert create_app(_settings(settings)).state.files_store is None

    monkeypatch.setenv("OPENLAKEFORGE_FILES_ACCESS_KEY_ID", "files")
    monkeypatch.setenv("OPENLAKEFORGE_FILES_SECRET_ACCESS_KEY", "files-secret")

    assert create_app(_settings(settings)).state.files_store is not None
    assert create_app(settings).state.files_store is None  # no file_buckets configured
