import json
from pathlib import Path

import pytest
import yaml

from olf.release import _manifest

ROOT = Path(__file__).parents[3]


def _write_catalog(tmp_path: Path, **overrides) -> Path:
    catalog = {
        "apiVersion": "openlakeforge.io/v1alpha1",
        "kind": "ComponentCatalog",
        "metadata": {"name": "openlakeforge"},
        "distribution": {"version": "0.1.0-alpha.1", "release_tag_policy": "immutable-semver"},
        "components": {
            "terraform": {"required_version": ">= 1.6.0", "providers": {"hashicorp/aws": "5.100.0"}},
            "python": {
                "project_code_lock": "images/project-code/requirements.lock",
                "tooling_lock": "tools/olf/uv.lock",
            },
            "images": {
                "project_code_base": "python:3.12-slim@sha256:" + "a" * 64,
            },
            "actions": {"actions/checkout": "a" * 40},
        },
    }
    catalog.update(overrides)
    path = tmp_path / "component-catalog.yaml"
    path.write_text(yaml.safe_dump(catalog))
    return path


def test_load_catalog_reads_real_repo_catalog() -> None:
    catalog = _manifest.load_catalog(ROOT / "release/component-catalog.yaml")
    assert _manifest.catalog_version(catalog) == "0.3.0-alpha.1"


def test_load_catalog_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(_manifest.ReleaseError):
        _manifest.load_catalog(tmp_path / "missing.yaml")


def test_load_catalog_rejects_bad_version(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, distribution={"version": "not-a-version"})
    with pytest.raises(_manifest.ReleaseError):
        _manifest.load_catalog(path)


def test_tag_for_version_and_back() -> None:
    assert _manifest.tag_for_version("0.1.0-alpha.1") == "v0.1.0-alpha.1"
    assert _manifest.version_for_tag("v0.1.0-alpha.1") == "0.1.0-alpha.1"
    assert _manifest.version_for_tag("0.1.0-alpha.1") == "0.1.0-alpha.1"


def test_build_manifest_includes_catalog_and_digests(tmp_path: Path) -> None:
    catalog = _manifest.load_catalog(_write_catalog(tmp_path))
    manifest = _manifest.build_manifest(
        catalog,
        git_sha="deadbeef",
        image_digests={"project-code": "ghcr.io/openlakeforge/openlakeforge/project-code@sha256:" + "b" * 64},
    )
    assert manifest["distribution"]["version"] == "0.1.0-alpha.1"
    assert manifest["distribution"]["tag"] == "v0.1.0-alpha.1"
    assert manifest["distribution"]["git_sha"] == "deadbeef"
    assert manifest["catalog"] == catalog
    assert manifest["resolved_images"]["project-code"].endswith("b" * 64)


def test_render_manifest_json_round_trips(tmp_path: Path) -> None:
    catalog = _manifest.load_catalog(_write_catalog(tmp_path))
    manifest = _manifest.build_manifest(catalog, git_sha="deadbeef")
    rendered = _manifest.render_manifest(manifest, fmt="json")
    assert json.loads(rendered) == manifest


def test_render_manifest_rejects_unknown_format(tmp_path: Path) -> None:
    catalog = _manifest.load_catalog(_write_catalog(tmp_path))
    manifest = _manifest.build_manifest(catalog, git_sha="deadbeef")
    with pytest.raises(_manifest.ReleaseError):
        _manifest.render_manifest(manifest, fmt="toml")


def test_compute_checksums_is_deterministic_and_excludes_itself(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.txt").write_text("world")
    (tmp_path / "checksums.txt").write_text("stale")
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "c.txt").write_text("nested")

    entries = _manifest.compute_checksums(tmp_path)
    paths = [path for path, _digest in entries]
    assert paths == ["a.txt", "b.txt", "nested/c.txt"]

    entries_again = _manifest.compute_checksums(tmp_path)
    assert entries == entries_again


def test_write_checksums_produces_sha256sum_compatible_file(tmp_path: Path) -> None:
    (tmp_path / "asset.txt").write_text("payload")
    out = _manifest.write_checksums(tmp_path)
    content = out.read_text()
    assert content.strip().split("  ") == [
        __import__("hashlib").sha256(b"payload").hexdigest(),
        "asset.txt",
    ]


def test_write_checksums_excludes_its_own_custom_output_path(tmp_path: Path) -> None:
    """A custom --output inside the checksummed directory must never hash itself,
    even across a rerun where the file already exists with stale content.
    """
    (tmp_path / "asset.bin").write_text("v1")
    out = tmp_path / "custom-checksums.txt"
    _manifest.write_checksums(tmp_path, output=out)

    (tmp_path / "asset.bin").write_text("v2-different-content")
    _manifest.write_checksums(tmp_path, output=out)

    content = out.read_text()
    assert "custom-checksums.txt" not in content
    assert "asset.bin" in content
