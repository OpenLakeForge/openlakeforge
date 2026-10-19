"""`olf check docs`: the published-docs manifest and relative links.

openlakeforge.dev builds its sidebar from `docs/navigation.yaml` and renders
every page it lists, so a page that moves without the manifest following it
disappears from the site, and a dead relative link becomes a dead site link.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import typer
import yaml

from olf.commands._shared import fail
from olf.commands.checks._shared import _root

MANIFEST = "docs/navigation.yaml"

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_CODE_SPAN = re.compile(r"`+[^`]*`+")
# `](target)` rather than a full `[text](target)` match: link text can itself
# hold brackets, as a badge image nested inside a link does.
_INLINE_LINK = re.compile(r"\]\(\s*<?([^)\s>]*)>?")
_REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
_HTML_ATTRIBUTE = re.compile(r"""\b(src|href|srcset)\s*=\s*["']([^"']+)["']""")
_EXPLICIT_ANCHOR = re.compile(r"""<a\s+(?:[^>]*\s)?(?:id|name)\s*=\s*["']([^"']+)["']""")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass(frozen=True)
class _Manifest:
    pages: tuple[str, ...]
    autogenerate: tuple[str, ...]
    exclude: tuple[str, ...]


def docs(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Validate the published-docs manifest and every relative docs link."""
    errors = docs_errors(_root(repo_root))
    if errors:
        raise typer.Exit(code=fail("\n".join(errors)))
    typer.echo("Documentation manifest and links are valid.")


def docs_errors(root: Path) -> list[str]:
    manifest, errors = _load_manifest(root)
    sources = sorted({*_markdown_under(root, "docs"), "README.md"})
    if manifest is not None:
        errors.extend(_coverage_errors(root, manifest))
        sources = sorted({*sources, *(page for page in manifest.pages if page.endswith(".md"))})
    for source in sources:
        if (root / source).is_file():
            errors.extend(_link_errors(root, source))
    return errors


def _load_manifest(root: Path) -> tuple[_Manifest | None, list[str]]:
    path = root / MANIFEST
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, [f"{MANIFEST}: cannot read: {exc}"]
    if not isinstance(document, dict) or not isinstance(document.get("sections"), list):
        return None, [f"{MANIFEST}: must be a mapping with a `sections` list"]
    errors: list[str] = []
    pages: list[str] = []
    autogenerate: list[str] = []
    for index, section in enumerate(document["sections"]):
        where = f"{MANIFEST}: sections[{index}]"
        if not isinstance(section, dict) or not isinstance(section.get("label"), str):
            errors.append(f"{where}: needs a `label`")
            continue
        if isinstance(section.get("autogenerate"), str):
            autogenerate.append(section["autogenerate"].rstrip("/"))
        for item in section.get("items") or []:
            page = item.get("path") if isinstance(item, dict) else item
            if isinstance(page, str):
                pages.append(page)
            else:
                errors.append(f"{where}: an item must be a path or a mapping with `path`")
    exclude = document.get("exclude") or []
    if not isinstance(exclude, list) or not all(isinstance(entry, str) for entry in exclude):
        errors.append(f"{MANIFEST}: `exclude` must be a list of paths")
        exclude = []
    return _Manifest(tuple(pages), tuple(autogenerate), tuple(exclude)), errors


def _coverage_errors(root: Path, manifest: _Manifest) -> list[str]:
    errors = [
        f"{MANIFEST}: listed path does not exist: {entry}"
        for entry in (*manifest.pages, *manifest.exclude)
        if not (root / entry).exists()
    ]
    errors.extend(
        f"{MANIFEST}: autogenerate directory does not exist: {directory}"
        for directory in manifest.autogenerate
        if not (root / directory).is_dir()
    )
    seen: set[str] = set()
    for page in manifest.pages:
        if page in seen:
            errors.append(f"{MANIFEST}: page listed twice: {page}")
        seen.add(page)
    for page in _markdown_under(root, "docs"):
        listed = page in seen or any(_within(page, directory) for directory in manifest.autogenerate)
        excluded = any(_within(page, entry) for entry in manifest.exclude)
        if listed and excluded:
            errors.append(f"{MANIFEST}: page is both published and excluded: {page}")
        elif not listed and not excluded:
            errors.append(f"{MANIFEST}: page is neither published nor excluded: {page}")
    return errors


def _within(page: str, entry: str) -> bool:
    entry = entry.rstrip("/")
    return page == entry or page.startswith(entry + "/")


def _markdown_under(root: Path, directory: str) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in (root / directory).rglob("*.md"))


def _link_errors(root: Path, source: str) -> list[str]:
    errors: list[str] = []
    for line_number, target in _links(root / source):
        problem = _resolve(root, source, target)
        if problem:
            errors.append(f"{source}:{line_number}: {problem}: {target}")
    return errors


def _links(path: Path) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    for line_number, line in _prose_lines(path):
        text = _CODE_SPAN.sub("", line)
        links.extend((line_number, match) for match in _INLINE_LINK.findall(text))
        links.extend((line_number, match) for match in _REFERENCE_DEFINITION.findall(text))
        for attribute, value in _HTML_ATTRIBUTE.findall(text):
            candidates = [value]
            if attribute == "srcset":
                candidates = [part.split()[0] for part in value.split(",") if part.split()]
            links.extend((line_number, candidate) for candidate in candidates)
    return [(line_number, target) for line_number, target in links if target and not _SCHEME.match(target)]


def _prose_lines(path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    fence: str | None = None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        opener = _FENCE.match(line)
        if opener and (fence is None or opener.group(1) == fence):
            fence = None if fence else opener.group(1)
            continue
        if fence is None:
            lines.append((line_number, line))
    return lines


def _resolve(root: Path, source: str, target: str) -> str | None:
    if target.startswith("//"):
        return None
    path_part, _, anchor = target.partition("#")
    path_part = unquote(path_part.split("?", 1)[0])
    if not path_part:
        resolved = root / source
    elif path_part.startswith("/"):
        resolved = root / path_part.lstrip("/")
    else:
        resolved = (root / source).parent / path_part
    try:
        relative = resolved.resolve().relative_to(root)
    except ValueError:
        return "link leaves the repository"
    if not resolved.exists():
        return "link target does not exist"
    if anchor and resolved.is_file() and resolved.suffix == ".md":
        if unquote(anchor).lower() not in _anchors(root / relative):
            return "anchor does not exist"
    return None


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for _, line in _prose_lines(path):
        anchors.update(anchor.lower() for anchor in _EXPLICIT_ANCHOR.findall(line))
        heading = _HEADING.match(line)
        if not heading:
            continue
        slug = _github_slug(heading.group(2))
        seen = counts.get(slug, 0)
        counts[slug] = seen + 1
        anchors.add(slug if seen == 0 else f"{slug}-{seen}")
    return anchors


def _github_slug(heading: str) -> str:
    """Reproduce the anchor GitHub generates for a Markdown heading."""
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.lower()
    kept = "".join(
        char for char in text if char in " -_" or unicodedata.category(char)[0] in {"L", "N"}
    )
    return kept.replace(" ", "-")
