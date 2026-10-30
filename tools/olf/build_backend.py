"""PEP 517 wrapper that embeds the OpenLakeForge platform payload."""

from __future__ import annotations

import tomllib
from pathlib import Path

from setuptools import build_meta as _setuptools


def _ensure_embedded_payload() -> None:
    """Generate payload data when building from a checkout.

    An sdist already contains the generated files, so a wheel rebuilt from it
    does not need the repository's Terraform and Helm source tree.
    """
    project_root = Path(__file__).resolve().parent
    repository_root = project_root.parents[1]
    embedded = project_root / "olf" / "_embedded"
    archive = embedded / "platform.tar.gz"
    metadata = embedded / "platform.json"
    if not (repository_root / "infra").is_dir():
        if archive.is_file() and metadata.is_file():
            return
        raise RuntimeError("embedded platform payload is missing and repository assets are unavailable")

    from olf.distribution import build_embedded_payload

    payload = build_embedded_payload(repository_root, archive=archive, metadata_path=metadata)
    package_version = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if payload["package_version"] != package_version:
        raise RuntimeError(
            "payload package version does not match tools/olf/pyproject.toml: "
            f"{payload['package_version']!r} != {package_version!r}"
        )


def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:  # noqa: ANN001
    _ensure_embedded_payload()
    return _setuptools.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory: str, config_settings=None) -> str:  # noqa: ANN001
    _ensure_embedded_payload()
    return _setuptools.build_sdist(sdist_directory, config_settings)


def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings=None) -> str:  # noqa: ANN001
    _ensure_embedded_payload()
    return _setuptools.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def build_editable(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:  # noqa: ANN001
    _ensure_embedded_payload()
    return _setuptools.build_editable(wheel_directory, config_settings, metadata_directory)


def get_requires_for_build_wheel(config_settings=None):  # noqa: ANN001, ANN201
    return _setuptools.get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(config_settings=None):  # noqa: ANN001, ANN201
    return _setuptools.get_requires_for_build_sdist(config_settings)


def get_requires_for_build_editable(config_settings=None):  # noqa: ANN001, ANN201
    return _setuptools.get_requires_for_build_editable(config_settings)
