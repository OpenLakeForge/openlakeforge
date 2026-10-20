"""`olf check docs`: the published-docs manifest and relative links.

openlakeforge.dev builds its sidebar from `docs/navigation.yaml` and renders
every page it lists, so a page that moves without the manifest following it
disappears from the site, and a dead relative link becomes a dead site link.
"""

from __future__ import annotations

import html
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

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)\s*$")
_CODE_SPAN = re.compile(r"`+[^`]*`+")
_ESCAPED_LINK = re.compile(r"\\\[[^\]]*\]\([^)]*\)")
# `](target)` rather than a full `[text](target)` match: link text can itself
# hold brackets, as a badge image nested inside a link does. The destination
# may hold one level of balanced parentheses, as `guide_(draft).md` does.
# An angle-bracket destination may hold spaces: `[guide](<guide draft.md>)`.
_INLINE_LINK = re.compile(r"\]\(\s*(?:<([^<>\n]*)>|((?:[^()\s<>]|\([^()\s]*\))*))")
# `[^1]:` is a GitHub footnote, not a link reference definition.
_REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[(?!\^)[^\]]+\]:\s*(?:<([^<>\n]*)>|(\S+))")
_OPEN_REFERENCE = re.compile(r"^\s{0,3}\[(?!\^)[^\]]+\]:\s*$")
_BLOCKQUOTE = re.compile(r"^\s{0,3}(?:>\s?)+")
_HTML_COMMENT = re.compile(r"<!--.*?-->")
# Quoted or bare values: HTML allows `<img src=chart.svg>`.
_HTML_ATTRIBUTE = re.compile(r"""\b(src|href|srcset)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'=<>`]+))""", re.IGNORECASE)
_EXPLICIT_ANCHOR = re.compile(
    r"""<a\s+(?:[^>]*\s)?(?:id|name)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'=<>`]+))""", re.IGNORECASE
)
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass(frozen=True)
class _Manifest:
    pages: tuple[str, ...]
    slugs: dict[str, str]
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
    sources = {*_markdown_under(root, "docs"), "README.md"}
    if manifest is not None:
        published = _published_pages(root, manifest)
        errors.extend(_coverage_errors(root, manifest, published))
        sources.update(page for page in published if page.endswith(".md"))
    for source in sorted(sources):
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
    slugs: dict[str, str] = {}
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
            if not isinstance(page, str):
                errors.append(f"{where}: an item must be a path or a mapping with `path`")
                continue
            pages.append(page)
            if isinstance(item, dict) and isinstance(item.get("slug"), str):
                slugs[page] = item["slug"].strip("/")
    exclude = document.get("exclude") or []
    if not isinstance(exclude, list) or not all(isinstance(entry, str) for entry in exclude):
        errors.append(f"{MANIFEST}: `exclude` must be a list of paths")
        exclude = []
    return _Manifest(tuple(pages), slugs, tuple(autogenerate), tuple(exclude)), errors


def _published_pages(root: Path, manifest: _Manifest) -> list[str]:
    """Every page the site renders, explicit items first, in manifest order."""
    generated = [
        page
        for directory in manifest.autogenerate
        if (root / directory).is_dir()
        for page in _markdown_under(root, directory)
    ]
    return [*manifest.pages, *generated]


def _coverage_errors(root: Path, manifest: _Manifest, published: list[str]) -> list[str]:
    errors = [
        f"{MANIFEST}: listed page is not a Markdown file: {page}"
        for page in manifest.pages
        if not (page.endswith(".md") and (root / page).is_file())
    ]
    errors.extend(
        f"{MANIFEST}: excluded path does not exist: {entry}"
        for entry in manifest.exclude
        if not (root / entry).exists()
    )
    errors.extend(
        f"{MANIFEST}: autogenerate directory does not exist: {directory}"
        for directory in manifest.autogenerate
        if not (root / directory).is_dir()
    )
    seen: set[str] = set()
    routes: dict[str, str] = {}
    for page in published:
        if page in seen:
            errors.append(f"{MANIFEST}: page published twice: {page}")
            continue
        seen.add(page)
        route = manifest.slugs.get(page) or _default_slug(page)
        if route in routes:
            errors.append(f"{MANIFEST}: {page} and {routes[route]} publish to the same slug: {route}")
        routes.setdefault(route, page)
    for page in sorted({*seen, *_markdown_under(root, "docs")}):
        listed = page in seen
        excluded = any(_within(page, entry) for entry in manifest.exclude)
        if listed and excluded:
            errors.append(f"{MANIFEST}: page is both published and excluded: {page}")
        elif not listed and not excluded:
            errors.append(f"{MANIFEST}: page is neither published nor excluded: {page}")
    return errors


