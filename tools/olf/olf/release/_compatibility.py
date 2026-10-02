"""Compatibility matrix rendering and drift checks.

Renders the OpenLakeForge <-> Kubernetes <-> Terraform <-> Helm chart
compatibility matrix from `release/component-catalog.yaml` plus the
repo's own Terraform lockfiles and module defaults, and validates the
checked-in docs copy and Terraform required_version/lockfile invariants
stay in sync with it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from olf.release._manifest import CheckResult

COMPATIBILITY_MATRIX_HEADER = """<!--
This file is generated from release/component-catalog.yaml. Do not hand-edit
the tables below -- regenerate with:

    olf release compatibility-matrix --output docs/release/compatibility-matrix.md

(or `uv run --project tools/olf olf release compatibility-matrix --output ...`
from the repo root). The same command produces the copy embedded in every
release bundle by .github/workflows/release.yml, so this file always matches
what a tagged release publishes as of the last catalog update.
`olf check all` fails if this checked-in file drifts from a fresh render;
regenerate it whenever release/component-catalog.yaml changes.
-->

"""


def render_compatibility_matrix(catalog: dict[str, Any], repo_root: str | Path = ".") -> str:
    """Render the OpenLakeForge <-> Kubernetes <-> Terraform <-> Helm chart
    compatibility matrix as Markdown.

    Every table is sourced from the catalog except "Terraform providers by
    root", which is read directly from each `.terraform.lock.hcl` under
    `infra/terraform` -- the exact version a consumer of that root actually
    gets, with no intermediate copy to fall out of sync. Likewise "Helm
    charts" is read from each Terraform module's own `chart_version`
    variable default, which the release check cross-validates against the
    cached-chart versions used by the deployment wrappers.

    Always includes COMPATIBILITY_MATRIX_HEADER so the checked-in docs copy
    and _check_compatibility_matrix_up_to_date's comparison stay identical by
    construction.
    """
    root = Path(repo_root)
    components = catalog.get("components") or {}
    distribution = catalog.get("distribution") or {}
    version = distribution.get("version", "unknown")
    terraform = components.get("terraform") or {}
    images = components.get("images") or {}

    lines: list[str] = []
    lines.append(f"# OpenLakeForge {version} compatibility matrix")
    lines.append("")
    lines.append(
        "Generated from `release/component-catalog.yaml`. Every version below is "
        "the exact input pinned for this release; see "
        "[docs/release/component-catalog.md](component-catalog.md) for the update process."
    )
    lines.append("")

    lines.append("## Platform")
    lines.append("")
    lines.append("| Component | Required version |")
    lines.append("| --- | --- |")
    lines.append(f"| Terraform | {terraform.get('required_version', 'unknown')} |")
    lines.append("")

    lines.append("## Terraform providers")
    lines.append("")
    lines.append(
        "The tracked/approved version for each provider. Individual Terraform "
        "roots can lock an older compatible version (below) -- consult that "
        "table for the exact version actually applied to a given target."
    )
    lines.append("")
    lines.append("| Provider | Tracked version |")
    lines.append("| --- | --- |")
    for provider, provider_version in sorted((terraform.get("providers") or {}).items()):
        lines.append(f"| {provider} | {provider_version} |")
    lines.append("")

    terraform_root = root / "infra" / "terraform"
    lockfile_paths = sorted(terraform_root.rglob(".terraform.lock.hcl")) if terraform_root.is_dir() else []
    lockfiles = {
        path.relative_to(root).as_posix(): _terraform_lock_provider_versions(path) for path in lockfile_paths
    }
    if lockfiles:
        lines.append("### Terraform providers by root")
        lines.append("")
        lines.append(
            "The exact version `.terraform.lock.hcl` pins for each root -- what a "
            "consumer of that target actually gets, not the tracked version above."
        )
        lines.append("")
        roots = sorted(lockfiles)
        providers = sorted({provider for root_locks in lockfiles.values() for provider in root_locks})
        lines.append("| Provider | " + " | ".join(roots) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in roots) + " |")
        for provider in providers:
            row = [lockfiles.get(root_path, {}).get(provider, "") for root_path in roots]
            lines.append(f"| {provider} | " + " | ".join(row) + " |")
        lines.append("")

    lines.append("## Helm charts")
    lines.append("")
    lines.append("| Chart | Version |")
    lines.append("| --- | --- |")
    for chart, chart_version in sorted(_helm_chart_versions_from_terraform_modules(root).items()):
        lines.append(f"| {chart} | {chart_version} |")
    lines.append("")

    toolchain = components.get("toolchain") or {}
    if toolchain:
        lines.append("## Managed toolchain")
        lines.append("")
        lines.append(
            "Terraform, Helm, kubectl, and kind are provisioned by `olf toolchain` "
            "(#127) rather than installed by the consumer; versions below are what "
            "the current release provisions."
        )
        lines.append("")
        lines.append("| Tool | Version |")
        lines.append("| --- | --- |")
        for tool, entry in sorted(toolchain.items()):
            lines.append(f"| {tool} | {entry.get('version', 'unknown') if isinstance(entry, dict) else 'unknown'} |")
        lines.append("")

    lines.append("## Container images")
    lines.append("")
    lines.append("| Image | Reference |")
    lines.append("| --- | --- |")
    for name, reference in sorted(images.items()):
        lines.append(f"| {name} | `{reference}` |")
    lines.append("")

    lines.append("## Cloud services (deployment targets)")
    lines.append("")
    lines.append("| Target | Kubernetes foundation | Object storage | Catalog | Managed database |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append("| Local | kind | SeaweedFS (in-cluster) | Polaris (in-cluster) | PostgreSQL (in-cluster) |")
    lines.append("| Azure POC | AKS | SeaweedFS (in-cluster) | Polaris (in-cluster) | PostgreSQL (in-cluster) |")
    lines.append("| AWS POC | EKS | S3 | AWS Glue | RDS PostgreSQL |")
    lines.append("")

    lines.append("## Supported upgrade paths")
    lines.append("")
    lines.append(
        "OpenLakeForge is in the Alpha lifecycle stage (see "
        "[releasing.md](releasing.md#lifecycle-stages)): breaking changes are allowed between alpha "
        "releases, with migration notes published in `CHANGELOG.md` for every "
        "tag. Until Beta, only the latest alpha tag is maintained; there is no "
        "supported upgrade path guarantee prior to `v0.1.0-alpha.1`."
    )
    lines.append("")

    return COMPATIBILITY_MATRIX_HEADER + "\n".join(lines)


def _check_compatibility_matrix_up_to_date(repo_root: Path, catalog: dict[str, Any]) -> CheckResult:
    """The checked-in docs/release/compatibility-matrix.md must match a fresh
    render of the catalog exactly.
    """
    doc_path = repo_root / "docs/release/compatibility-matrix.md"
    if not doc_path.is_file():
        return CheckResult("compatibility matrix doc is up to date", False, f"{doc_path} does not exist")
    expected = render_compatibility_matrix(catalog, repo_root)
    actual = doc_path.read_text()
    if actual != expected:
        return CheckResult(
            "compatibility matrix doc is up to date",
            False,
            "docs/release/compatibility-matrix.md does not match a fresh render of release/component-catalog.yaml "
            "-- regenerate with 'olf release compatibility-matrix --output docs/release/compatibility-matrix.md'",
        )
    return CheckResult("compatibility matrix doc is up to date", True)


_TERRAFORM_BLOCK_PATTERN = re.compile(r"^terraform\s*\{(?P<body>.*?)^\}", re.MULTILINE | re.DOTALL)
_REQUIRED_VERSION_PATTERN = re.compile(
    r'^\s*required_version\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE
)
_REQUIRED_PROVIDERS_PATTERN = re.compile(r"\brequired_providers\s*\{")


def _terraform_root_dirs(repo_root: Path) -> list[Path]:
    """Return supported deployment/foundation Terraform roots."""
    terraform_root = repo_root / "infra" / "terraform"
    return [
        root_dir
        for group_dir in (terraform_root / "foundations", terraform_root / "environments")
        if group_dir.is_dir()
        for root_dir in sorted(path for path in group_dir.iterdir() if path.is_dir())
    ]


def _provider_using_terraform_roots(repo_root: Path) -> list[Path]:
    """Return supported Terraform roots that declare provider dependencies."""
    return [
        root_dir
        for root_dir in _terraform_root_dirs(repo_root)
        if any(_REQUIRED_PROVIDERS_PATTERN.search(path.read_text()) for path in root_dir.glob("*.tf"))
    ]


def _check_terraform_required_versions_match_catalog(repo_root: Path, catalog: dict[str, Any]) -> CheckResult:
    """Every Terraform root must declare the catalog's advertised constraint."""
    catalog_required_version = ((catalog.get("components") or {}).get("terraform") or {}).get(
        "required_version"
    )
    if not isinstance(catalog_required_version, str) or not catalog_required_version:
        return CheckResult(
            "Terraform required versions match the component catalog",
            False,
            "components.terraform.required_version is missing",
        )

    terraform_root = repo_root / "infra" / "terraform"
    if not terraform_root.is_dir():
        return CheckResult(
            "Terraform required versions match the component catalog",
            False,
            "infra/terraform does not exist",
        )

    problems: list[str] = []
    roots_checked = 0
    root_dirs = _terraform_root_dirs(repo_root)
    if not root_dirs:
        problems.append("no Terraform roots found under infra/terraform/foundations or environments")

    for root_dir in root_dirs:
        blocks = [
            (source_path, block)
            for source_path in sorted(root_dir.glob("*.tf"))
            for block in _TERRAFORM_BLOCK_PATTERN.finditer(source_path.read_text())
        ]
        relative_root = root_dir.relative_to(repo_root).as_posix()
        if not blocks:
            problems.append(f"{relative_root}: no terraform block")
            continue

        roots_checked += 1
        for source_path, block in blocks:
            relative_path = source_path.relative_to(repo_root).as_posix()
            found = _REQUIRED_VERSION_PATTERN.search(block.group("body"))
            if found is None:
                problems.append(f"{relative_path}: terraform block has no required_version")
                continue
            actual_version = found.group("version")
            if actual_version != catalog_required_version:
                problems.append(
                    f"{relative_path}: catalog={catalog_required_version!r}, "
                    f"terraform={actual_version!r}"
                )
    if problems:
        return CheckResult(
            "Terraform required versions match the component catalog", False, "; ".join(problems)
        )
    return CheckResult(
        "Terraform required versions match the component catalog", True, f"{roots_checked} root(s) checked"
    )


