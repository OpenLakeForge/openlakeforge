from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import typer

from olf.commands import release
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.local.config import ImageSettings


class _Runner:
    def run(self, *args, **kwargs):  # noqa: ANN002,ANN003
        return None


class _Resolver:
    def resolve(self, name: str) -> Path:
        return Path(name)


class _Tools:
    runner = _Runner()
    resolver = _Resolver()


def test_release_identity_escapes_tag_and_repository() -> None:
    assert release._release_identity("OpenLakeForge/openlakeforge", "v0.1.0-alpha.1") == (
        r"^https://github\.com/OpenLakeForge/openlakeforge/\.github/workflows/release\.yml@refs/tags/v0\.1\.0\-alpha\.1$"
    )


def test_verify_release_assets_rejects_checksum_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "checksums.txt").write_text("deadbeef  ../outside\n")
    (tmp_path / "checksums.txt.bundle").write_text("bundle")
    (tmp_path / "component-manifest.json").write_text("{}")

    with pytest.raises(typer.Exit):
        release._verify_release_assets(
            tmp_path,
            tag="v0.1.0-alpha.1",
            repo_slug="OpenLakeForge/openlakeforge",
            tools=_Tools(),
        )


def test_verify_release_assets_checks_all_local_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = tmp_path / "asset.txt"
    payload.write_text("payload")
    (tmp_path / "checksums.txt").write_text(f"{hashlib.sha256(payload.read_bytes()).hexdigest()}  asset.txt\n")
    (tmp_path / "checksums.txt.bundle").write_text("bundle")
    (tmp_path / "component-manifest.json").write_text("{}")

    release._verify_release_assets(
        tmp_path,
        tag="v0.1.0-alpha.1",
        repo_slug="OpenLakeForge/openlakeforge",
        tools=_Tools(),
    )


