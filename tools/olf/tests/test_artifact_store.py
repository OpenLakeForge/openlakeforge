from __future__ import annotations

import pytest

from olf import artifact_store, auth


def test_direct_artifact_client_uses_the_selected_olf_aws_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    sdk_client = object()

    class Session:
        def client(self, service: str, **kwargs):  # noqa: ANN003, ANN201
            calls.append((service, kwargs))
            return sdk_client

    monkeypatch.setattr(auth, "aws_session", lambda environ, *, region: calls.append((environ, region)) or Session())
    monkeypatch.setattr(
        artifact_store.config,
        "env",
        lambda name, default=None: "eu-west-1" if name == "OPENLAKEFORGE_STORAGE_REGION" else default,
    )

    with artifact_store.artifact_storage_client("direct", "ops") as client:
        assert client is sdk_client

    assert calls[0][1] == "eu-west-1"
    assert calls[1] == ("s3", {"region_name": "eu-west-1"})


def test_artifact_bucket_prefers_ops_bucket_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        artifact_store.config,
        "env",
        lambda name, default=None: {
            "OPENLAKEFORGE_OPS_BUCKET_NAME": "ops",
            "OPENLAKEFORGE_ARTIFACT_BUCKET_NAME": "legacy-artifacts",
        }.get(name, default),
    )

    assert artifact_store.artifact_bucket() == "ops"


def test_artifact_bucket_fails_closed_when_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_store.config, "env", lambda name, default=None: default)

    with pytest.raises(artifact_store.ArtifactStoreError, match="no ops/artifact bucket"):
        artifact_store.artifact_bucket()


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