def _check_provider_using_terraform_roots_have_lockfiles(repo_root: Path) -> CheckResult:
    """Every provider-using supported root must commit its resolved lockfile."""
    roots = _provider_using_terraform_roots(repo_root)
    if not roots:
        return CheckResult(
            "provider-using Terraform roots have lockfiles",
            False,
            "no provider-using Terraform roots found under infra/terraform/foundations or environments",
        )

    missing = [
        (root_dir / ".terraform.lock.hcl").relative_to(repo_root).as_posix()
        for root_dir in roots
        if not (root_dir / ".terraform.lock.hcl").is_file()
    ]
    if missing:
        return CheckResult(
            "provider-using Terraform roots have lockfiles",
            False,
            f"missing tracked lockfile(s): {', '.join(missing)}",
        )
    return CheckResult("provider-using Terraform roots have lockfiles", True, f"{len(roots)} root(s) checked")


def _terraform_lock_provider_versions(lock_path: Path) -> dict[str, str]:
    """Read provider name -> exact selected version from a `.terraform.lock.hcl`.

    Terraform lockfiles are HCL rather than TOML, so this parses just the
    `provider "<source>" { version = "<v>" }` blocks the compatibility matrix
    needs.
    """
    provider_blocks = re.finditer(
        r'^provider\s+"(?P<provider>[^"]+)"\s*\{(?P<body>.*?)^\}',
        lock_path.read_text(),
        re.MULTILINE | re.DOTALL,
    )
    versions: dict[str, str] = {}
    for block in provider_blocks:
        version = re.search(r'^\s*version\s*=\s*"(?P<version>[^"]+)"\s*$', block.group("body"), re.MULTILINE)
        if version is not None:
            provider = block.group("provider").removeprefix("registry.terraform.io/")
            versions[provider] = version.group("version")
    return versions