def test_write_local_sboms_uses_syft_before_checksums(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            calls.append(argv)

    class Resolver:
        def resolve(self, name: str) -> Path:
            assert name == "syft"
            return Path("syft")

    class Tools:
        runner = Runner()
        resolver = Resolver()

    release._write_local_sboms(tmp_path, images={"project-code": "project", "superset": "superset"}, tools=Tools())

    assert calls == [
        ["syft", "project", "-o", f"spdx-json={tmp_path / 'project-code.spdx.json'}"],
        ["syft", "superset", "-o", f"spdx-json={tmp_path / 'superset.spdx.json'}"],
    ]


def test_bundle_environment_uses_catalog_pins_and_isolated_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_CODE_IMAGE_TAG", "local")
    monkeypatch.setenv("PROJECT_CODE_PYTHON_BASE_IMAGE", "python:untrusted")
    monkeypatch.setenv("SUPERSET_IMAGE_TAG", "local")
    monkeypatch.setenv("SUPERSET_BASE_IMAGE", "superset:untrusted")

    environment = release._bundle_environment(
        {
            "components": {
                "images": {
                    "project_code_base": "python:3.12@sha256:project",
                    "superset_base": "superset:6@sha256:superset",
                }
            }
        }
    )

    assert environment["PROJECT_CODE_IMAGE_TAG"] == "bundle"
    assert environment["PROJECT_CODE_PYTHON_BASE_IMAGE"] == "python:3.12@sha256:project"
    assert environment["SUPERSET_IMAGE_TAG"] == "bundle"
    assert environment["SUPERSET_BASE_IMAGE"] == "superset:6@sha256:superset"
    settings = ImageSettings.from_environment(environment)
    assert settings.project_code_image.endswith(":bundle")
    assert settings.project_code_python_base_image == "python:3.12@sha256:project"
    assert settings.superset_image.endswith(":bundle")
    assert settings.superset_base_image == "superset:6@sha256:superset"


def test_verify_clean_checkout_uses_a_fresh_temporary_directory_per_run(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    class Runner:
        def run(self, argv, *, cwd=None, **kwargs):  # noqa: ANN001, ANN003
            calls.append((argv, cwd))
            stdout = "expected-sha\n" if argv[1:3] == ["rev-parse", "HEAD"] else ""
            return type("Result", (), {"stdout": stdout})()

    class Resolver:
        def resolve(self, name: str) -> Path:
            return Path(name)

    tools = type("Tools", (), {"runner": Runner(), "resolver": Resolver()})()
    manifest = {"distribution": {"git_sha": "expected-sha"}}

    release._verify_clean_checkout(tmp_path, ".tmp/release-verify", "v0.1.0", "owner/repo", manifest, tools)
    release._verify_clean_checkout(tmp_path, ".tmp/release-verify", "v0.1.0", "owner/repo", manifest, tools)

    clone_paths = [Path(argv[-1]) for argv, _ in calls if argv[1] == "clone"]
    assert len(clone_paths) == 2
    assert clone_paths[0] != clone_paths[1]
    assert all(not path.exists() for path in clone_paths)


def test_cached_release_assets_are_refreshed_when_the_tag_changes(tmp_path: Path) -> None:
    (tmp_path / "checksums.txt").write_text("checksum")
    (tmp_path / "checksums.txt.bundle").write_text("signed bundle")
    (tmp_path / "component-manifest.json").write_text('{"distribution": {"tag": "v0.1.0"}}')
    release._write_release_cache_metadata(tmp_path, tag="v0.1.0", repo_slug="owner/repo")

    assert release._cached_assets_match_tag(tmp_path, "v0.1.0", "owner/repo") is True
    assert release._cached_assets_match_tag(tmp_path, "v0.2.0", "owner/repo") is False
    assert release._cached_assets_match_tag(tmp_path, "v0.1.0", "another/repo") is False


def test_rehearsal_bundle_is_not_treated_as_cached_published_assets(tmp_path: Path) -> None:
    (tmp_path / "checksums.txt").write_text("checksum")
    (tmp_path / "component-manifest.json").write_text('{"distribution": {"tag": "v0.1.0"}}')

    assert release._cached_assets_match_tag(tmp_path, "v0.1.0", "owner/repo") is False


def test_cached_release_assets_without_a_valid_manifest_are_refreshed(tmp_path: Path) -> None:
    (tmp_path / "checksums.txt").write_text("checksum")
    (tmp_path / "checksums.txt.bundle").write_text("signed bundle")
    (tmp_path / "component-manifest.json").write_text("not json")

    assert release._cached_assets_match_tag(tmp_path, "v0.1.0", "owner/repo") is False


def test_require_green_main_accepts_the_latest_successful_main_checks_run() -> None:
    calls: list[list[str]] = []

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            calls.append(argv)
            if "compare" in argv[2]:
                return type("Result", (), {"stdout": "ahead\n"})()
            return type(
                "Result",
                (),
                {
                    "stdout": (
                        '{"workflow_runs": ['
                        '{"head_sha":"abc","head_branch":"main","event":"push",'
                        '"created_at":"2026-08-01T00:00:00Z","status":"completed","conclusion":"failure"},'
                        '{"head_sha":"abc","head_branch":"main","event":"push",'
                        '"created_at":"2026-08-02T00:00:00Z","status":"completed","conclusion":"success"}'
                        "]}"
                    )
                },
            )()

    tools = type("Tools", (), {"runner": Runner(), "resolver": _Resolver()})()

    release._require_green_main("owner/repo", "abc", tools)

    assert calls[0] == ["gh", "api", "repos/owner/repo/compare/abc...main", "--jq", ".status"]
    assert calls[1] == [
        "gh",
        "api",
        "repos/owner/repo/actions/workflows/checks.yml/runs?head_sha=abc&event=push&per_page=100",
    ]


def test_require_green_main_rejects_a_newer_failed_checks_run() -> None:
    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            if "compare" in argv[2]:
                return type("Result", (), {"stdout": "identical\n"})()
            return type(
                "Result",
                (),
                {
                    "stdout": (
                        '{"workflow_runs": ['
                        '{"head_sha":"abc","head_branch":"main","event":"push",'
                        '"created_at":"2026-08-02T00:00:00Z","status":"completed","conclusion":"failure"}'
                        "]}"
                    )
                },
            )()

    tools = type("Tools", (), {"runner": Runner(), "resolver": _Resolver()})()

    with pytest.raises(DeploymentPreconditionError, match="does not have a successful completed"):
        release._require_green_main("owner/repo", "abc", tools)
