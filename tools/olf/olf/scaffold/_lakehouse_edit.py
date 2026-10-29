"""Deterministic text splicing for the YAML files the scaffold edits
(`lakehouse_code/governance.yml`, the Floe project's `sources.yml`).

A parse/dump round-trip through PyYAML would rewrite the whole file and
produce a hostile diff. Instead this module inserts pre-rendered,
correctly-indented text at known anchors and leaves every other byte
untouched. Correctness is not assumed from the splice: the caller always
re-validates the spliced result through the canonical loader before
committing it (see `_commit.py`).

Flow-style lists (`products: []`, `sources: [{...}]`) are detected and
converted to block style before an item is appended, since appending a
block-style item directly after an already-closed flow value is invalid
YAML. A flow-style list is required to fit on one line; a flow sequence that
spans multiple lines is not detected and is treated as unconvertible --
`_verify` in `_commit.py` then rejects the result and nothing is written. A
trailing inline comment on the converted line itself is dropped by the
conversion; a comment on its own line before/after the flow line is
preserved.
"""

from __future__ import annotations

import re

import yaml

from olf.scaffold._shared import ScaffoldError

_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z]")


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _ensure_newline_before(lines: list[str], index: int) -> None:
    """Ensure `lines[index - 1]` ends with a newline before splicing new
    content in at `index`. Property order at every level of this document
    is unconstrained (JSON Schema doesn't enforce it, and this module was
    already relaxed to accept any field order within a domain), so any
    insertion point computed as "next top-level/sibling key, or end" can
    land at true end-of-file -- not just `dashboards:`, which merely does
    so the most often. A file missing its final newline would otherwise get
    the new content concatenated directly onto its last line."""
    if index > 0 and not lines[index - 1].endswith("\n"):
        lines[index - 1] += "\n"


def _join(lines: list[str]) -> str:
    return "".join(lines)


def _top_level_span(lines: list[str], key: str, *, source_label: str) -> tuple[int, int]:
    """Return (start, end) line indices for a top-level `key:` block."""
    pattern = re.compile(rf"^{re.escape(key)}:")
    start = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    if start is None:
        raise ScaffoldError(f"{source_label}: missing top-level key {key!r}")
    end = start + 1
    while end < len(lines) and not _TOP_LEVEL_KEY.match(lines[end]):
        end += 1
    return start, end


def append_to_top_level_list(text: str, key: str, addition: str, *, source_label: str, indent: str = "  ") -> str:
    """Append `addition` right before the next top-level key after `key:`
    -- or at end-of-file if `key:` is the document's last top-level key --
    rather than always appending at end-of-file. A hand-edited file (e.g. a
    Floe contract with a custom key placed after `entities:`) is not
    guaranteed to keep any particular field last. Also converts `key:` from
    flow style to block style first if needed, the same as every other
    list this module appends to."""
    lines = _lines(text)
    start, end = _top_level_span(lines, key, source_label=source_label)
    end = _ensure_block_style(lines, start, end, indent=indent)
    _ensure_newline_before(lines, end)
    lines[end:end] = _lines(addition)
    return _join(lines)


_FLOW_LIST_KEY = re.compile(r"^(\s*)(\w[\w-]*):\s*\[.*\](\s*#.*)?\s*$")


class _BlockIndentDumper(yaml.SafeDumper):
    """PyYAML's default block-sequence style indents list items flush with
    their parent key (`products:\n- id: x`). This module's fixed indentation
    scheme instead indents them one level further (`products:\n  - id: x`),
    matching the entries this tool writes and the checked-in samples."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _render_flow_item_as_block(item: object, *, indent: str) -> list[str]:
    """Render one already-parsed flow-list item (a scalar or a mapping, e.g.
    a dashboard `{name: ..., products: [...]}`) as block-style lines at
    `indent`. Re-parses through PyYAML rather than splitting the flow text on
    commas, since a naive split breaks on commas inside a nested
    mapping/list (`[{name: x, products: [a, b]}]`)."""
    if isinstance(item, str):
        # yaml.safe_dump(..., default_style=None) lets PyYAML decide
        # whether the scalar needs quoting -- e.g. a source literally named
        # "on" must round-trip as the string "on", not the plain scalar
        # `on`, which YAML 1.1 loaders (including PyYAML's SafeLoader) read
        # back as the boolean True. `splitlines()[0]` drops the `...`
        # document-end marker safe_dump appends to a bare scalar document.
        rendered_item = yaml.safe_dump(item, default_style=None).splitlines()[0]
        return [f"{indent}- {rendered_item}\n"]
    dumped_lines = yaml.dump(item, Dumper=_BlockIndentDumper, default_flow_style=False, sort_keys=False).splitlines()
    return [f"{indent}- {dumped_lines[0]}\n"] + [f"{indent}  {extra}\n" for extra in dumped_lines[1:]]


def _ensure_block_style(lines: list[str], start: int, end: int, *, indent: str = "  ") -> int:
    """If the list at `lines[start]` is flow style (`key: [a, b]`, including
    the empty `key: []`), rewrite that one line in place as block style with
    items indented by `indent`. Returns the index right after the converted
    block -- where a caller should insert a new item, immediately following
    the existing ones. A no-op (returns `end` unchanged) when it is already
    block style.

    Only `lines[start]` is replaced, never the full `lines[start:end]` span:
    `end` is "next top-level key", so anything between the flow-list line
    and `end` -- trailing comments, blank lines -- is unrelated content that
    must survive untouched, not be deleted along with the flow line.
    """
    stripped = lines[start].rstrip("\n")
    match = _FLOW_LIST_KEY.match(stripped)
    if match is None:
        return end
    leading, key = match.group(1), match.group(2)
    items = yaml.safe_load(stripped)[key] or []
    block_lines = [f"{leading}{key}:\n"]
    for item in items:
        block_lines.extend(_render_flow_item_as_block(item, indent=indent))
    lines[start : start + 1] = block_lines
    return start + len(block_lines)
