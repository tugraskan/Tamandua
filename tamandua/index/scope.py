"""What is in scope at a line, for setting a conditional breakpoint.

The parser records where each loop starts but not where it ends, so nesting has
to be recovered from the source. Fortran makes that tractable: `do` opens and
`end do` closes, with no early exit from the block structure. Across the pinned
SWAT+ tree 647 of 648 files balance exactly; the one that does not is reported
as unresolved rather than guessed at, because a breakpoint condition built on a
wrong loop variable costs a whole compile-and-run cycle to discover.
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
        statement = raw.split("!")[0]
        if _OPEN.match(statement):
            match = _INDEX.match(statement)
            stack.append((number, match.group(1) if match else None, raw.strip()))
        elif _CLOSE.match(statement):
            if not stack:
                return None  # more closers than openers
            start, index, header = stack.pop()
            found.append(LoopScope(index=index, start=start, end=number, header=header))
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
