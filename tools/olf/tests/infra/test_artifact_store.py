from __future__ import annotations

import pytest

from olf.infra import artifact_store


def test_filesystem_revision_store_round_trips_content(tmp_path) -> None:  # noqa: ANN001
    store = artifact_store.FilesystemRevisionStore(tmp_path)

    assert store.read("some/key.json") is None

    store.write("some/key.json", b"hello", content_type="application/json")

    assert store.read("some/key.json") == b"hello"


def test_publish_immutable_rejects_a_changed_object() -> None:
    store = artifact_store.FilesystemRevisionStore.__new__(artifact_store.FilesystemRevisionStore)
    written: dict[str, bytes] = {}
    store.read = lambda key: written.get(key)  # type: ignore[method-assign]
    store.write = lambda key, content, *, content_type: written.__setitem__(key, content)  # type: ignore[method-assign]

    artifact_store.publish_immutable(store, "k", b"first", content_type="application/json")
    artifact_store.publish_immutable(store, "k", b"first", content_type="application/json")

    with pytest.raises(artifact_store.ArtifactStoreError, match="collision"):
        artifact_store.publish_immutable(store, "k", b"second", content_type="application/json")


def test_read_required_fails_closed_on_missing_key() -> None:
    store = artifact_store.FilesystemRevisionStore.__new__(artifact_store.FilesystemRevisionStore)
    store.read = lambda key: None  # type: ignore[method-assign]

    with pytest.raises(artifact_store.ArtifactStoreError, match="missing"):
        artifact_store.read_required(store, "missing-key")
