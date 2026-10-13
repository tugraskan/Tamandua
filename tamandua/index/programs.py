"""Index a main program, which the scanner does not.

SWAT+ keeps its main program in ``src/main.f90.in``, a CMake template that
becomes ``main.f90`` at build time. Two gaps kept it out of the index, and
with it every call it makes: ``callers("lsu_read_elements")`` answered none,
although ``main.f90.in:69`` calls it, and a dozen readers had no call path.

1. The scanner lists files by their last suffix, so ``main.f90.in`` -- suffix
   ``.in`` -- is never read. :func:`program_scanner` adds every file named
   for a Fortran suffix followed by ``.in``.

2. The scanner keeps a ``program`` unit's calls and control flow but nothing
   else: no declarations, assignments or I/O, and it is not a procedure, so
   the call graph has no node for it. A program's body is the same Fortran as
   a subroutine's, so :func:`program_as_subroutine` hands the scanner the
   program's text with its opening and closing statements spelled as a
   subroutine's -- every other byte and every line number unchanged -- and the
   procedure that comes back is marked ``kind = "program"``.

The rewrite is never stored or shown: the scanner reads it in place of the
file, and every location it reports still names the real file and line.
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

_PROGRAM = re.compile(r"^(\s*)program(\s+)([A-Za-z_]\w*)", re.IGNORECASE)
_END_PROGRAM = re.compile(r"^(\s*(?:\d+\s+)?end\s*)program\b", re.IGNORECASE)
_BARE_END = re.compile(r"^(\s*(?:\d+\s+)?)end(\s*)$", re.IGNORECASE)
_CONTAINS = re.compile(r"^\s*contains\s*$", re.IGNORECASE)


def _code(line: str) -> str:
    """A physical line without its comment, minding string literals."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "!":
            return line[:index]
    return line


def program_as_subroutine(text: str) -> tuple[str, list[str]]:
    """Spell each ``program`` unit as a subroutine; return the text and names.

    ``program main`` becomes ``subroutine main``, and the statement closing
    it -- ``end program [main]``, or a bare ``end`` before any ``contains`` --
    becomes ``end subroutine``. Nothing else changes, including the number of
    lines, so every line number the scanner reports is the real one. Text with
    no program comes back unchanged with no names.
    """
    if "program" not in text.lower():
        return text, []
    lines = text.splitlines(keepends=True)
    names: list[str] = []
    state = ""  # "", "program" (its body), or "contains" (after it)
    for number, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        code = _code(body)
        comment = body[len(code):]
        if not state:
            match = _PROGRAM.match(code)
            if match:
                names.append(match.group(3))
                code = f"{match.group(1)}subroutine{code[match.start(2):]}"
                lines[number] = code + comment + ending
                state = "program"
            continue
        match = _END_PROGRAM.match(code)
        if match:
            code = f"{match.group(1)}subroutine{code[match.end():]}"
            lines[number] = code + comment + ending
            state = ""
            continue
        if state == "program":
            if _CONTAINS.match(code):
                state = "contains"
                continue
            match = _BARE_END.match(code)
            if match:
                code = f"{match.group(1)}end subroutine{match.group(2)}"
                lines[number] = code + comment + ending
                state = ""
    return "".join(lines), names


class _Rewritten:
    """A source path whose text reads as rewritten; everything else is real.

    The scanner asks a path for its text and its place under the source root.
    Only the text differs, so the file and line it reports are the real ones.
    """

    def __init__(self, path: Path, text: str) -> None:
        self._path = path
        self._text = text

    def read_text(self, *args: Any, **kwargs: Any) -> str:
        return self._text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._path, name)

    def __fspath__(self) -> str:
        return os.fspath(self._path)

    def __str__(self) -> str:
        return str(self._path)


def program_scanner(
    scanner_class: type,
    config: Any,
    templates: Callable[[Path], list[Path]],
) -> Any:
    """The corpus scanner, reading templates and indexing programs.

    ``templates`` lists the template sources under a root (``main.f90.in``);
    they are scanned beside the scanner's own files, honouring its excludes.
    Subclassing keeps the scanner's whole ``scan()`` -- its cross-file
    filename and unit resolution included -- running over both.
    """

    class ProgramScanner(scanner_class):  # type: ignore[misc, valid-type]
        def _iter_source_files(self, source_root: Path) -> list[Path]:
            files = list(super()._iter_source_files(source_root))
            known = {path.resolve() for path in files}
            for path in templates(source_root):
                if path.resolve() in known:
                    continue
                relative = path.relative_to(source_root).as_posix()
                if any(fnmatch(relative, pattern) or fnmatch(path.as_posix(), pattern)
                       for pattern in self.config.exclude):
                    continue
                files.append(path)
            return sorted(files, key=lambda path: path.relative_to(source_root).as_posix())

        def _scan_file(self, path: Path, source_root: Path, project: Any) -> Any:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            rewritten, programs = program_as_subroutine(text)
            if not programs:
                return super()._scan_file(path, source_root, project)
            first_new = len(project.procedures)
            scanned = super()._scan_file(_Rewritten(path, rewritten), source_root, project)
            wanted = {name.lower() for name in programs}
            for proc in project.procedures[first_new:]:
                if proc.parent is None and proc.name.lower() in wanted:
                    proc.kind = "program"
            return scanned

    return ProgramScanner(config)
