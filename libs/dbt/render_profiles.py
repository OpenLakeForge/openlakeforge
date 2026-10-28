from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_TEMPLATE_DIR = Path(__file__).resolve().parent / "profiles"
# Where the image bakes the profile and the code location reads it: outside
# lakehouse_code/, so it never enters a project revision (#546).
BAKED_PROFILES_DIR = REPO_ROOT / "generated" / "dbt-profiles"
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


def discover_project_dirs(root: Path | None = None) -> list[Path]:
    search_root = root or REPO_ROOT / "lakehouse_code" / "gold"
    return sorted(path.parent for path in search_root.glob("*/dbt/dbt_project.yml"))


def render_profile(project_dir: Path, adapter: str = DEFAULT_ADAPTER) -> str:
    project_dir = project_dir.resolve()
    template_path = PROFILE_TEMPLATE_DIR / f"{adapter}.yml"
    if not template_path.is_file():
        supported = ", ".join(sorted(path.stem for path in PROFILE_TEMPLATE_DIR.glob("*.yml")))
        raise ValueError(f"Unsupported dbt adapter {adapter!r}. Supported: {supported}.")

    profile_name = _read_dbt_project_field(project_dir / "dbt_project.yml", "profile")
    if not profile_name:
        profile_name = _read_dbt_project_field(project_dir / "dbt_project.yml", "name")
    if not profile_name:
        raise ValueError(f"Cannot determine dbt profile name from {project_dir / 'dbt_project.yml'}")

    replacements = {
        "{{PROFILE_NAME}}": profile_name,
        "{{GOLD_SCHEMA}}": f"{project_dir.parent.name}_gold",
    }
    rendered = template_path.read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def write_profiles(project_dirs: Iterable[Path], output_dir: Path, adapter: str = DEFAULT_ADAPTER) -> Path:
    """Write one profiles.yml holding every product's profile."""
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_path = output_dir / "profiles.yml"
    profile_path.write_text(
        "".join(render_profile(project_dir, adapter=adapter) for project_dir in project_dirs), encoding="utf-8"
    )
    return profile_path


def write_profile(project_dir: Path, output_dir: Path, adapter: str = DEFAULT_ADAPTER) -> Path:
    return write_profiles([project_dir], output_dir, adapter=adapter)


def bake_manifest(project_dir: Path, profiles_dir: Path, dbt: str = "dbt") -> Path:
    """Parse a stage-neutral manifest into the project's target/."""
    env = {**os.environ, **PARSE_PLACEHOLDERS}
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


def ensure_baked_manifest(project_dir: Path, profiles_dir: Path = BAKED_PROFILES_DIR) -> Path:
    """Return the image's manifest, baking one where none was (a checkout)."""
    manifest_path = project_dir / "target" / "manifest.json"
    if manifest_path.is_file():
        return manifest_path
    if not (profiles_dir / "profiles.yml").is_file():
        write_profiles(discover_project_dirs(project_dir.parents[1]), profiles_dir)
    return bake_manifest(project_dir, profiles_dir)


def _read_dbt_project_field(path: Path, field: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:\s*[\"']?([^\"'#]+)[\"']?\s*(?:#.*)?$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the OpenLakeForge dbt profiles and bake product manifests.")
    parser.add_argument("--adapter", default=DEFAULT_ADAPTER)
    parser.add_argument("--project-dir", action="append", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, default=BAKED_PROFILES_DIR)
    parser.add_argument("--bake", action="store_true", help="Also resolve and parse each project's manifest.")
    args = parser.parse_args()

    project_dirs = [path.resolve() for path in args.project_dir] if args.project_dir else discover_project_dirs()
    if not project_dirs:
        raise SystemExit("ERROR: no product dbt projects found.")

    profile_path = write_profiles(project_dirs, args.output_dir, adapter=args.adapter)
    print(f"Rendered {profile_path}")
    if args.bake:
        for project_dir in project_dirs:
            print(f"dbt manifest baked at {bake_manifest(project_dir, args.output_dir)}")


if __name__ == "__main__":
    main()
