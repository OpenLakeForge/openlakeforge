"""Release manifest, checksums, compatibility matrix, and readiness gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

import typer

from olf import config
from olf.commands._shared import fail
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentError, DeploymentPreconditionError, ExecutableNotFoundError

app = typer.Typer(help="Release manifest, checksums, compatibility matrix, and readiness gate.")
workflow_app = typer.Typer(help="GitHub release-workflow safety checks.")
app.add_typer(workflow_app, name="workflow")
_RELEASE_CACHE_METADATA = ".olf-release-cache.json"


@app.command("manifest")
def release_manifest(
    catalog: str = typer.Option(
        "release/component-catalog.yaml", "--catalog", help="Path to the component catalog."
    ),
    git_sha: str = typer.Option("", "--git-sha", help="Git commit SHA for this build (defaults to HEAD)."),
    image: list[str] | None = typer.Option(  # noqa: B008 - typer's repeatable-option pattern
        None,
        "--image",
        help="Resolved image digest as name=repo@sha256:digest. Repeatable, e.g. "
        "--image project-code=ghcr.io/openlakeforge/openlakeforge/project-code@sha256:....",
    ),
    fmt: str = typer.Option("json", "--format", help="Output format: json or yaml."),
    output: str = typer.Option("", "--output", help="Write to this path instead of stdout."),
) -> None:
    """Emit the resolved component manifest: catalog + resolved image digests + git SHA."""
    from olf import release as release_module

    resolved_git_sha = git_sha or _git_sha()
    image_digests: dict[str, str] = {}
    for entry in image or []:
        if "=" not in entry:
            raise typer.Exit(code=fail(f"invalid --image {entry!r}; expected name=repo@sha256:digest"))
        name, _, reference = entry.partition("=")
        image_digests[name] = reference

    try:
        catalog_data = release_module.load_catalog(catalog)
        manifest = release_module.build_manifest(catalog_data, git_sha=resolved_git_sha, image_digests=image_digests)
        rendered = release_module.render_manifest(manifest, fmt=fmt)
    except release_module.ReleaseError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(rendered)
    else:
        typer.echo(rendered, nl=False)


@app.command("checksums")
def release_checksums(
    directory: str = typer.Option(..., "--dir", help="Directory of release assets to checksum."),
    output: str = typer.Option("", "--output", help="Write checksums.txt here (default: <dir>/checksums.txt)."),
) -> None:
    """Write a deterministic sha256 checksums.txt over every file in --dir."""
    from olf import release as release_module

    try:
        out_path = release_module.write_checksums(directory, output or None)
    except release_module.ReleaseError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(f"Wrote {out_path}")


@app.command("compatibility-matrix")
def release_compatibility_matrix(
    catalog: str = typer.Option(
        "release/component-catalog.yaml", "--catalog", help="Path to the component catalog."
    ),
    output: str = typer.Option("", "--output", help="Write to this path instead of stdout."),
) -> None:
    """Render the OpenLakeForge compatibility matrix from the component catalog."""
    from olf import release as release_module

    try:
        catalog_data = release_module.load_catalog(catalog)
    except release_module.ReleaseError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    rendered = release_module.render_compatibility_matrix(catalog_data, config.repo_root())

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(rendered)
    else:
        typer.echo(rendered, nl=False)


@app.command("check")
def release_check(
    catalog: str = typer.Option(
        "release/component-catalog.yaml", "--catalog", help="Path to the component catalog."
    ),
    tag: str = typer.Option(
        "", "--tag", help="Release tag to validate against the catalog version (e.g. v0.1.0-alpha.1)."
    ),
) -> None:
    """Release-readiness gate: catalog/tag consistency, digest and SHA pinning, lockfiles."""
    from olf import release as release_module

    try:
        report = release_module.run_release_check(config.repo_root(), catalog_path=catalog, tag=tag or None)
    except release_module.ReleaseError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc

    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("build-bundle")
def build_bundle(
    output_dir: str = typer.Option(".tmp/release-bundle", "--output-dir", help="Local bundle destination."),
) -> None:
    """Build a local, inspectable release bundle with Python-owned checksums."""
    from olf import release as release_module
    from olf.commands.images import _local_config
    from olf.deployment.local.images import build_project_code_image, build_superset_image

    root = config.repo_root()
    output = (root / output_dir).resolve()
    catalog = release_module.load_catalog(root / "release/component-catalog.yaml")
    bundle_env = _bundle_environment(catalog)
    settings, tools = _local_config("openlakeforge-local", environ=bundle_env)
    try:
        project = build_project_code_image(
            settings,
            tools,
            env=bundle_env,
            revision=settings.images.project_code_revision,
        )
        superset = build_superset_image(settings, tools, env=bundle_env)
        docker = str(tools.resolver.resolve("docker"))
        project_id = tools.runner.run([docker, "image", "inspect", "--format", "{{.Id}}", project]).stdout.strip()
        superset_id = tools.runner.run([docker, "image", "inspect", "--format", "{{.Id}}", superset]).stdout.strip()
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest = release_module.build_manifest(
        catalog,
        git_sha=_git_sha(),
        image_digests={
            "project-code": f"{project}@{project_id} (local build, not pushed)",
            "superset": f"{superset}@{superset_id} (local build, not pushed)",
        },
    )
    (output / "component-manifest.json").write_text(release_module.render_manifest(manifest, fmt="json"))
    (output / "compatibility-matrix.md").write_text(
        release_module.render_compatibility_matrix(catalog, root)
    )
    shutil.copy2(root / "release/component-catalog.yaml", output / "component-catalog.yaml")
    shutil.copy2(root / "CHANGELOG.md", output / "CHANGELOG.md")
    _write_local_sboms(output, images={"project-code": project, "superset": superset}, tools=tools)
    release_module.write_checksums(output)
    typer.echo(f"Release bundle written to {output}")


def _bundle_environment(catalog: Mapping[str, object]) -> dict[str, str]:
    """Return an isolated image configuration for a local release rehearsal."""
    components = catalog.get("components")
    images = components.get("images") if isinstance(components, Mapping) else None
    if not isinstance(images, Mapping):
        raise ValueError("component catalog has no components.images mapping")
    project_code_base = images.get("project_code_base")
    superset_base = images.get("superset_base")
    if not isinstance(project_code_base, str) or not isinstance(superset_base, str):
        raise ValueError("component catalog must pin project_code_base and superset_base images")
    return {
        **os.environ,
        "PROJECT_CODE_IMAGE_TAG": "bundle",
        "PROJECT_CODE_PYTHON_BASE_IMAGE": project_code_base,
        "SUPERSET_IMAGE_TAG": "bundle",
        "SUPERSET_BASE_IMAGE": superset_base,
    }


def _write_local_sboms(output: Path, *, images: dict[str, str], tools: Toolkit) -> None:
    """Add inspectable SPDX SBOMs when Syft is available on the maintainer host."""
    try:
        syft = str(tools.resolver.resolve("syft"))
    except ExecutableNotFoundError:
        typer.echo("WARN: syft is not installed; skipping local SBOM generation.", err=True)
        return
    for name, image in images.items():
        tools.runner.run([syft, image, "-o", f"spdx-json={output / f'{name}.spdx.json'}"], stream_output=True)


@app.command("verify-install")
def verify_install(
    asset_dir: str = typer.Option(
        ".tmp/release-assets", "--asset-dir", help="Directory containing downloaded, signed release assets."
    ),
    tag: str = typer.Option("", "--tag", help="Published release tag (defaults to the catalog version)."),
    repo_slug: str = typer.Option(
        "OpenLakeForge/openlakeforge", "--repo", help="GitHub owner/repository for the release."
    ),
    work_dir: str = typer.Option(".tmp/release-verify", "--work-dir", help="Clean-checkout verification workspace."),
    pull_images: bool = typer.Option(False, "--pull-images", help="Also pull authenticated published images."),
) -> None:
    """Authenticate a release, verify its assets/images, and validate a clean checkout."""
    from olf import release as release_module

    root = config.repo_root()
    directory = (root / asset_dir).resolve()
    catalog = release_module.load_catalog(root / "release/component-catalog.yaml")
    resolved_tag = tag or f"v{catalog['distribution']['version']}"
    tools = Toolkit.default()
    try:
        if not _cached_assets_match_tag(directory, resolved_tag, repo_slug):
            directory.mkdir(parents=True, exist_ok=True)
            tools.runner.run(
                [
                    str(tools.resolver.resolve("gh")), "release", "download", resolved_tag,
                    "--repo", repo_slug, "--clobber", "--dir", str(directory),
                ],
                cwd=root,
                stream_output=True,
            )
            _write_release_cache_metadata(directory, tag=resolved_tag, repo_slug=repo_slug)
        _verify_release_assets(directory, tag=resolved_tag, repo_slug=repo_slug, tools=tools)
        manifest = json.loads((directory / "component-manifest.json").read_text())
        images = manifest.get("resolved_images", {})
        identity = _release_identity(repo_slug, resolved_tag)
        cosign = str(tools.resolver.resolve("cosign"))
        for name in ("project-code", "superset"):
            reference = images.get(name)
            if not isinstance(reference, str) or "@sha256:" not in reference:
                raise typer.Exit(code=fail(f"component-manifest.json lacks a digest-pinned {name} image"))
            tools.runner.run(
                [
                    cosign, "verify", "--certificate-identity-regexp", identity,
                    "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com", reference,
                ],
                cwd=directory,
                stream_output=True,
            )
        _verify_clean_checkout(root, work_dir, resolved_tag, repo_slug, manifest, tools)
        if pull_images:
            docker = str(tools.resolver.resolve("docker"))
            for reference in images.values():
                tools.runner.run([docker, "pull", str(reference)], cwd=root, stream_output=True)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(f"Verified authenticated OpenLakeForge release {resolved_tag}.")


def _cached_assets_match_tag(directory: Path, tag: str, repo_slug: str) -> bool:
    """Return whether a persistent cache contains signed published assets for a repository/tag pair."""
    if not all((directory / filename).is_file() for filename in ("checksums.txt", "checksums.txt.bundle")):
        return False
    try:
        manifest = json.loads((directory / "component-manifest.json").read_text())
        cache_metadata = json.loads((directory / _RELEASE_CACHE_METADATA).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    distribution = manifest.get("distribution") if isinstance(manifest, dict) else None
    return (
        isinstance(distribution, dict)
        and distribution.get("tag") == tag
        and isinstance(cache_metadata, dict)
        and cache_metadata == {"repo": repo_slug, "tag": tag}
    )


def _write_release_cache_metadata(directory: Path, *, tag: str, repo_slug: str) -> None:
    """Record the source identity of command-owned downloaded release assets."""
    (directory / _RELEASE_CACHE_METADATA).write_text(json.dumps({"repo": repo_slug, "tag": tag}, sort_keys=True) + "\n")


@workflow_app.command("require-green-main")
def require_green_main(
    repo_slug: str = typer.Option(..., "--repo", help="GitHub owner/repository to inspect."),
    git_sha: str = typer.Option(..., "--sha", help="Release commit SHA that must be reachable from main."),
) -> None:
    """Require a release commit to be reachable from main with green repository checks."""
    try:
        _require_green_main(repo_slug, git_sha, Toolkit.default())
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def _require_green_main(repo_slug: str, git_sha: str, tools: Toolkit) -> None:
    """Verify main ancestry and the latest completed successful checks workflow run."""
    gh = str(tools.resolver.resolve("gh"))
    root = config.repo_root()
    ancestry = tools.runner.run(
        [gh, "api", f"repos/{repo_slug}/compare/{git_sha}...main", "--jq", ".status"], cwd=root
    ).stdout.strip()
    if ancestry not in {"ahead", "identical"}:
        raise DeploymentPreconditionError(
            f"release commit {git_sha} is not reachable from main (comparison status: {ancestry or 'unknown'})"
        )

    payload = tools.runner.run(
        [
            gh,
            "api",
            f"repos/{repo_slug}/actions/workflows/checks.yml/runs?head_sha={git_sha}&event=push&per_page=100",
        ],
        cwd=root,
    ).stdout
    try:
        workflow_runs = json.loads(payload).get("workflow_runs", [])
    except json.JSONDecodeError as exc:
        raise DeploymentPreconditionError("GitHub returned invalid repository-checks workflow data") from exc
    if not isinstance(workflow_runs, list):
        raise DeploymentPreconditionError("GitHub repository-checks response did not contain workflow_runs")
    matching_runs = [
        run
        for run in workflow_runs
        if isinstance(run, dict)
        and run.get("head_sha") == git_sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
    ]
    latest = max(matching_runs, key=lambda run: str(run.get("created_at", "")), default=None)
    if not isinstance(latest, dict) or latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise DeploymentPreconditionError(
            f"release commit {git_sha} does not have a successful completed Repository checks run on main"
        )


def _release_identity(repo_slug: str, tag: str) -> str:
    subject = f"https://github.com/{repo_slug}/.github/workflows/release.yml@refs/tags/{tag}"
    return "^" + re.escape(subject) + "$"


def _verify_release_assets(directory: Path, *, tag: str, repo_slug: str, tools: Toolkit) -> None:
    checksums = directory / "checksums.txt"
    if not checksums.is_file():
        raise typer.Exit(code=fail(f"missing checksum manifest: {checksums}"))
    bundle = directory / "checksums.txt.bundle"
    manifest = directory / "component-manifest.json"
    if not bundle.is_file() or not manifest.is_file():
        missing = [str(path.name) for path in (bundle, manifest) if not path.is_file()]
        raise typer.Exit(code=fail(f"missing release asset(s): {', '.join(missing)}"))
    tools.runner.run(
        [
            str(tools.resolver.resolve("cosign")), "verify-blob", str(checksums), "--bundle", str(bundle),
            "--certificate-identity-regexp", _release_identity(repo_slug, tag),
            "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
        ],
        cwd=directory,
        stream_output=True,
    )
    failures: list[str] = []
    for line in checksums.read_text().splitlines():
        digest, _, filename = line.partition("  ")
        candidate = Path(filename)
        if not digest or not filename or candidate.is_absolute() or ".." in candidate.parts:
            failures.append(f"invalid checksum entry: {line!r}")
            continue
        path = directory / filename
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        if actual != digest:
            failures.append(f"{filename}: expected {digest}, got {actual}")
    if failures:
        raise typer.Exit(code=fail("\n".join(failures)))


def _verify_clean_checkout(
    root: Path,
    work_dir: str,
    tag: str,
    repo_slug: str,
    manifest: dict[str, object],
    tools: Toolkit,
) -> None:
    work_root = (root / work_dir).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="checkout-", dir=work_root) as temporary_checkout:
        checkout = Path(temporary_checkout)
        git = str(tools.resolver.resolve("git"))
        tools.runner.run(
            [git, "clone", "--depth", "1", "--branch", tag, f"https://github.com/{repo_slug}.git", str(checkout)],
            cwd=root,
            stream_output=True,
        )
        cloned_sha = tools.runner.run([git, "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
        distribution = manifest.get("distribution", {})
        manifest_sha = distribution.get("git_sha") if isinstance(distribution, dict) else None
        if not isinstance(manifest_sha, str) or cloned_sha != manifest_sha:
            raise typer.Exit(
                code=fail(
                    f"tag {tag} points at {cloned_sha}, but component-manifest.json records git_sha={manifest_sha!r}"
                )
            )
        uv = str(tools.resolver.resolve("uv"))
        for check in ("structure", "components"):
            tools.runner.run(
                [uv, "run", "--project", "tools/olf", "--locked", "olf", "check", check],
                cwd=checkout,
                stream_output=True,
            )


def _git_sha() -> str:
    try:
        tools = Toolkit.default()
        result = tools.runner.run(
            [str(tools.resolver.resolve("git")), "rev-parse", "HEAD"], cwd=config.repo_root()
        )
        return result.stdout.strip()
    except DeploymentError:
        return "unknown"
