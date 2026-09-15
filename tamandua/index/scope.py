"""What is in scope at a line, for setting a conditional breakpoint.

The parser recorded where each loop starts but not where it ends, so nesting is
recovered from the source once, while the index is built. Fortran makes that tractable: `do` opens and
`end do` closes, with no early exit from the block structure. Across the pinned
SWAT+ tree 647 of 648 files balance exactly; the one that does not is reported
as unresolved rather than guessed at, because a breakpoint condition built on a
wrong loop variable costs a whole compile-and-run cycle to discover. Query-time
scope lookups use the stored ranges and never reopen this source path.

As of the parser pinned on 2026-09-15 that first sentence is out of date:
`ControlStep` now carries `end_line`, `depth`, `parent_id` and `branch_of`, so
the block tree is now a parser fact too. Comparing the two against SWAT+
62.0.0 found a defect here: this scan matched `do` only at the start of a
line, so it missed all 172 loops SWAT+ packs onto one line behind a `;`
(`buf = 0.0; do k = 1, n; buf(k) = ...; end do`, in soil_nutcarb_write.f90 and
soil_carbvar_write.f90). Every line inside one reported no scope at all, which
is the silent-wrong-answer this module exists to avoid. `_split_statements`
fixes it; `test_scope.py` guards it.

After the fix the two agree exactly: 2,833 loops in common, no disagreement on
any end line, and none invented. The parser finds 19 more, all in
gwflow_pond.f90 -- the one unbalanced file, still reported unresolved here
rather than guessed at. Switching wholesale would trade that safety net for
the parser's block tracker and is not obviously worth it; see docs/status.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Opens a block: `do`, `do while (...)`, `do i = 1, n`, or a named variant.
#: The negative lookahead excludes the labelled form `do 100 i = ...`, which is
#: closed by a labelled statement rather than `end do` -- absent from this tree.
_OPEN = re.compile(r"^\s*(?:\w+\s*:\s*)?do\b(?!\s*\d)", re.IGNORECASE)
_INDEX = re.compile(r"^\s*(?:\w+\s*:\s*)?do\b(?!\s*while)\s*(\w+)\s*=", re.IGNORECASE)
_WHILE = re.compile(r"^\s*(?:\w+\s*:\s*)?do\s+while\s*\((.*)\)\s*$", re.IGNORECASE)
_CLOSE = re.compile(r"^\s*end\s*do\b", re.IGNORECASE)

#: A quoted string, so a `;` inside one is not mistaken for a separator.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


@dataclass(frozen=True)
class LoopScope:
    """One loop enclosing a line."""

    index: str | None
    start: int
    end: int
    header: str

    @property
    def kind(self) -> str:
        return "counted" if self.index else "while"


def _split_statements(line: str) -> list[str]:
    """The `;`-separated statements on one physical line.

    SWAT+ packs whole loops onto one line -- `buf = 0.0; do k = 1, n; buf(k) =
    soil1(j)%str(k)%c; end do` in soil_nutcarb_write.f90 -- so a scan that only
    looks at the start of a line sees neither the `do` nor its `end do`. A `;`
    inside a quoted string is not a separator.
    """
    if ";" not in line:
        return [line]
    masked = _QUOTED.sub(lambda m: "\x00" * len(m.group()), line)
    parts: list[str] = []
    start = 0
    for position, character in enumerate(masked):
        if character == ";":
            parts.append(line[start:position])
            start = position + 1
    parts.append(line[start:])
    return parts


def loop_ranges(source_file: Path) -> list[LoopScope] | None:
    """Every loop in a file with its start and end line.

    Returns ``None`` when the file's `do`/`end do` pairs do not balance, which
    means the nesting could not be recovered and no scope should be reported
    for it.
    """
    try:
        lines = source_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    stack: list[tuple[int, str | None, str]] = []
    found: list[LoopScope] = []
    for number, raw in enumerate(lines, start=1):
        body = raw.split("!")[0]
        statements = _split_statements(body)
        inline = len(statements) > 1
        for statement in statements:
            if _OPEN.match(statement):
                match = _INDEX.match(statement)
                # On a single-statement line the whole line is the header, which
                # keeps any trailing comment; on a packed line only the `do`
                # statement is.
                header = statement.strip() if inline else raw.strip()
                stack.append((number, match.group(1) if match else None, header))
            elif _CLOSE.match(statement):
                if not stack:
                    return None  # more closers than openers
                start, index, header = stack.pop()
                found.append(
                    LoopScope(index=index, start=start, end=number, header=header))
    if stack:
        return None  # unclosed loops
    return sorted(found, key=lambda loop: loop.start)


def scope_at(source_file: Path, line: int) -> list[LoopScope] | None:
    """The loops enclosing ``line``, outermost first.

    An empty list means the line sits in no loop -- which is itself the answer
    when a routine is called once per object and its index comes from the
    caller, as `aquifer_output` is for `iaq`.
    """
    ranges = loop_ranges(source_file)
    if ranges is None:
        return None
    return [loop for loop in ranges if loop.start <= line <= loop.end]


def condition_for(scopes: list[LoopScope], extra: dict[str, str] | None = None) -> str:
    """A Fortran condition pinning every loop index, for a debugger.

    Values are left as placeholders rather than invented -- the point is to
    hand over the right variable names, which is the part that is hard to work
    out and easy to get wrong.
    """
    terms = [f"{loop.index} == <value>" for loop in scopes if loop.index]
    terms += [f"{name} == {value}" for name, value in (extra or {}).items()]
    return " .and. ".join(terms) if terms else ""
