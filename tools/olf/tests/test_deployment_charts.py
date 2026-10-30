from __future__ import annotations

import fcntl
import hashlib
import io
import tarfile
from pathlib import Path

from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.charts import (
    ChartRequest,
    _chart_cache_lock,
    prepare_cached_chart,
    prepare_cached_dagster_chart_no_schema,
)
from olf.deployment.context import DeploymentContext
from olf.tooling.helm import Helm
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver
from olf.tooling.retry import RetryPolicy

_POLICY = RetryPolicy(max_attempts=2, delay_seconds=0)


def _fake_dagster_archive_bytes() -> bytes:
    """A minimal real gzip tarball shaped like `helm pull`'s Dagster output:
    a top-level `dagster/` dir with a `Chart.yaml` and the
    `values.schema.json` the code under test must strip."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, content in (
            (b"dagster/Chart.yaml", b"name: dagster\n"),
            (b"dagster/values.schema.json", b"{}"),
        ):
            info = tarfile.TarInfo(name.decode())
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _request(tmp_path: Path) -> ChartRequest:
    return ChartRequest(
        display_name="Trino",
        repo_name="trino",
        repo_url="https://trinodb.github.io/charts",
        chart_ref="trino/trino",
        version="1.42.2",
        package_path=tmp_path / "helm" / "charts" / "trino-1.42.2.tgz",
    )


def _dagster_request(tmp_path: Path) -> ChartRequest:
    return ChartRequest(
        display_name="Dagster",
        repo_name="dagster",
        repo_url="https://dagster-io.github.io/helm",
        chart_ref="dagster/dagster",
        version="1.13.6",
        package_path=tmp_path / "helm" / "charts" / "dagster-1.13.6-no-schema.tgz",
    )


def test_chart_cache_lock_blocks_another_handle_for_the_same_package(tmp_path: Path) -> None:
    """The shared installed cache must serialize chart preparation across processes."""
    request = _request(tmp_path)
    lock_path = request.package_path.with_name(f".{request.package_path.name}.lock")

    with _chart_cache_lock(request):
        with lock_path.open("a+") as competing_handle:
            try:
                fcntl.flock(competing_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("chart cache lock did not block a competing handle")


def test_reuses_valid_cached_chart(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request.package_path.parent.mkdir(parents=True)
    request.package_path.write_text("cached")
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0))
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    result = prepare_cached_chart(request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY)

    assert result == request.package_path
    assert len(runner.calls) == 1  # only the `helm show chart` cache-validity check
    assert runner.calls[0].argv[1:3] == ["show", "chart"]


def test_downloads_when_no_cached_chart_exists(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0))
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    prepare_cached_chart(request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY)

    subcommands = [tuple(c.argv[1:3]) for c in runner.calls]
    assert ("repo", "add") in subcommands
    assert ("repo", "update") in subcommands
    assert ("pull", "trino/trino") in subcommands
    add_call = next(c for c in runner.calls if c.argv[1:3] == ["repo", "add"])
    assert add_call.kwargs["retry_policy"] is _POLICY


def test_redownloads_when_cache_is_invalid(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request.package_path.parent.mkdir(parents=True)
    request.package_path.write_text("corrupt")

    class FailingShowRunner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            from _tooling_support import RecordedCall

            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if argv[1:3] == ["show", "chart"]:
                return CommandResult(argv=(), returncode=1, stdout="", stderr="bad chart", duration_seconds=0.0)
            return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)

    runner = FailingShowRunner()
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    prepare_cached_chart(request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY)

    assert not request.package_path.exists()  # the corrupt package was removed before re-download
    assert any(c.argv[1:3] == ["pull", "trino/trino"] for c in runner.calls)


class _DagsterPackagingRunner(RecordingRunner):
    """Fakes `helm pull --destination` (writes a real gzip tarball shaped
    like Dagster's upstream chart, schema file included) and `helm package`
    (writes the naturally-named `.tgz` `helm package` would produce), so the
    digest check, extraction, schema-strip, and rename steps under test all
    have real files to act on.
    """

    def __init__(self, *, archive_bytes: bytes | None = None) -> None:
        super().__init__(CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0))
        self._archive_bytes = archive_bytes if archive_bytes is not None else _fake_dagster_archive_bytes()

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))

        if argv[1] == "pull" and "--destination" in argv:
            destination = Path(argv[argv.index("--destination") + 1])
            (destination / "dagster-1.13.6.tgz").write_bytes(self._archive_bytes)
        elif argv[1] == "package":
            destination = Path(argv[argv.index("--destination") + 1])
            (destination / "dagster-1.13.6.tgz").write_bytes(b"fake-chart")

        return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="", duration_seconds=0.0)


def test_dagster_chart_reuses_valid_cached_package(tmp_path: Path) -> None:
    request = _dagster_request(tmp_path)
    request.package_path.parent.mkdir(parents=True)
    request.package_path.write_text("cached")
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0))
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    result = prepare_cached_dagster_chart_no_schema(
        request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY
    )

    assert result == request.package_path
    assert len(runner.calls) == 1  # only the `helm show chart` cache-validity check
    assert runner.calls[0].argv[1:3] == ["show", "chart"]


def test_dagster_chart_strips_schema_and_repackages_under_no_schema_name(tmp_path: Path) -> None:
    request = _dagster_request(tmp_path)
    runner = _DagsterPackagingRunner()
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    result = prepare_cached_dagster_chart_no_schema(
        request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY
    )

    assert result == request.package_path
    assert request.package_path.is_file()
    assert request.package_path.read_bytes() == b"fake-chart"

    subcommands = [tuple(c.argv[1:3]) for c in runner.calls]
    assert ("repo", "add") in subcommands
    assert ("repo", "update") in subcommands
    pull_call = next(c for c in runner.calls if c.argv[1:3] == ["pull", "dagster/dagster"])
    assert "--destination" in pull_call.argv
    assert "--untar" not in pull_call.argv
    package_call = next(c for c in runner.calls if c.argv[1] == "package")
    assert package_call.argv[2].endswith("/dagster")  # packaged the extracted chart dir, schema already deleted


def test_dagster_chart_verifies_downloaded_digest_before_repack(tmp_path: Path) -> None:
    """An installed distribution pins the pristine upstream Dagster chart's
    digest via the component catalog - it must be checked against the
    downloaded archive before extraction, since the repacked artifact's own
    digest never matches (its content changed)."""
    archive_bytes = _fake_dagster_archive_bytes()
    request = _dagster_request(tmp_path)
    request = ChartRequest(**{**request.__dict__, "sha256": hashlib.sha256(archive_bytes).hexdigest()})
    runner = _DagsterPackagingRunner(archive_bytes=archive_bytes)
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    result = prepare_cached_dagster_chart_no_schema(
        request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY
    )

    assert result == request.package_path
    assert request.package_path.read_bytes() == b"fake-chart"


def test_dagster_chart_rejects_digest_mismatch(tmp_path: Path) -> None:
    request = _dagster_request(tmp_path)
    request = ChartRequest(**{**request.__dict__, "sha256": "0" * 64})
    runner = _DagsterPackagingRunner()
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    try:
        prepare_cached_dagster_chart_no_schema(request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY)
    except ValueError as exc:
        assert "digest" in str(exc)
    else:
        raise AssertionError("expected a digest-mismatch ValueError")

    assert not request.package_path.exists()
    # The mismatch must be caught before the archive is ever unpacked.
    assert not any(c.argv[1] == "package" for c in runner.calls)


def test_dagster_chart_writes_a_digest_sidecar_for_a_pinned_cache_entry(tmp_path: Path) -> None:
    archive_bytes = _fake_dagster_archive_bytes()
    request = _dagster_request(tmp_path)
    request = ChartRequest(**{**request.__dict__, "sha256": hashlib.sha256(archive_bytes).hexdigest()})
    runner = _DagsterPackagingRunner(archive_bytes=archive_bytes)
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    prepare_cached_dagster_chart_no_schema(request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY)

    sidecar = request.package_path.with_name(request.package_path.name + ".sha256")
    assert sidecar.is_file()
    assert sidecar.read_text().strip() == hashlib.sha256(request.package_path.read_bytes()).hexdigest()


def test_dagster_chart_reuses_a_pinned_cache_entry_whose_sidecar_matches(tmp_path: Path) -> None:
    request = _dagster_request(tmp_path)
    request = ChartRequest(**{**request.__dict__, "sha256": "a" * 64})
    request.package_path.parent.mkdir(parents=True)
    request.package_path.write_bytes(b"cached-derivative")
    sidecar = request.package_path.with_name(request.package_path.name + ".sha256")
    sidecar.write_text(hashlib.sha256(b"cached-derivative").hexdigest() + "\n")
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0))
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    result = prepare_cached_dagster_chart_no_schema(
        request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY
    )

    assert result == request.package_path
    assert request.package_path.read_bytes() == b"cached-derivative"
    assert len(runner.calls) == 1  # only the `helm show chart` cache-validity check


def test_dagster_chart_redownloads_when_a_pinned_cache_entry_has_no_sidecar(tmp_path: Path) -> None:
    """A structurally-valid but corrupted-or-replaced cached derivative must
    not be trusted just because `helm show chart` can parse it - a missing
    or mismatched digest sidecar must force a fresh, re-verified download.
    """
    archive_bytes = _fake_dagster_archive_bytes()
    request = _dagster_request(tmp_path)
    request = ChartRequest(**{**request.__dict__, "sha256": hashlib.sha256(archive_bytes).hexdigest()})
    request.package_path.parent.mkdir(parents=True)
    request.package_path.write_bytes(b"corrupted-but-helm-parses-it")
    runner = _DagsterPackagingRunner(archive_bytes=archive_bytes)
    helm = Helm(runner, PathExecutableResolver(overrides={"helm": Path("helm")}))
    context = DeploymentContext.local(repo_root=tmp_path)

    result = prepare_cached_dagster_chart_no_schema(
        request, helm=helm, paths=context.paths, env={}, retry_policy=_POLICY
    )

    assert result == request.package_path
    assert request.package_path.read_bytes() == b"fake-chart"  # freshly re-packaged, not the corrupted bytes
    assert any(c.argv[1:3] == ["pull", "dagster/dagster"] for c in runner.calls)
