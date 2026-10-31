"""Plain-text tables, the platform commands' default output."""

from __future__ import annotations

from collections.abc import Sequence


def table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max([len(name), *(len(row[i]) for row in rows)]) for i, name in enumerate(columns)]
    lines = [
        " | ".join(name.center(width) for name, width in zip(columns, widths, strict=True)),
        "-+-".join("-" * width for width in widths),
        *(" | ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)) for row in rows),
        f"({len(rows)} row{'' if len(rows) == 1 else 's'})",
    ]
    return "\n".join(lines) + "\n"
