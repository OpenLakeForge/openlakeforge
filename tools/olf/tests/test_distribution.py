from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from olf.distribution import (
    DistributionError,
    DistributionManager,
    build_embedded_payload,
    release_to_pep440,
    runtime_layout,
)


def _archive() -> tuple[bytes, dict[str, object]]:
    file_data = b"distribution:\n  version: 0.1.0-alpha.1\n"
    manifest_data = (
        json.dumps(
            {
                "schema_version": 1,
                "distribution_version": "0.1.0-alpha.1",
                "files": [
                    {
                        "path": "release/component-catalog.yaml",
                        "sha256": hashlib.sha256(file_data).hexdigest(),
                        "size": len(file_data),
                        "mode": 0o444,
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            for name, data in (("release/component-catalog.yaml", file_data), ("payload-manifest.json", manifest_data)):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                entry.mode = 0o444
                entry.mtime = 0
                bundle.addfile(entry, io.BytesIO(data))
    archive = raw.getvalue()
    return archive, {
        "distribution_version": "0.1.0-alpha.1",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
    }


def _manager(home: Path) -> DistributionManager:
    archive, metadata = _archive()
    manager = DistributionManager(home=home, metadata=metadata)
    manager._archive_bytes = lambda: archive  # type: ignore[method-assign]
    return manager


def test_release_version_conversion() -> None:
    assert release_to_pep440("0.2.0-alpha.1") == "0.2.0a1"
    with pytest.raises(DistributionError, match="unsupported"):
        release_to_pep440("0.2.0")


def test_payload_archive_is_deterministic_for_the_same_checkout(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    one = build_embedded_payload(root, archive=tmp_path / "one.tar.gz", metadata_path=tmp_path / "one.json")
    two = build_embedded_payload(root, archive=tmp_path / "two.tar.gz", metadata_path=tmp_path / "two.json")

    assert one["sha256"] == two["sha256"]
    assert one["manifest_sha256"] == two["manifest_sha256"]
    with tarfile.open(tmp_path / "one.tar.gz", "r:gz") as archive:
        assert "openlakeforge.yaml" in archive.getnames()


def test_payload_install_verify_and_clean_are_scoped_to_olf_home(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "home")

    root = manager.ensure()

    assert (root / "release/component-catalog.yaml").is_file()
    manager.verify()
    assert manager.clean() is True
    assert not root.exists()


def test_payload_tampering_is_rejected_before_use(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "home")
    root = manager.ensure()
    catalog = root / "release/component-catalog.yaml"
    catalog.chmod(0o644)
    catalog.write_text("changed\n", encoding="utf-8")

    with pytest.raises(DistributionError, match="missing or changed|digest mismatch"):
        manager.verify()


def test_source_layout_preserves_checkout_owned_state(tmp_path: Path) -> None:
    layout = runtime_layout(
        {
            "OLF_DISTRIBUTION_MODE": "source",
            "OLF_DISTRIBUTION_ROOT": str(tmp_path),
            "OPENLAKEFORGE_PROJECT_ROOT": str(tmp_path / "project"),
        }
    )

    assert layout.is_source
    assert layout.distribution_root == tmp_path
    assert layout.project_root == tmp_path / "project"
    assert layout.work_root == tmp_path / ".tmp"


def test_checkout_defaults_to_source_mode_even_when_a_payload_has_been_generated() -> None:
    layout = runtime_layout({})

    assert layout.is_source
    assert layout.distribution_root == Path(__file__).resolve().parents[3]


def test_installed_layout_defaults_the_project_to_the_current_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = tmp_path / "payload"
    (payload / "release").mkdir(parents=True)
    (payload / "release" / "component-catalog.yaml").write_text("distribution:\n  version: 0.1.0-alpha.1\n")
    (tmp_path / "project").mkdir()

    class Manager:
        home = tmp_path / "olf-home"
        version = "0.1.0-alpha.1"
        sha256 = "a" * 64

        def ensure(self) -> Path:
            return payload

    monkeypatch.setattr("olf.distribution.DistributionManager.from_embedded", lambda **_kwargs: Manager())
    monkeypatch.chdir(tmp_path / "project")

    layout = runtime_layout({"OLF_DISTRIBUTION_MODE": "installed"})

    assert layout.project_root == (tmp_path / "project").resolve()


def test_installed_layout_keeps_platform_assets_in_the_payload_when_a_project_is_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = tmp_path / "payload"
    (payload / "release").mkdir(parents=True)
    (payload / "release" / "component-catalog.yaml").write_text("distribution:\n  version: 0.1.0-alpha.1\n")
    project = tmp_path / "project"
    project.mkdir()

    class Manager:
        home = tmp_path / "olf-home"
        version = "0.1.0-alpha.1"
        sha256 = "a" * 64

        def ensure(self) -> Path:
            return payload

    monkeypatch.setattr("olf.distribution.DistributionManager.from_embedded", lambda **_kwargs: Manager())
    monkeypatch.chdir(tmp_path)

    layout = runtime_layout(
        {"OLF_DISTRIBUTION_MODE": "installed", "OPENLAKEFORGE_PROJECT_ROOT": str(project)}
    )

    assert layout.project_root == project.resolve()
    assert layout.distribution_root == payload
    assert layout.catalog_path == payload / "release" / "component-catalog.yaml"
    assert layout.project.root == project.resolve()
    assert layout.project.distribution_root == payload


def test_distribution_version_at_reads_a_distribution_root(tmp_path: Path) -> None:
    from olf.distribution import distribution_version_at

    repo_root = Path(__file__).resolve().parents[3]

    assert distribution_version_at(repo_root) == _catalog_version(repo_root)
    # An external project checkout ships no catalog; activation must read the
    # resolved distribution root instead of falling back to a wrong version.
    assert distribution_version_at(tmp_path) is None


def _catalog_version(root: Path) -> str:
    import re

    catalog = (root / "release" / "component-catalog.yaml").read_text()
    pattern = re.compile(r"^distribution:\s*$.*?^\s+version:\s*['\"]?([^'\"\s#]+)", re.MULTILINE | re.DOTALL)
    return pattern.search(catalog).group(1)


def test_the_payload_ships_every_repo_file_a_terraform_root_reads() -> None:
    import re

    from olf.distribution import _PAYLOAD_FILES, _PAYLOAD_ROOTS

    repo_root = Path(__file__).resolve().parents[3]
    read = re.compile(r'file\("\$\{path\.root\}/\.\./\.\./\.\./\.\./([^"]+)"\)')
    for tf in (repo_root / "infra/terraform/environments").glob("*/*.tf"):
        for path in read.findall(tf.read_text()):
            shipped = path in _PAYLOAD_FILES or path.startswith(_PAYLOAD_ROOTS)
            assert shipped, f"{tf.name} reads {path}, not in the payload"