def _default_slug(page: str) -> str:
    """The site route a page gets without an override: its path under docs/, README as the folder index."""
    path = page.removeprefix("docs/").removesuffix(".md")
    if path == "README" or path.endswith("/README"):
        path = path.removesuffix("README").rstrip("/")
    return path
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
    carried = ""
    for line_number, line in _prose_lines(path):
        # Hard-wrapped prose can break a link right after `](`, or a reference
        # definition right after `[label]:`, leaving the destination at the
        # start of the next line.
        text = carried + _ESCAPED_LINK.sub("", _CODE_SPAN.sub("", line)).lstrip()
        if _OPEN_REFERENCE.match(text):
            carried = text.strip() + " "
            continue
        carried = "](" if text.rstrip().endswith("](") else ""
        for pattern in (_INLINE_LINK, _REFERENCE_DEFINITION):
            links.extend((line_number, angled or bare) for angled, bare in pattern.findall(text))
        for attribute, *quoted_or_bare in _HTML_ATTRIBUTE.findall(text):
            value = next(part for part in quoted_or_bare if part)
            candidates = [value]
            if attribute.lower() == "srcset":
                candidates = [part.split()[0] for part in value.split(",") if part.split()]
            links.extend((line_number, candidate) for candidate in candidates)
    return [(line_number, target) for line_number, target in links if target and not _SCHEME.match(target)]


def _prose_lines(path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    fence: str | None = None
    in_comment = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if fence is None:
            # HTML comment contents are raw HTML, never parsed as Markdown.
            if in_comment:
                end = line.find("-->")
                if end < 0:
                    continue
                line, in_comment = line[end + 3 :], False
            line = _HTML_COMMENT.sub("", line)
            if "<!--" in line:
                line, in_comment = line[: line.index("<!--")], True
        opener = _FENCE.match(line)
        # A fence closes only on the same character, at least as long as the
        # opener: a ```` block can show a ``` block inside it.
        if opener and fence is None:
            fence = opener.group(1)
            continue
        if opener and fence and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
            fence = None
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
        if unquote(anchor) not in _anchors(root / relative):
            return "anchor does not exist"
    return None


def _anchors(path: Path) -> set[str]:
    # Explicit ids keep their case: a fragment must match one exactly.
    anchors: set[str] = set()
    headings: set[str] = set()
    previous = ""
    for _, quoted_line in _prose_lines(path):
        line = _BLOCKQUOTE.sub("", quoted_line)
        anchors.update(next(part for part in match if part) for match in _EXPLICIT_ANCHOR.findall(line))
        atx = _HEADING.match(line)
        # A paragraph line followed by `===` or `---` is a Setext heading.
        setext = bool(_SETEXT_UNDERLINE.match(line)) and _is_paragraph_line(previous)
        text = atx.group(2) if atx else previous
        previous = line
        if not (atx or setext):
            continue
        # GitHub skips a suffix an earlier heading already took: `Notes`,
        # `Notes-1`, `Notes` gives notes, notes-1, notes-2.
        base = slug = _github_slug(text.strip())
        suffix = 0
        while slug in headings:
            suffix += 1
            slug = f"{base}-{suffix}"
        headings.add(slug)
    return anchors | headings


def _is_paragraph_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not _HEADING.match(line) and not re.match(r"[|>*+<-]|\d+[.)]\s", stripped)


def _github_slug(heading: str) -> str:
    """Reproduce the anchor GitHub generates for a Markdown heading."""
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = html.unescape(text)
    text = text.lower()
    kept = "".join(
        char for char in text if char in " -_" or unicodedata.category(char)[0] in {"L", "N"}
    )
    return kept.replace(" ", "-")
