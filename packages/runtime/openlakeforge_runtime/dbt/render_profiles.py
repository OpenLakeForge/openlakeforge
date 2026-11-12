from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

# The project root is an input, never derived from where this module is installed.
PROJECT_ROOT_ENV = "OPENLAKEFORGE_PROJECT_ROOT"
PROFILE_TEMPLATE_DIR = Path(__file__).resolve().parent / "profiles"
# Where the image bakes the profile and the code location reads it: outside
# lakehouse_code/, so it never enters a project revision (#546).
BAKED_PROFILES_SUBDIR = Path("generated") / "dbt-profiles"
# A check of a customer project renders that project's profiles elsewhere:
# the distribution's baked file names only the products it was built with.
PROFILES_DIR_ENV = "OPENLAKEFORGE_DBT_PROFILES_DIR"
# Each product's packages.yml names the shared openlakeforge_dbt package
# through this variable, because a path relative to the product cannot name
# the installed distribution from a customer repository (#627). Every dbt
# command renders packages.yml, so whatever runs dbt sets it to this
# distribution's own.
PACKAGES_DIR_ENV = "OPENLAKEFORGE_DBT_PACKAGES_DIR"
DBT_PACKAGES_DIR = Path(__file__).resolve().parent
# A template per query engine. Trino is the only one; a second engine (#547)
# adds a template here, while the stage values stay env_var() references.
DEFAULT_ADAPTER = "trino"

# The bake parse never connects, but the profile reads these with no
# default, so it needs values. They are deliberately no stage's: the image
# serves every stage, and each run re-parses with its pod's own values
# (dagster-dbt gives every invocation a fresh target path).
PARSE_PLACEHOLDERS = {
    "OPENLAKEFORGE_QUERY_TRINO_HOST": "stage-query-host",
    "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
    "OPENLAKEFORGE_DBT_TRINO_USER": "stage-query-user",
    "OPENLAKEFORGE_CATALOG_NAME": "stage_catalog",
    "OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX": "",
}


def project_root() -> Path:
    """The directory holding `lakehouse_code/`: `OPENLAKEFORGE_PROJECT_ROOT`, else the working directory."""
    return Path(os.environ.get(PROJECT_ROOT_ENV) or Path.cwd())


def baked_profiles_dir() -> Path:
    return project_root() / BAKED_PROFILES_SUBDIR


def resolve_profiles_dir() -> Path:
    return Path(os.environ.get(PROFILES_DIR_ENV) or baked_profiles_dir())


def dbt_environment() -> dict[str, str]:
    """The variables a dbt invocation needs to resolve the distribution's package."""
    return {PACKAGES_DIR_ENV: str(DBT_PACKAGES_DIR)}


def gold_project_dir(gold_root: Path) -> Path | None:
    """The Gold dbt project (ADR 0004): `gold_root` itself, when it holds one."""
    return gold_root if (gold_root / "dbt_project.yml").is_file() else None


def product_names(gold_root: Path) -> list[str]:
    """Each product's folder under `models/`; `_`-prefixed folders, such as `_sources`, are shared."""
    models = gold_root / "models"
    if not models.is_dir():
        return []
    return sorted(path.name for path in models.iterdir() if path.is_dir() and not path.name.startswith(("_", ".")))


def profile_name(project_dir: Path) -> str:
    dbt_project = project_dir / "dbt_project.yml"
    name = _read_dbt_project_field(dbt_project, "profile") or _read_dbt_project_field(dbt_project, "name")
    if not name:
        raise ValueError(f"Cannot determine dbt profile name from {dbt_project}")
    return name


def render_profile(project_dir: Path, adapter: str = DEFAULT_ADAPTER) -> str:
    project_dir = project_dir.resolve()
    template_path = PROFILE_TEMPLATE_DIR / f"{adapter}.yml"
    if not template_path.is_file():
        supported = ", ".join(sorted(path.stem for path in PROFILE_TEMPLATE_DIR.glob("*.yml")))
        raise ValueError(f"Unsupported dbt adapter {adapter!r}. Supported: {supported}.")

    return template_path.read_text(encoding="utf-8").replace("{{PROFILE_NAME}}", profile_name(project_dir))


def write_profile(project_dir: Path, output_dir: Path, adapter: str = DEFAULT_ADAPTER) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_path = output_dir / "profiles.yml"
    profile_path.write_text(render_profile(project_dir, adapter=adapter), encoding="utf-8")
    return profile_path


def bake_manifest(project_dir: Path, profiles_dir: Path, dbt: str = "dbt") -> Path:
    """Parse a stage-neutral manifest into the project's target/."""
    env = {**dbt_environment(), **os.environ, **PARSE_PLACEHOLDERS}
    subprocess.run([dbt, "deps", "--project-dir", str(project_dir)], check=True, env=env)
    subprocess.run(
        [dbt, "parse", "--project-dir", str(project_dir), "--profiles-dir", str(profiles_dir), "--no-partial-parse"],
        check=True,
        env=env,
    )
    # dagster-dbt seeds each run's target path with this file, and a partial
    # parse could keep placeholder relations; without it every run parses
    # in full with its own stage's values. --no-partial-parse above likewise
    # keeps a target/ copied in from a workstation from steering the bake.
    (project_dir / "target" / "partial_parse.msgpack").unlink(missing_ok=True)
    return project_dir / "target" / "manifest.json"


def ensure_baked_manifest(project_dir: Path, profiles_dir: Path | None = None) -> Path:
    """Return the image's manifest, baking one where none was (a checkout).

    The profile is ensured either way: runs read it from `profiles_dir`, and a
    checkout can hold a manifest from `olf check dbt` without one.
    """
    profiles_dir = profiles_dir or resolve_profiles_dir()
    if not (profiles_dir / "profiles.yml").is_file():
        write_profile(project_dir, profiles_dir)
    manifest_path = project_dir / "target" / "manifest.json"
    if manifest_path.is_file():
        return manifest_path
    return bake_manifest(project_dir, profiles_dir)


def _read_dbt_project_field(path: Path, field: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:\s*[\"']?([^\"'#]+)[\"']?\s*(?:#.*)?$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the OpenLakeForge dbt profile and bake the Gold manifest.")
    parser.add_argument("--adapter", default=DEFAULT_ADAPTER)
    parser.add_argument("--project-dir", type=Path, help="The dbt project; default lakehouse_code/gold.")
    parser.add_argument("--root", type=Path, help=f"Project root; default ${PROJECT_ROOT_ENV}, else the working dir.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--bake", action="store_true", help="Also resolve and parse the project's manifest.")
    args = parser.parse_args()

    root = args.root.resolve() if args.root else project_root()
    output_dir = args.output_dir or root / BAKED_PROFILES_SUBDIR
    project_dir = args.project_dir.resolve() if args.project_dir else gold_project_dir(root / "lakehouse_code" / "gold")
    if project_dir is None:
        raise SystemExit("ERROR: no Gold dbt project at lakehouse_code/gold/dbt_project.yml.")

    profile_path = write_profile(project_dir, output_dir, adapter=args.adapter)
    print(f"Rendered {profile_path}")
    if args.bake:
        print(f"dbt manifest baked at {bake_manifest(project_dir, output_dir)}")


if __name__ == "__main__":
    main()
