"""Query SWAT+ run output without reading it into context.

A run's output only exists on the machine that produced it, so no shipped index
can answer questions about it. This reads those files and returns the *answer* --
a summary of a series, not the series -- because returning the data costs the
same as the assistant reading the file itself.

Correctness first: 9 of the 25 output files in the Ames reference project have a
header row whose field count does not match the data rows. Indexing a column by
position there returns the wrong number with no sign that anything is wrong. This
module detects the mismatch and refuses to guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

#: Rows scanned when deciding whether the header can be trusted.
_PROBE_ROWS = 20


class OutputError(RuntimeError):
    """Raised with an actionable message when a file cannot be read safely."""


@dataclass
class Layout:
    """How one output file is structured, worked out by inspection."""

    path: Path
    header_line: int | None
    units_line: int | None
    first_data_line: int
    columns: tuple[str, ...]
    trusted: bool
    note: str = ""

    def index_of(self, column: str) -> int:
        key = column.strip().lower()
        for i, name in enumerate(self.columns):
            if name.lower() == key:
                return i
        raise OutputError(
            f"no column {column!r} in {self.path.name}. "
            f"available: {', '.join(self.columns[:24])}"
            + (" ..." if len(self.columns) > 24 else "")
        )


@dataclass
class Summary:
    """What a query returns by default: the shape of a series, not the series."""

    column: str
    n: int
    first: float | None = None
    last: float | None = None
    minimum: float | None = None
    minimum_at: str | None = None
    maximum: float | None = None
    maximum_at: str | None = None
    negatives: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        if self.n == 0:
            return f"{self.column}: no rows matched"
        parts = [
            f"{self.column}: n={self.n}",
            f"first={self.first:g}",
            f"last={self.last:g}",
            f"min={self.minimum:g}@{self.minimum_at}",
            f"max={self.maximum:g}@{self.maximum_at}",
        ]
        if self.first is not None and self.last is not None and self.first != self.last:
            parts.append("trend=" + ("declining" if self.last < self.first else "rising"))
        if self.negatives:
            parts.append(f"negatives={self.negatives}")
        return " ".join(parts) + ("  [" + "; ".join(self.warnings) + "]" if self.warnings else "")


def _numeric(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_layout(path: Path) -> Layout:
    """Work out a file's header, units row, and whether columns can be trusted.

    SWAT+ output is conventionally title / header / units / data, but not
    universally -- some files carry a group caption instead of per-column names,
    and some declare more header names than they ever write.
    """
    if not path.is_file():
        raise OutputError(f"no such output file: {path}")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        raise OutputError(f"{path.name} has no data rows")

    # The first row that parses as mostly numbers starts the data.
    first_data = None
    for i, line in enumerate(lines[:12]):
        tokens = line.split()
        if len(tokens) >= 3 and sum(_numeric(t) is not None for t in tokens) >= len(tokens) / 2:
            first_data = i
            break
    if first_data is None or first_data == 0:
        raise OutputError(f"{path.name}: could not find a data row in the first 12 lines")

    header_line = first_data - 1
    units_line = None
    # A units row sits between header and data and is mostly non-numeric.
    if first_data >= 2:
        candidate = lines[first_data - 1].split()
        prev = lines[first_data - 2].split()
        if candidate and prev and not any(_numeric(t) is not None for t in candidate):
            if len(prev) >= len(candidate):
                header_line, units_line = first_data - 2, first_data - 1

    columns = tuple(lines[header_line].split()) if header_line >= 0 else ()

    widths = {len(lines[i].split()) for i in range(first_data, min(first_data + _PROBE_ROWS, len(lines)))
              if lines[i].strip()}
    data_width = max(widths) if widths else 0

    trusted = bool(columns) and len(columns) == data_width
    note = "" if trusted else (
        f"header declares {len(columns)} names but rows have {data_width} fields; "
        "columns cannot be matched by position"
    )
    return Layout(path, header_line, units_line, first_data, columns, trusted, note)


def query(
    path: Path,
    column: str,
    where: dict[str, str] | None = None,
    label_by: str | None = None,
    raw: bool = False,
) -> Summary | list[tuple[str, float]]:
    """Summarise one column, optionally filtered.

    ``where`` matches column name to an exact string value. ``label_by`` names
    the column used to report where the min and max occurred (default: the first
    column, conventionally ``jday``).
    """
    layout = read_layout(path)
    if not layout.trusted:
        raise OutputError(f"{path.name}: {layout.note}")

    col = layout.index_of(column)
    label = layout.index_of(label_by) if label_by else 0
    filters = [(layout.index_of(k), v.strip()) for k, v in (where or {}).items()]

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    series: list[tuple[str, float]] = []
    skipped = 0

    for line in lines[layout.first_data_line:]:
        tokens = line.split()
        if len(tokens) <= col:
            continue
        if any(len(tokens) <= i or tokens[i] != v for i, v in filters):
            continue
        value = _numeric(tokens[col])
        if value is None:
            skipped += 1
            continue
        series.append((tokens[label] if label < len(tokens) else "?", value))

    if raw:
        return series

    warnings = (f"{skipped} non-numeric rows skipped",) if skipped else ()
    if not series:
        return Summary(column=column, n=0, warnings=warnings)

    lo = min(series, key=lambda p: p[1])
    hi = max(series, key=lambda p: p[1])
    return Summary(
        column=column,
        n=len(series),
        first=series[0][1],
        last=series[-1][1],
        minimum=lo[1],
        minimum_at=lo[0],
        maximum=hi[1],
        maximum_at=hi[0],
        negatives=sum(1 for _, v in series if v < 0),
        warnings=warnings,
    )
