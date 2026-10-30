"""Column-type inference for scaffold-generated Floe entities.

`olf source new` writes a one-column placeholder example CSV per resource (a
real source has no columns for the scaffold to know in advance). Whichever
command later builds a Floe entity for that resource -- `domain new` or
`product new` via `--input` -- infers that entity's `columns:` list from the
resource's example CSV header and data rows, string/integer/number/date, the
same way a contributor would read the tutorial and hand-write them.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

_INTEGER_PATTERN = re.compile(r"^-?\d+$")
_NUMBER_PATTERN = re.compile(r"^-?\d+\.\d+$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class InferredColumn:
    name: str
    type: str
    nullable: bool


def placeholder_example_csv(resource: str) -> str:
    """A minimal single-column example CSV for a newly declared resource."""
    return f"{resource}_id\n{resource}-001\n"


def _infer_type(values: list[str]) -> str:
    non_empty = [value for value in values if value != ""]
    if not non_empty:
        return "string"
    if all(_INTEGER_PATTERN.fullmatch(value) for value in non_empty):
        return "integer"
    if all(_INTEGER_PATTERN.fullmatch(value) or _NUMBER_PATTERN.fullmatch(value) for value in non_empty):
        return "number"
    if all(_DATE_PATTERN.fullmatch(value) for value in non_empty):
        return "date"
    return "string"


def infer_columns(csv_text: str) -> tuple[InferredColumn, ...]:
    """Infer Floe column types from a CSV's header and data rows.

    Every column is nullable=False: the scaffold cannot know real
    nullability, and Floe's `reject` policy on the generated contract makes a
    too-strict placeholder safe to loosen later, while a too-loose one would
    silently accept bad rows.
    """
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return ()
    rows = list(reader)
    columns = []
    for index, name in enumerate(header):
        values = [row[index] for row in rows if index < len(row)]
        columns.append(InferredColumn(name=name.strip(), type=_infer_type(values), nullable=False))
    return tuple(columns)
