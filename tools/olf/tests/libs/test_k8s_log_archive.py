import gzip
import json
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("kubernetes")

from libs import k8s_log_archive  # noqa: E402


class _Core:
    def __init__(self, pods: list[NS], logs: dict[tuple[str, str], str | Exception]) -> None:
        self._pods, self._logs = pods, logs

    def list_namespaced_pod(self, namespace: str) -> NS:
        return NS(items=self._pods)

    def read_namespaced_pod_log(self, *, name: str, container: str, **_kwargs: object) -> str:
        value = self._logs[(name, container)]
        if isinstance(value, Exception):
            raise value
        return value


def _pod(name: str, *containers: str, started: bool = True) -> NS:
    return NS(
        metadata=NS(name=name),
        status=NS(container_statuses=[NS(name=c) for c in containers] if started else None),
        spec=NS(containers=[NS(name=c) for c in containers]),
    )


class _Uploads:
    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put_object(self, **kwargs: object) -> None:
        self.puts.append(kwargs)

    def records(self) -> list[dict]:
        return [json.loads(line) for line in gzip.decompress(self.puts[0]["Body"]).decode().splitlines()]


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch) -> _Uploads:
    fake = _Uploads()
    monkeypatch.setattr(k8s_log_archive, "s3_client", lambda: fake)
    monkeypatch.setattr(k8s_log_archive.config, "load_incluster_config", lambda: None)
    monkeypatch.setenv("OPENLAKEFORGE_KUBE_NAMESPACE", "olf-dev")
    monkeypatch.setenv("OPENLAKEFORGE_LOG_BASE_URI", "s3://ops/logs")
    return fake


def _serve(monkeypatch: pytest.MonkeyPatch, core: _Core) -> None:
    monkeypatch.setattr(k8s_log_archive.client, "CoreV1Api", lambda: core)


def test_every_log_line_is_archived_as_gzipped_ndjson_under_a_partitioned_key(
    uploads: _Uploads, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logs = {("dagster-0", "web"): "a\nb", ("dagster-0", "sidecar"): "c"}
    _serve(monkeypatch, _Core([_pod("dagster-0", "web", "sidecar")], logs))

    k8s_log_archive.main()

    (put,) = uploads.puts
    assert put["Bucket"] == "ops"
    assert put["ContentEncoding"] == "gzip"
    assert put["Key"].startswith("logs/k8s/namespace=olf-dev/date=")
    assert put["Key"].endswith(".ndjson.gz")
    records = uploads.records()
    assert [(r["pod"], r["container"], r["line"]) for r in records] == [
        ("dagster-0", "web", "a"),
        ("dagster-0", "web", "b"),
        ("dagster-0", "sidecar", "c"),
    ]
    assert {r["schema"] for r in records} == {"openlakeforge.k8s_log.v1"}
    assert "Archived 3 Kubernetes log records" in capsys.readouterr().out


def test_an_unreadable_container_is_recorded_and_does_not_stop_the_archive(
    uploads: _Uploads, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _Core([_pod("p", "bad", "ok")], {("p", "bad"): RuntimeError("container not started"), ("p", "ok"): "fine"})
    _serve(monkeypatch, core)

    k8s_log_archive.main()

    records = uploads.records()
    assert records[0]["archive_error"] == "container not started"
    assert records[1]["line"] == "fine"


def test_a_pod_without_container_statuses_is_read_through_its_spec(
    uploads: _Uploads, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _Core([_pod("pending", "init", started=False)], {("pending", "init"): "boot"}))

    k8s_log_archive.main()

    assert [r["line"] for r in uploads.records()] == ["boot"]


def test_an_empty_namespace_still_leaves_a_marker_record(uploads: _Uploads, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _Core([], {}))

    k8s_log_archive.main()

    (record,) = uploads.records()
    assert record["message"] == "no pod log lines returned"
