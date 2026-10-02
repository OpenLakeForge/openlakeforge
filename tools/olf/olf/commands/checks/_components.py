"""`olf check components` and `olf check lockfiles`: catalog pins and locks."""

from __future__ import annotations

import shlex
import shutil
import tempfile
from pathlib import Path

import typer
import yaml

from olf.commands._shared import fail
from olf.commands.checks._shared import _root, _run
from olf.deployment.engine import Toolkit


def components(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Validate component catalog pins and release-readiness inputs."""
    from olf import release

    root = _root(repo_root)
    report = release.run_release_check(root)
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)


def lockfiles(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Verify uv lockfiles declared by the component catalog are current."""
    root = _root(repo_root)
    catalog = yaml.safe_load((root / "release/component-catalog.yaml").read_text())
    locks = catalog["components"]["python"].values()
    uv = str(Toolkit.default().resolver.resolve("uv"))
    for lock in locks:
        path = root / lock
        if not path.is_file() or not path.stat().st_size:
            raise typer.Exit(code=fail(f"missing or empty lockfile: {path}"))
        if path.name == "uv.lock":
            _run([uv, "lock", "--project", str(path.parent), "--check"], cwd=root)
        else:
            _check_compiled_lock(path, uv=uv, root=root)
    typer.echo("Lockfiles are in sync.")


def _check_compiled_lock(path: Path, *, uv: str, root: Path) -> None:
    """Re-run the exact pinned compile command recorded in a requirements lock."""
    lines = path.read_text().splitlines()
    if len(lines) < 2 or not lines[1].lstrip("# ").startswith("uv pip compile "):
        raise typer.Exit(code=fail(f"{path}: line 2 must record a uv pip compile command"))
    argv = shlex.split(lines[1].lstrip("# "))
    try:
        index = argv.index("--output-file")
        original_output = argv[index + 1]
    except (ValueError, IndexError) as exc:
        raise typer.Exit(code=fail(f"{path}: compile command must include --output-file")) from exc
    if (root / original_output).resolve() != path.resolve():
        raise typer.Exit(code=fail(f"{path}: compile command output must be {path.relative_to(root)}"))
    argv[0] = uv
    with tempfile.TemporaryDirectory(prefix="olf-lockfile-") as temporary:
        compiled = Path(temporary) / path.name
        shutil.copy2(path, compiled)
        argv[index + 1] = str(compiled)
        _run([*argv, "--quiet"], cwd=root)
        if "\n".join(lines[2:]) != "\n".join(compiled.read_text().splitlines()[2:]):
            raise typer.Exit(
                code=fail(f"{path.relative_to(root)} does not match its pinned uv pip compile command; regenerate it.")
            )