def _helm_chart_versions_from_terraform_modules(repo_root: Path) -> dict[str, str]:
    """Read Helm chart name -> version from each Terraform module's own
    `chart_version` variable default under infra/terraform/modules, keyed by
    the module directory name (e.g. .../query/trino -> "trino"). A module
    with a `deps_chart_version` variable (governance/openmetadata's paired
    dependencies chart) also contributes "<module>-dependencies".
    """
    variable_blocks = re.compile(r'variable\s+"(?P<name>[^"]+)"\s*\{(?P<body>.*?)^\}', re.MULTILINE | re.DOTALL)
    default_value = re.compile(r'^\s*default\s*=\s*"(?P<default>[^"]+)"\s*$', re.MULTILINE)

    versions: dict[str, str] = {}
    modules_root = repo_root / "infra" / "terraform" / "modules"
    if not modules_root.is_dir():
        return versions
    for variables_file in sorted(modules_root.rglob("variables.tf")):
        module_name = variables_file.parent.name
        text = variables_file.read_text()
        for block in variable_blocks.finditer(text):
            if block.group("name") not in ("chart_version", "deps_chart_version"):
                continue
            default = default_value.search(block.group("body"))
            if default is None:
                continue
            key = module_name if block.group("name") == "chart_version" else f"{module_name}-dependencies"
            versions[key] = default.group("default")
    return versions
