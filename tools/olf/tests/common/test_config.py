"""`common.config` readers resolve from an explicit mapping or `os.environ`."""

from __future__ import annotations

from pathlib import Path

import pytest

from olf.common import config

KEYS = (
    "NAMESPACE",
    "OPENLAKEFORGE_KUBE_NAMESPACE",
    "OPENLAKEFORGE_SHARED_NAMESPACE",
    "OPENLAKEFORGE_REPO_ROOT",
    "OLF_DISTRIBUTION_ROOT",
    "OPENLAKEFORGE_PROJECT_ROOT",
    "X",
)


@pytest.fixture(autouse=True)
def _clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_readers_resolve_from_given_mapping(tmp_path: Path) -> None:
    mapping = {
        "X": "v",
        "NAMESPACE": "ns-a",
        "OPENLAKEFORGE_SHARED_NAMESPACE": "shared-a",
        "OPENLAKEFORGE_REPO_ROOT": str(tmp_path / "repo"),
        "OLF_DISTRIBUTION_ROOT": str(tmp_path / "dist"),
        "OPENLAKEFORGE_PROJECT_ROOT": str(tmp_path / "proj"),
    }
    assert config.env("X", environ=mapping) == "v"
    assert config.env("MISSING", "d", environ=mapping) == "d"
    assert config.namespace(environ=mapping) == "ns-a"
    assert config.shared_namespace(environ=mapping) == "shared-a"
    assert config.repo_root(environ=mapping) == (tmp_path / "repo").resolve()
    assert config.distribution_root(environ=mapping) == (tmp_path / "dist").resolve()
    assert config.project_root(environ=mapping) == (tmp_path / "proj").resolve()
    spec = config.project_spec(environ=mapping)
    assert spec.root == (tmp_path / "proj").resolve()
    assert spec.distribution_root == (tmp_path / "dist").resolve()


def test_shared_namespace_and_roots_fall_back_within_given_mapping(tmp_path: Path) -> None:
    mapping = {"OPENLAKEFORGE_KUBE_NAMESPACE": "kube-ns", "OPENLAKEFORGE_REPO_ROOT": str(tmp_path)}
    assert config.shared_namespace(environ=mapping) == "kube-ns"
    assert config.project_root(environ=mapping) == tmp_path.resolve()
    assert config.distribution_root(environ=mapping) == tmp_path.resolve()


def test_given_mapping_ignores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAMESPACE", "from-os")
    monkeypatch.setenv("X", "from-os")
    assert config.namespace(environ={}) == "olf-dev"
    assert config.env("X", "d", environ={}) == "d"


def test_none_reads_os_environ(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NAMESPACE", "from-os")
    monkeypatch.setenv("X", "from-os")
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(tmp_path))
    assert config.namespace() == config.namespace(environ=None) == "from-os"
    assert config.shared_namespace() == "from-os"
    assert config.env("X") == config.env("X", environ=None) == "from-os"
    assert config.project_spec().root == tmp_path.resolve()
