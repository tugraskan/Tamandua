"""Build a facts-only index of SWAT+ Fortran source.

Everything here comes from static analysis performed by ``swatplus-reference-corpus``
-- procedure locations, call graphs, file I/O with unit numbers, variable
assignments, loop headers. No prose, nothing written by a model, so the index
can be rebuilt from any checkout in seconds and cannot describe a tree it did
not read.

This module is the single implementation. ``scripts/build_index.py`` renders it
to a checked-in file; ``tamandua.mcp.server`` serves the same objects
over MCP. Neither re-parses anything itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tamandua.config import resolve_checkout
from tamandua.index.analyze import analyze_project
from tamandua.index.diagnostics import ScannerWarning, scan_source_warnings
from tamandua.index.scope import LoopScope, condition_for, loop_ranges

#: Bumped when the extracted fields change shape, so a stale index is
#: recognisable as stale rather than silently mis-read.
#: Format 4 adds ``declaration`` to derived-type fields, matching what
#: module variables already carried -- without it, a component like
#: ``soil_profile%phys`` reported only its bare type name, with no way to
#: tell a scalar from ``dimension(:), allocatable``.
#: Format 5 adds ``comparisons``: the equality tests between two variables in
#: an ``if`` condition, which is how SWAT+ joins one input file's column to
#: another file's rows -- by matching names at run time, declared nowhere --
#: and ``copies``, the assignments that carry a column's value to one.
INDEX_FORMAT_VERSION = "5"

# Assignment targets: `name`, `name(i)`, `name%comp`, `a%b(i)%c = ...`.
# The negative lookahead keeps `==` comparisons out.
# The parser pinned on 2026-09-15 reports `target`, `target_root` and
# `expression` on each assignment, which makes re-deriving the target from
# `raw` here redundant. Measured against SWAT+ 62.0.0: both agree on all
# 21,770 assignments, and neither finds a target the other misses. So this is
# a safe refactor with no behaviour change -- and, being no behaviour change,
# not urgent. Left as is deliberately.
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*(?:\s*%\s*\w+|\s*\([^=]*?\))*)\s*=(?!=)")

#: Array subscripts carry no identity -- `aqu_d(iaq)` and `aqu_d(3)` are the
#: same variable -- so they are stripped, leaving the `%`-separated field path.
_SUBSCRIPT_RE = re.compile(r"\([^()]*\)")

# SWAT+ opens its output files through a helper rather than a bare `open`
# statement: `call open_output_file(2520, "aquifer_day.txt", 1500)`. Without
# resolving these, every output file's writers are recorded only as
# `unit_2520` and "which routine writes aquifer_day.txt" cannot be answered.
_OPEN_HELPER_RE = re.compile(
    r"""open_output_file\s*\(\s*(\d+)\s*,\s*["']([^"']+)["']""",
    re.IGNORECASE,
)

# The `parameter` attribute, matched only in the attribute list ahead of the
# `::`. Matching the whole declaration would also hit the word inside an
# initialiser string or a trailing `!` comment.
_PARAMETER_RE = re.compile(r"\bparameter\b", re.IGNORECASE)

# `type (input_aqu)` / `type(input_aqu)` -- a variable's declared derived
# type, which is the first hop from `in_aqu` to the component defaults.
_TYPE_DECL_RE = re.compile(r"\s*type\s*\(\s*([A-Za-z_]\w*)\s*\)", re.IGNORECASE)

#: A positioning statement that names its unit without parentheses,
#: ``backspace 107``. The parenthesised form is the scanner's; this one is not.
_BARE_POSITIONING_RE = re.compile(
    r"^\s*(?:backspace|rewind|endfile)\s+(\w+)\s*$", re.IGNORECASE)

#: The opening of an ``if`` or ``else if`` condition.
_IF_RE = re.compile(r"^\s*(?:else\s*)?if\s*\(", re.IGNORECASE)
#: Logical connectives, which separate the comparisons inside one condition.
_LOGICAL_RE = re.compile(r"\.(?:and|or|not|eqv|neqv)\.", re.IGNORECASE)
#: Equality and inequality in both spellings. `<=` and `>=` are not matched.
_EQUALITY_RE = re.compile(r"==|/=|\.eq\.|\.ne\.", re.IGNORECASE)
#: Intrinsics that change a string's padding and nothing else, so a
#: comparison through them still compares the variable's value.
_PADDING_RE = re.compile(r"^(?:trim|adjustl|adjustr)\s*\(", re.IGNORECASE)

# Keep this in step with the corpus scanner. Suffix matching is deliberately
# case-insensitive: Git checkouts on Linux distinguish ``.F90`` from ``.f90``.
FORTRAN_SUFFIXES = {".f90", ".for", ".f", ".f95"}


class IndexError_(RuntimeError):
    """Raised with an actionable message when the index cannot be built."""


#: Prefix of the fingerprint line in a rendered index's provenance block.
FINGERPRINT_KEY = "source_fingerprint: "
#: Prefix of the parser revision in a rendered index's provenance block.
PARSER_COMMIT_KEY = "parser_commit: "
#: Prefix of the facts projection version in rendered provenance.
INDEX_FORMAT_KEY = "index_format: "


@dataclass(frozen=True)
class Provenance:
    """Where an index came from, so a stale one can be spotted.

    ``source_fingerprint`` hashes the *working tree*, not the commit. Questions
    get asked about code that is being edited and has not been committed, so a
    commit hash would report an index as current at exactly the moment it is
    wrong.
    """

    source_path: str
    source_commit: str | None
    source_describe: str | None
    source_fingerprint: str
    generated_at: str
    format_version: str
    parser_commit: str | None
    #: Only a real compiler result may change this from ``not_checked``.
    compile_status: str = "not_checked"

    def as_lines(self) -> list[str]:
        return [
            f"source_path: {self.source_path}",
            f"source_commit: {self.source_commit or 'unknown'}",
            f"source_version: {self.source_describe or 'unknown'}",
            f"{FINGERPRINT_KEY}{self.source_fingerprint}",
            f"generated_at: {self.generated_at}",
            f"{INDEX_FORMAT_KEY}{self.format_version}",
            f"parser_commit: {self.parser_commit or 'unknown'}",
            f"compile_status: {self.compile_status}",
        ]


@dataclass
class IOUse:
    """One I/O statement: which routine touched which file, on which unit.

    ``fields`` are the variables the statement itself reads or writes. They are
    what is actually in scope at that line -- which a loop listing cannot give
    you when the routine has no loop of its own and is called once per object
    (``aquifer_output`` writes ``aqu_d(iaq)`` with ``iaq`` set by its caller).
    That makes them the terms of a conditional breakpoint.
    """

    file: str
    op: str
    unit: str | None
    procedure: str
    line: int
    fields: tuple[str, ...] = ()


@dataclass
class Loop:
    procedure: str
    line: int
    header: str
    end_line: int | None = None
    index: str | None = None


@dataclass
class Comparison:
    """One equality test between two variables in an ``if`` condition.

    SWAT+ never declares that a column of one input file names a row of
    another. It finds out at run time, by searching:

        do ilum = 1, db_mx%landuse
          if (hru_db(i)%dbsc%land_use_mgt == lum(ilum)%name) then

    This is that statement, kept: both operands as written, and the loops the
    line sits in. The operands keep their subscripts because the subscript is
    what shows which side is being searched -- ``lum(ilum)`` is indexed by the
    loop, ``hru_db(i)`` is not. Operands are whitespace-free, with any
    ``trim``/``adjustl``/``adjustr`` removed; only comparisons whose two sides
    are both variables declared in scope are kept, so a literal
    (``== "null"``) or a function result is not.
    """

    procedure: str
    line: int
    left: str
    #: ``==`` or ``/=``; ``.eq.`` and ``.ne.`` are stored as these.
    op: str
    right: str
    #: Enclosing loops, outermost first, each by index variable or, for an
    #: uncounted loop, its header. ``None`` when the file's loops could not be
    #: resolved, which is not the same as "in no loop".
    loops: tuple[str, ...] | None = ()
    #: Set when the test is made in a called routine on its dummy arguments:
    #: ``search:22``, where ``search`` compares ``sch(nn) == cfind``. This
    #: record is then the call site, ``line`` its line, and the operands the
    #: actual arguments -- an array passed whole written ``wst_n(:)``, since
    #: the callee compares against its elements.
    via: str | None = None

    @property
    def left_path(self) -> str | None:
        return field_path(self.left)

    @property
    def right_path(self) -> str | None:
        return field_path(self.right)


@dataclass
class Copy:
    """One assignment of a variable to another, ``mgt = sched(isched)%mgt_ops(iop)``.

    Kept only where the assigned variable, or a structure containing it, is
    an operand of a :class:`Comparison` -- the value a comparison tests may
    have been copied there from a column. Pointer association (``=>``) is
    kept too, with ``op`` saying which.
    """

    procedure: str
    line: int
    target: str
    #: ``=`` or ``=>``.
    op: str
    source: str

    @property
    def target_path(self) -> str | None:
        return field_path(self.target)

    @property
    def source_path(self) -> str | None:
        return field_path(self.source)


@dataclass
class Use:
    """One module import made by a procedure."""

    module: str
    only: tuple[str, ...]
    line: int
    intrinsic: bool = False


@dataclass
class VariableDeclaration:
    """One argument or local declaration, including its inline source doc."""

    name: str
    declaration: str | None
    line: int
    vartype: str | None
    initial: str | None
    units: str | None
    description: str | None


@dataclass
class ModuleVariable:
    """One variable declared in a module body, outside any procedure.

    The third class of Fortran name, and the one this index used to drop.
    Derived-type components and procedure arguments/locals were both kept;
    module-level variables were parsed and discarded, although SWAT+ keeps
    nearly everything in module-level instances of derived types -- `aqu_d`,
    `in_aqu`, `sp_ob`. The index could describe the type `aquifer_dynamic`
    in full while unable to say that `aqu_d` existed or which module owned it.

    ``module`` is half the identity, not decoration: 15 of the 2,003 distinct
    bare names in SWAT+ 62.0.0 are declared in more than one module, so a
    lookup keyed on the name alone answers confidently and wrongly 15 times.
    ``hsaltb_d`` is declared in both `output_ls_salt_module` and `salt_module`.

    ``is_parameter`` is stored rather than left for a consumer to re-derive
    from ``declaration``. A ``parameter`` is a compile-time constant with no
    runtime storage, so anything projecting a debugger symbol map has to
    exclude it, and should not need a Fortran attribute parser to do it. 10 of
    the 2,018 declarations carry the attribute. That is deliberately a wider
    filter than the 4 declarations an ifx build showed with no object symbol:
    whether the compiler emitted a symbol for a constant or not, it is not a
    variable whose value can be watched change.
    """

    name: str
    module: str
    vartype: str | None
    declaration: str | None
    line: int
    units: str | None
    description: str | None
    initial: str | None = None
    is_parameter: bool = False

    @property
    def path(self) -> str:
        """``module%name`` -- unique where the bare name is not."""
        return f"{self.module}%{self.name}"


@dataclass
class SelectCase:
    """The closed vocabulary parsed from one ``select case`` block."""

    subject: str
    cases: tuple[str, ...]
    line: int


@dataclass
class WriterStatement:
    """One assignment and the complete logical statement parsed from source."""

    procedure: str
    line: int
    raw: str


@dataclass
class Field:
    """One component of a derived type, with whatever the source says it means.

    SWAT+ documents most fields inline -- `real :: rchrg = 0.  !mm | recharge
    entering aquifer from other objects`. 4,003 of 6,904 fields carry a
    description and 2,428 carry units, all parsed, none written by a model.
    That is a searchable route from ordinary words to an identifier without
    embeddings or a curated glossary.
    """

    type_name: str
    name: str
    vartype: str | None
    units: str | None
    description: str | None
    location: str
    declaration: str | None = None

    @property
    def path(self) -> str:
        return f"{self.type_name}%{self.name}"


@dataclass
class DerivedType:
    name: str
    module: str | None
    location: str
    fields: list[Field] = field(default_factory=list)


@dataclass
class Procedure:
    """One procedure, reduced to the facts consumers actually read.

    The parser's own procedure object carries the whole parse -- assignments,
    control steps, raw I/O statements -- and every one of those is consumed
    during the build into ``writers``, ``loops`` and ``io_by_file``, then never
    read again. Only this surface survives into queries.

    Keeping just it is what lets an index be serialised and served with no
    ``swatplus_reference`` present at all: the parser is a build-time
    dependency, not a runtime one.
    """

    name: str
    module: str | None
    #: Human-readable site, e.g. ``aquifer_module.f90:22``.
    location: str
    #: Source file the procedure was found in; what ``scope_at`` needs.
    path: str
    called_by: list[str] = field(default_factory=list)
    #: Resolved callees only. An unresolved call names no procedure to look up.
    callees: list[str] = field(default_factory=list)
    uses: list[Use] = field(default_factory=list)
    arguments: list[VariableDeclaration] = field(default_factory=list)
    locals: list[VariableDeclaration] = field(default_factory=list)
    select_cases: list[SelectCase] = field(default_factory=list)


@dataclass
class SourceIndex:
    """Facts extracted from one SWAT+ checkout."""

    provenance: Provenance
    procedures: dict[str, Procedure] = field(default_factory=dict)
    io_by_file: dict[str, list[IOUse]] = field(default_factory=lambda: defaultdict(list))
    io_by_unit: dict[str, list[IOUse]] = field(default_factory=lambda: defaultdict(list))
    writers: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    writer_statements: dict[str, list[WriterStatement]] = field(
        default_factory=lambda: defaultdict(list)
    )
    loops: dict[str, list[Loop]] = field(default_factory=lambda: defaultdict(list))
    #: Keyed like ``loops``, by lowercased procedure name.
    comparisons: dict[str, list[Comparison]] = field(
        default_factory=lambda: defaultdict(list))
    #: Keyed by the lowercased field path assigned, like ``writers``.
    copies: dict[str, list[Copy]] = field(default_factory=lambda: defaultdict(list))
    types: dict[str, DerivedType] = field(default_factory=dict)
    #: Keyed ``(module, name)``, both lowercased. A flat name key would be
    #: wrong for the 15 names SWAT+ declares in more than one module.
    module_variables: dict[tuple[str, str], ModuleVariable] = field(default_factory=dict)
    call_paths: dict[str, list[list[str]]] = field(default_factory=lambda: defaultdict(list))
    scanner_warnings: list[ScannerWarning] = field(default_factory=list)
    unresolved_loop_files: set[str] = field(default_factory=set)

    # -- lookups; every consumer goes through these ----------------------

    def procedure(self, name: str) -> Procedure | None:
        return self.procedures.get(name.strip().lower())

    def callers_of(self, name: str) -> list[str]:
        proc = self.procedure(name)
        return sorted(set(proc.called_by)) if proc else []

    def callees_of(self, name: str) -> list[str]:
        proc = self.procedure(name)
        return sorted(set(proc.callees)) if proc else []

    def io_for_file(self, file: str) -> list[IOUse]:
        return self.io_by_file.get(file.strip().lower(), [])

    def io_for_unit(self, unit: str, op: str = "") -> list[IOUse]:
        uses = self.io_by_unit.get(unit.strip(), [])
        return [u for u in uses if u.op == op] if op else list(uses)

    def writers_of(self, variable: str) -> list[str]:
        return sorted(set(self.writers.get(variable.strip().lower(), [])))

    def writer_details(self, variable: str) -> list[dict[str, Any]]:
        """Every write site with its expression when the RHS sidecar is loaded."""
        key = variable.strip().lower()
        raw_by_site = {
            f"{item.procedure}:{item.line}": item.raw
            for item in self.writer_statements.get(key, [])
        }
        return [
            {"at": site, "expression": raw_by_site.get(site, "unavailable")}
            for site in self.writers_of(key)
        ]

    def loops_in(self, procedure: str) -> list[Loop]:
        return self.loops.get(procedure.strip().lower(), [])

    def comparisons_in(self, procedure: str) -> list[Comparison]:
        return self.comparisons.get(procedure.strip().lower(), [])

    def copies_to(self, variable: str) -> list[Copy]:
        return self.copies.get(variable.strip().lower(), [])

    def derived_type(self, name: str) -> DerivedType | None:
        return self.types.get(name.strip().lower())

    def module_variable(self, module: str, name: str) -> ModuleVariable | None:
        """One module variable by its full identity."""
        return self.module_variables.get(
            (module.strip().lower(), name.strip().lower()))

    def module_variables_named(self, name: str) -> list[ModuleVariable]:
        """Every module that declares ``name`` -- the candidate set.

        Returning a list rather than a winner is the point. Intel mangles a
        module variable as ``<module>_mp_<name>``, so a consumer building a
        symbol map has to pick a module; the existing generator keyed on the
        bare name and resolved duplicates by sorting the mangled symbols,
        which means the winner was whichever *module name* sorted first --
        unrelated to the scope the question was asked from. A caller that
        knows its frame's ``use`` statements can choose correctly; one that
        does not should see that the answer is ambiguous.
        """
        needle = name.strip().lower()
        return sorted(
            (item for (_, key), item in self.module_variables.items()
             if key == needle),
            key=lambda item: item.module.lower(),
        )

    def module_variables_in(self, module: str) -> list[ModuleVariable]:
        """Every variable a module declares, in source order."""
        needle = module.strip().lower()
        return sorted(
            (item for (key, _), item in self.module_variables.items()
             if key == needle),
            key=lambda item: item.line,
        )

    def search_module_variables(self, text: str, limit: int = 25) -> list[ModuleVariable]:
        """Find a module variable from ordinary words or a partial name.

        The same three-band ranking as :meth:`search_fields`: an exact name,
        then a name containing the text, then a documented meaning mentioning
        it. ``search_fields`` reaches `aquifer_dynamic%rchrg` -- the type's
        component -- while the name a developer types is `aqu_d%rchrg`, whose
        root is a module variable. This is the other half of that lookup.
        """
        needle = text.strip().lower()
        if not needle:
            return []
        exact: list[ModuleVariable] = []
        partial: list[ModuleVariable] = []
        described: list[ModuleVariable] = []
        for item in self.module_variables.values():
            name = item.name.lower()
            if name == needle:
                exact.append(item)
            elif needle in name:
                partial.append(item)
            elif needle in (item.description or "").lower():
                described.append(item)
        def order(item: ModuleVariable) -> tuple[str, str]:
            return (item.name.lower(), item.module.lower())

        return (sorted(exact, key=order) + sorted(partial, key=order)
                + sorted(described, key=order))[:limit]

    def colliding_module_variable_names(self) -> dict[str, list[str]]:
        """Bare names declared in more than one module, to the modules.

        The measured answer for SWAT+ 62.0.0 is 15 names. A consumer
        projecting a symbol map needs to know which lookups it cannot do on
        the bare name alone.
        """
        by_name: dict[str, list[str]] = defaultdict(list)
        for item in self.module_variables.values():
            by_name[item.name.lower()].append(item.module)
        return {name: sorted(modules) for name, modules in sorted(by_name.items())
                if len(modules) > 1}

    def paths_to(self, procedure: str) -> list[list[str]]:
        """Execution paths from an entry point down to this procedure."""
        return self.call_paths.get(procedure.strip().lower(), [])

    def warnings_for_procedure(self, procedure: str) -> list[ScannerWarning]:
        """Warnings attached to a procedure or to its containing source file."""

        proc = self.procedure(procedure)
        if proc is None:
            return []
        wanted = proc.name.lower()
        return [
            warning for warning in self.scanner_warnings
            if ((warning.procedure or "").lower() == wanted)
            or (warning.procedure is None and warning.file == proc.path)
        ]

    def scope_at(self, source_file: str, line: int) -> list[LoopScope] | None:
        """Loops enclosing a line, from stored facts; ``None`` if unresolved."""
        normalised = source_file.replace("\\", "/")
        if normalised in self.unresolved_loop_files:
            return None
        ranges = [
            LoopScope(index=item.index, start=item.line, end=item.end_line,
                      header=item.header)
            for items in self.loops.values()
            for item in items
            if item.end_line is not None
            and (proc := self.procedure(item.procedure)) is not None
            and proc.path.replace("\\", "/") == normalised
            and item.line <= line <= item.end_line
        ]
        return sorted(ranges, key=lambda item: item.start)

    def breakpoint_for(self, variable: str) -> dict[str, Any]:
        """Where to stop to watch a variable, and on what condition.

        Ties the pieces together: who assigns it, what loops enclose that line,
        and which caller supplies the index when the routine has no loop of its
        own -- the case that defeats a loop listing, since `aquifer_output`
        writes `aqu_d(iaq)` with `iaq` set by `command`.
        """
        sites = self.writers_of(variable)
        if not sites:
            return {"variable": variable, "found": False}

        stops = []
        for site in sites:
            name, _, line_text = site.rpartition(":")
            proc = self.procedure(name)
            if proc is None or not line_text.isdigit():
                continue
            line = int(line_text)
            scopes = self.scope_at(proc.path, line)
            stop: dict[str, Any] = {
                "at": f"{proc.path}:{line}",
                "procedure": proc.name,
            }
            if scopes is None:
                stop["scope"] = "unresolved"
            elif scopes:
                stop["loops"] = [
                    {"index": s.index, "lines": f"{s.start}-{s.end}", "header": s.header}
                    for s in scopes
                ]
                stop["condition"] = condition_for(scopes)
            else:
                stop["loops"] = []
                # No loop here: the index comes from whoever called this.
                stop["callers"] = self.callers_of(proc.name)
            stops.append(stop)
        return {"variable": variable, "stops": stops}

    def search_fields(self, text: str, limit: int = 25) -> list[Field]:
        """Find fields whose documented meaning mentions ``text``.

        Substring search over what the source itself says a field is for, which
        is what makes "recharge" reach `aquifer_dynamic%rchrg`.

        Ranked in three bands: an exact field name, then a name containing the
        text, then a description mentioning it. Without the middle band
        separated out, searching `rchrg` returned `rchrg_prev` ahead of `rchrg`
        -- a known identifier buried under a longer one that merely contains it.
        """
        needle = text.strip().lower()
        if not needle:
            return []
        exact: list[Field] = []
        partial: list[Field] = []
        described: list[Field] = []
        for derived in self.types.values():
            for item in derived.fields:
                name = item.name.lower()
                if name == needle:
                    exact.append(item)
                elif needle in name:
                    partial.append(item)
                elif needle in (item.description or "").lower():
                    described.append(item)
        return (exact + partial + described)[:limit]


# -------------------------------------------------------------- discovery

def field_path(target: str) -> str | None:
    """Normalise an assignment target to its field path.

    ``aqu_d(iaq)%rchrg`` -> ``aqu_d%rchrg``. Keeping the whole path rather than
    the root symbol is what makes a variable findable by the name people
    actually use: SWAT+ keeps nearly everything in derived types, so reducing to
    the root buries ``rchrg`` among forty unrelated writes to ``aqu_d``, and a
    search for the name it is known by finds only where it is zeroed.

    A row keyed on the full path stays greppable both ways -- ``^aqu_d%`` for
    every field of a structure, ``rchrg|`` for one field wherever it lives.

    Returns ``None`` for anything that is not a plain field path, so pointer
    dereferences and expressions are dropped rather than indexed wrongly.
    """
    previous = None
    text = target.strip()
    while text != previous:  # nested subscripts: soil(j)%ly(ly)%st
        previous = text
        text = _SUBSCRIPT_RE.sub("", text)
    text = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"[A-Za-z_]\w*(?:%[A-Za-z_]\w*)*", text):
        return None
    return text.lower()


def find_corpus(explicit: Path | None = None) -> Path:
    """Locate an importable ``swatplus_reference``.

    Order: an explicit path, then the ``SWATPLUS_REFERENCE_CORPUS`` checkout
    convention shared with the rest of the package, then an already-importable
    install. An explicit environment checkout must not be shadowed by an
    unrelated editable install or freshness compares two parser revisions.
    """
    if explicit is not None:
        src = explicit / "src" if (explicit / "src").is_dir() else explicit
        if not (src / "swatplus_reference").is_dir():
            raise IndexError_(
                f"no swatplus_reference package under {explicit}. Pass the "
                "repository root or its src/ directory."
            )
        return src

    checkout = resolve_checkout("reference_corpus")
    if checkout is not None:
        src = checkout / "src"
        if not (src / "swatplus_reference").is_dir():
            raise IndexError_(
                f"SWATPLUS_REFERENCE_CORPUS={checkout} has no src/swatplus_reference."
            )
        return src

    try:  # already installed
        import swatplus_reference  # noqa: F401
    except ImportError:
        pass
    else:
        return Path(swatplus_reference.__file__).resolve().parent.parent

    raise IndexError_(
        "cannot find swatplus-reference-corpus. Set SWATPLUS_REFERENCE_CORPUS "
        "to its checkout, pip install it, or pass --corpus."
    )


def looks_like_swatplus(path: Path) -> bool:
    """True when ``path`` is a SWAT+ checkout or its ``src/`` directory."""
    if not path.is_dir():
        return False
    return _contains_fortran(path, recursive=False) or _contains_fortran(
        path / "src", recursive=False)


def resolve_source(explicit: Path | None = None) -> Path:
    """Locate the SWAT+ Fortran source directory.

    Order: an explicit path, the working directory when it is already a SWAT+
    checkout, then ``SWATPLUS_SOURCE``. Running from inside the checkout is the
    common case -- an end user should not have to type any path at all.

    Accepts either the repository root or its ``src/`` directory, so callers
    need not know which layout they have.
    """
    candidate = explicit
    if candidate is None and looks_like_swatplus(Path.cwd()):
        candidate = Path.cwd()
    if candidate is None:
        candidate = resolve_checkout("swatplus_source")
    if candidate is None:
        raise IndexError_(
            "cannot find SWAT+ source. Run this from inside a SWAT+ checkout, "
            "set SWATPLUS_SOURCE, or pass --source."
        )
    # Resolved, always. The scanner resolves its own root before listing files,
    # so it reports absolute paths; a relative root left as-is here reaches
    # every consumer that compares the two and fails the comparison. `--source
    # .` from inside a checkout -- the documented way to run it -- crashed in
    # scan_source_warnings for exactly that reason.
    candidate = Path(candidate).resolve()
    if not candidate.is_dir():
        raise IndexError_(f"source path does not exist or is not a directory: {candidate}")
    if (candidate / "src").is_dir() and not _contains_fortran(
        candidate, recursive=False
    ):
        candidate = candidate / "src"
    if not _contains_fortran(candidate):
        raise IndexError_(
            f"no .f90 files or other supported Fortran source files found under {candidate}"
        )
    return candidate


def _contains_fortran(path: Path, *, recursive: bool = True) -> bool:
    if not path.is_dir():
        return False
    candidates = path.rglob("*") if recursive else path.glob("*")
    return any(
        item.is_file() and item.suffix.lower() in FORTRAN_SUFFIXES
        for item in candidates
    )


def source_files(source: Path) -> list[Path]:
    """The tracked Fortran files under ``source``, with a fixture fallback.

    The corpus scanner uses Git's tracked file list when it has one. Mirroring
    that rule prevents an untracked scratch file from invalidating a snapshot
    that the scanner would rebuild identically. A non-Git tree (including unit
    test fixtures and exported source archives) falls back to walking disk.
    """
    root = source.resolve()
    top_text = _git(root, "rev-parse", "--show-toplevel")
    if top_text:
        top = Path(top_text).resolve()
        try:
            target = root.relative_to(top).as_posix()
        except ValueError:
            target = ""
        try:
            result = subprocess.run(
                ["git", "-C", str(top), "ls-files", "-z", "--", target or "."],
                capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            tracked = [
                top / raw.decode("utf-8", errors="surrogateescape")
                for raw in result.stdout.split(b"\0") if raw
            ]
            matched = [
                path for path in tracked
                if path.is_file() and path.suffix.lower() in FORTRAN_SUFFIXES
            ]
            if matched:
                return sorted(matched, key=lambda path: path.relative_to(root).as_posix())

    return sorted(
        (path for path in root.rglob("*")
         if path.is_file() and path.suffix.lower() in FORTRAN_SUFFIXES),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def split_field_doc(doc: str | None) -> dict[str, str | None]:
    """Split an inline field comment into units and meaning.

    SWAT+ writes them as ``mm | recharge entering aquifer from other objects``.
    Without the separator the whole comment is the meaning -- a bare unit with
    no explanation is not worth indexing as one.
    """
    text = (doc or "").strip()
    if not text:
        return {"units": None, "description": None}
    if "|" not in text:
        return {"units": None, "description": text}
    units, _, description = text.partition("|")
    return {"units": units.strip() or None, "description": description.strip() or None}


def source_fingerprint(source: Path) -> str:
    """Hash the Fortran source as it is on disk right now.

    Milliseconds against a multi-second build, so checking is effectively free
    and a rebuild can be made conditional on it. Content rather than mtime: an editor can
    touch a file without changing it, and a checkout can change a file without
    advancing its mtime.
    """
    digest = hashlib.blake2b(digest_size=16)
    root = source.resolve()
    for path in source_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def index_is_current(
    index_file: Path,
    source: Path,
    corpus: Path | None = None,
) -> bool:
    """True when ``index_file`` matches both source and parser revisions.

    Works for either artifact: the facts JSON or the rendered markdown.
    A parser swap can change extracted facts without changing one Fortran
    byte, so source fingerprint equality alone is not sufficient.
    """
    if not index_file.is_file():
        return False
    try:
        corpus_src = find_corpus(corpus)
    except IndexError_:
        return False
    current_parser = _git(corpus_src.parent, "rev-parse", "HEAD")
    stored_source = stored_fingerprint(index_file)
    stored_parser = stored_parser_commit(index_file)
    stored_format = stored_index_format(index_file)
    return bool(stored_source and stored_parser and current_parser and stored_format) and (
        stored_source == source_fingerprint(source)
        and stored_parser == current_parser
        and stored_format == INDEX_FORMAT_VERSION
    )


def stored_fingerprint(path: Path) -> str | None:
    """The source fingerprint an existing artifact was built from, if any."""
    if path.suffix == ".json":
        # Parsing a few MB costs tens of milliseconds against a multi-second reparse.
        # Duplicating the fingerprint somewhere cheaper to reach would give the
        # file two copies of one fact, free to disagree.
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload["provenance"]["source_fingerprint"]
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            return None
        return value if isinstance(value, str) else None

    with path.open(encoding="utf-8", errors="replace") as handle:
        for _ in range(40):  # the provenance block is at the top
            line = handle.readline()
            if not line:
                return None
            if line.startswith(FINGERPRINT_KEY):
                return line[len(FINGERPRINT_KEY):].strip()
    return None


def stored_parser_commit(path: Path) -> str | None:
    """The parser commit an existing artifact was built with, if known."""
    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload["provenance"]["parser_commit"]
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            return None
        return value if isinstance(value, str) and value != "unknown" else None

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    return None
                if line.startswith(PARSER_COMMIT_KEY):
                    value = line[len(PARSER_COMMIT_KEY):].strip()
                    return value if value and value != "unknown" else None
    except OSError:
        return None
    return None


def stored_index_format(path: Path) -> str | None:
    """The facts projection version in an existing JSON or markdown artifact."""

    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get("index_format") or payload["provenance"]["format_version"]
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            return None
        return str(value) if value is not None else None

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    return None
                if line.startswith(INDEX_FORMAT_KEY):
                    value = line[len(INDEX_FORMAT_KEY):].strip()
                    return value or None
    except OSError:
        return None
    return None


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def _provenance(source: Path, corpus_src: Path) -> Provenance:
    return Provenance(
        source_path=source.as_posix(),
        source_commit=_git(source, "rev-parse", "HEAD"),
        source_describe=_git(source, "describe", "--tags", "--always"),
        source_fingerprint=source_fingerprint(source),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        format_version=INDEX_FORMAT_VERSION,
        parser_commit=_git(corpus_src.parent, "rev-parse", "HEAD"),
    )


# ----------------------------------------------------------------- build

def output_unit_filenames(source: Path) -> dict[str, str]:
    """Map output unit number -> filename, from ``open_output_file`` calls."""
    mapping: dict[str, str] = {}
    for path in source_files(source):
        text = path.read_text(encoding="utf-8", errors="replace")
        for unit, name in _OPEN_HELPER_RE.findall(text):
            mapping[unit] = name
    return mapping


def declares_parameter(declaration: str | None) -> bool:
    """True when a declaration carries the ``parameter`` attribute.

    Derived once at build time and stored on the record, so a consumer
    filtering compile-time constants out of a debugger symbol map does not
    need its own Fortran attribute parser.
    """
    if not declaration:
        return False
    attributes, separator, _ = declaration.partition("::")
    # No `::` means no attribute list, so nothing can carry `parameter`.
    return bool(separator) and bool(_PARAMETER_RE.search(attributes))


def module_variables(project: Any) -> list[ModuleVariable]:
    """Every variable declared in a module body, outside any procedure.

    The parser reports these on ``project.modules``; until this function
    existed the only caller was :func:`input_filenames`, which read a name and
    a type to resolve input filenames and discarded the rest.
    """
    records: list[ModuleVariable] = []
    for module in getattr(project, "modules", ()) or ():
        module_name = getattr(module, "name", None)
        if not module_name:
            continue
        for variable in getattr(module, "variables", ()) or ():
            name = getattr(variable, "name", None)
            if not name:
                continue
            location = getattr(variable, "location", None)
            declaration = getattr(variable, "declaration", None)
            records.append(ModuleVariable(
                name=name,
                module=module_name,
                vartype=getattr(variable, "vartype", None),
                declaration=declaration,
                line=getattr(location, "line", 0) or 0,
                initial=getattr(variable, "initial", None),
                is_parameter=declares_parameter(declaration),
                **split_field_doc(getattr(variable, "doc", None)),
            ))
    return records


def input_filenames(project: Any) -> dict[str, str]:
    """Map an input-file expression to its source-declared default filename.

    The pinned parser now resolves this itself, but keeping the final mapping at
    the index boundary makes the headline lookup resilient to a parser
    regression or an older compatible parser. For example, ``in_aqu%aqu`` is
    declared through ``type(input_aqu)`` and defaults to ``aquifer.aqu``.

    Components without a literal default remain expressions because their
    runtime filename cannot be known by static analysis.
    """
    defaults: dict[str, str] = {}
    for derived in getattr(project, "types", ()):
        for component in getattr(derived, "components", ()):
            initial = (getattr(component, "initial", None) or "").strip()
            if (len(initial) > 2 and initial[0] in "\"'"
                    and initial[-1] == initial[0]):
                defaults[f"{derived.name.lower()}%{component.name.lower()}"] = initial[1:-1]

    var_types: dict[str, str] = {}
    for module in getattr(project, "modules", ()):
        for variable in getattr(module, "variables", ()):
            match = _TYPE_DECL_RE.match(getattr(variable, "vartype", "") or "")
            if match:
                var_types[variable.name.lower()] = match.group(1).lower()

    resolved: dict[str, str] = {}
    for variable, type_name in var_types.items():
        prefix = f"{type_name}%"
        for key, filename in defaults.items():
            if key.startswith(prefix):
                resolved[f"{variable}%{key[len(prefix):]}"] = filename
    return resolved


def call_arguments(raw: str, callee: str) -> list[str] | None:
    """The actual arguments of the first call to ``callee`` in a statement.

    Commas inside parentheses or quotes do not split, so
    ``call f(a(i, j), "x, y")`` gives ``["a(i, j)", '"x, y"']``. ``None`` when
    the statement does not call ``callee`` with an argument list.
    """
    match = re.search(rf"\b{re.escape(callee)}\s*\(", raw, re.IGNORECASE)
    if not match:
        return None
    args: list[str] = []
    depth, quote, current = 0, "", []
    for char in raw[match.end():]:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                args.append("".join(current).strip())
                return [arg for arg in args if arg]
            depth -= 1
        elif char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    return None


def actual_argument(raw: str, routine: str, dummies: list[str], dummy: str) -> str | None:
    """What one call statement passes for ``dummy``, by keyword or position.

    ``None`` when the statement does not call ``routine`` or passes nothing
    for that dummy (an optional argument left out).
    """
    wanted = dummy.lower()
    positions = [name.lower() for name in dummies]
    if wanted not in positions:
        return None
    actuals = call_arguments(raw, routine)
    if actuals is None:
        return None
    value: str | None = None
    positional: list[str] = []
    for actual in actuals:
        keyword = re.match(r"^\s*(\w+)\s*=(?!=)\s*(.*)$", actual, re.DOTALL)
        if keyword:
            if keyword.group(1).lower() == wanted:
                value = keyword.group(2)
        else:
            positional.append(actual)
    position = positions.index(wanted)
    if value is None and position < len(positional):
        value = positional[position]
    return value


def argument_filenames(
    routine: str,
    dummies: list[str],
    dummy: str,
    call_sites: list[str],
    inputs: dict[str, str],
) -> list[str]:
    """Resolve a filename a routine receives as an argument, via its callers.

    ``hyd_read_connect`` opens ``con_file``, its first dummy argument; all
    twelve connectivity files reach it from ``hyd_connect`` as
    ``in_con%hru_con``, ``in_con%aqu_con``, and so on. Keyed on the dummy name,
    every one of those reads is filed under ``con_file`` and a question about
    ``hru.con`` finds nothing.

    Each call site's actual argument is resolved the same way a direct
    ``open`` is: a quoted literal is the filename, and an input-file
    expression maps to its source-declared default. Anything else stays as the
    caller's expression, which still names more than the dummy did. Returns
    the distinct names in call-site order, or ``[]`` when no call site passes
    the argument -- the caller then keeps the dummy name rather than guess.
    """
    if dummy.lower() not in (name.lower() for name in dummies):
        return []
    found: list[str] = []
    for raw in call_sites:
        value = actual_argument(raw, routine, dummies, dummy)
        if value is None:
            continue
        value = value.strip()
        if len(value) > 1 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1].strip()
        else:
            value = inputs.get(re.sub(r"\s+", "", value).lower(), value)
        if value and value not in found:
            found.append(value)
    return found


def caller_opened_files(
    callee: str,
    unit: str,
    call_lines: dict[str, list[tuple[str, int]]],
    unit_events: dict[str, list[tuple[int, str, str, str]]],
) -> list[str]:
    """The file a unit is open on at every call of a routine that never opens it.

    ``read_mgtops`` reads unit 107 with no ``open`` of its own: its one caller,
    ``mgt_read_mgtops``, has ``management.sch`` open on 107 when it makes the
    call. Filed under the scanner's ``unit_107``, the operation lines had no
    file and their layout was lost.

    Each call site must find the unit open -- an ``open`` of it earlier in the
    caller with no ``close`` in between. One call site that does not, and the
    answer is ``[]``: the caller then keeps the unit rather than guess.
    """
    found: list[str] = []
    sites = call_lines.get(callee.lower(), [])
    for caller, line in sites:
        state: str | None = None
        for event_line, kind, event_unit, name in sorted(unit_events.get(caller, [])):
            if event_unit == unit.lower() and event_line < line:
                state = name if kind == "open" else None
        if not state:
            return []
        if state not in found:
            found.append(state)
    return found


def _closing(text: str, open_at: int) -> int | None:
    """Where the parenthesis opening at ``open_at`` closes, outside quotes."""
    depth, quote = 0, ""
    for position in range(open_at, len(text)):
        char = text[position]
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return position
    return None


def _top_level(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """Spans of ``pattern`` outside parentheses and quotes."""
    spans: list[tuple[int, int]] = []
    depth, quote, position = 0, "", 0
    while position < len(text):
        char = text[position]
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            match = pattern.match(text, position)
            if match:
                spans.append(match.span())
                position = match.end()
                continue
        position += 1
    return spans


def _unparenthesise(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and _closing(text, 0) == len(text) - 1:
        text = text[1:-1].strip()
    return text


def if_condition(statement: str) -> str | None:
    """The condition of an ``if`` or ``else if`` statement, parentheses removed."""
    match = _IF_RE.match(statement)
    if not match:
        return None
    close = _closing(statement, match.end() - 1)
    return None if close is None else statement[match.end():close]


def comparison_operand(text: str) -> str | None:
    """A comparison operand as a whitespace-free variable reference.

    ``trim(adjustl(lum(ilum)%name))`` gives ``lum(ilum)%name``. ``None`` for
    a literal, a number, or an expression -- anything :func:`field_path`
    cannot reduce to a field path.
    """
    text = _unparenthesise(text)
    while (match := _PADDING_RE.match(text)) and \
            _closing(text, match.end() - 1) == len(text) - 1:
        text = _unparenthesise(text[match.end():-1])
    text = re.sub(r"\s+", "", text)
    return text if text and field_path(text) is not None else None


def condition_comparisons(condition: str) -> list[tuple[str, str, str]]:
    """Every ``(left, op, right)`` equality test between two references.

    Split on the logical connectives first, so each side of an ``.and.`` or
    ``.or.`` is its own comparison, and parentheses are looked through.
    """
    text = _unparenthesise(condition)
    connectives = _top_level(text, _LOGICAL_RE)
    if connectives:
        found: list[tuple[str, str, str]] = []
        start = 0
        for begin, end in [*connectives, (len(text), len(text))]:
            found.extend(condition_comparisons(text[start:begin]))
            start = end
        return found
    operators = _top_level(text, _EQUALITY_RE)
    if len(operators) != 1:
        return []
    begin, end = operators[0]
    left = comparison_operand(text[:begin])
    right = comparison_operand(text[end:])
    if left is None or right is None:
        return []
    op = "==" if text[begin:end].lower() in ("==", ".eq.") else "/="
    return [(left, op, right)]


def substitute_actual(
    operand: str, routine: str, dummies: list[str], call: str,
) -> str | None:
    """Restate a routine's operand in its caller's terms, for one call.

    ``search`` compares ``sch(nn) == cfind``; ``call search (wst_n,
    db_mx%wst, ob(i)%wst_c, ob(i)%wst)`` makes that ``wst_n(:) ==
    ob(i)%wst_c``. A subscripted dummy handed a whole array becomes
    ``array(:)``, every element, since which one the callee reaches is its
    own business. An operand whose root is not a dummy comes back unchanged.
    ``None`` when the call passes something other than a variable, or an
    element or section where the callee indexes the dummy itself.
    """
    match = re.match(r"[A-Za-z_]\w*", operand)
    if match is None:
        return None
    root, tail = match.group(0), operand[match.end():]
    if root.lower() not in {name.lower() for name in dummies}:
        return operand
    subscript = ""
    if tail.startswith("("):
        close = _closing(tail, 0)
        if close is None:
            return None
        subscript, tail = tail[:close + 1], tail[close + 1:]
    actual = actual_argument(call, routine, dummies, root)
    actual = comparison_operand(actual) if actual is not None else None
    if actual is None:
        return None
    if subscript:
        if actual.endswith(")"):
            return None
        return f"{actual}(:){tail}"
    return f"{actual}{tail}"


def path_prefixes(path: str) -> list[str]:
    """``a%b%c`` -> ``["a", "a%b", "a%b%c"]``: every structure holding it."""
    parts = path.split("%")
    return ["%".join(parts[:count]) for count in range(1, len(parts) + 1)]


def _loops_at(index: SourceIndex, procedure: str, line: int) -> tuple[str, ...] | None:
    """The loops enclosing a line, as ``scope_at`` reports them from the facts."""
    proc = index.procedures.get(procedure.lower())
    if proc is None or proc.path.replace("\\", "/") in index.unresolved_loop_files:
        return None
    return tuple(
        item.index or item.header for item in index.loops.get(procedure.lower(), ())
        if item.end_line is not None and item.line <= line <= item.end_line)


def _add_call_comparisons(
    index: SourceIndex,
    project: Any,
    call_statements: dict[str, list[tuple[str, int, str]]],
    scopes: dict[str, tuple[set[str], set[str]]],
) -> None:
    """Restate each comparison a routine makes on its dummies at every call.

    One level only: a routine that passes its own dummies on to another is
    not followed further.
    """
    dummies_of = {proc.name.lower(): list(proc.args) for proc in project.procedures}
    for callee, items in list(index.comparisons.items()):
        dummies = dummies_of.get(callee, [])
        wanted = {name.lower() for name in dummies}
        on_dummies = [
            item for item in items if item.via is None and any(
                (path or "").split("%")[0] in wanted
                for path in (item.left_path, item.right_path))
        ]
        if not on_dummies:
            continue
        for caller, line, raw in call_statements.get(callee, []):
            caller_proc = index.procedures.get(caller)
            in_scope = scopes.get(caller, (set(), set()))[0]
            seen: set[tuple[str, str, str]] = set()
            for item in on_dummies:
                left = substitute_actual(item.left, callee, dummies, raw)
                right = substitute_actual(item.right, callee, dummies, raw)
                if left is None or right is None or caller_proc is None:
                    continue
                if not all((field_path(side) or "").split("%")[0] in in_scope
                           for side in (left, right)):
                    continue
                if (left, item.op, right) in seen:
                    continue
                seen.add((left, item.op, right))
                index.comparisons[caller].append(Comparison(
                    procedure=caller_proc.name, line=line, left=left, op=item.op,
                    right=right, loops=_loops_at(index, caller, line),
                    via=f"{index.procedures[callee].name}:{item.line}"))
            if seen:
                index.comparisons[caller].sort(
                    key=lambda item: (item.line, item.via or ""))


def _add_copies(
    index: SourceIndex, project: Any, scopes: dict[str, tuple[set[str], set[str]]],
) -> None:
    """Every copy into a variable a comparison tests, three hops back.

    A copy's source may itself have been copied, so the wanted set grows by
    each hop's sources; three hops is the bound, not a measured need.
    """
    def key(procedure: str, path: str) -> tuple[str, str]:
        # Module-level names are one variable everywhere; a local is not.
        own = scopes.get(procedure.lower(), (set(), set()))[1]
        return (procedure.lower() if path.split("%")[0] in own else "", path)

    wanted: set[tuple[str, str]] = set()
    for procedure, items in index.comparisons.items():
        for item in items:
            for path in (item.left_path, item.right_path):
                if path:
                    wanted.update(key(procedure, prefix) for prefix in path_prefixes(path))
    assignments = [
        (proc, step) for proc in project.procedures for step in proc.assignments
        if getattr(step, "target", None) and getattr(step, "expression", None)
    ]
    found: dict[tuple[str, int], Copy] = {}
    for _ in range(3):
        grown: set[tuple[str, str]] = set()
        for proc, step in assignments:
            target = comparison_operand(step.target)
            target_path = field_path(target) if target else None
            if not target_path or key(proc.name, target_path) not in wanted:
                continue
            source = comparison_operand(step.expression)
            source_path = field_path(source) if source else None
            if not source_path:
                continue
            in_scope = scopes.get(proc.name.lower(), (set(), set()))[0]
            if source_path.split("%")[0] not in in_scope:
                continue
            site = (proc.name.lower(), step.location.line)
            if site in found:
                continue
            found[site] = Copy(
                procedure=proc.name, line=step.location.line, target=target,
                op="=>" if step.kind == "pointer_association" else "=",
                source=source)
            grown.update(key(proc.name, prefix) for prefix in path_prefixes(source_path))
        if not grown - wanted:
            break
        wanted |= grown
    for copy in sorted(found.values(), key=lambda c: (c.procedure.lower(), c.line)):
        index.copies[copy.target_path].append(copy)


def build_source_index(
    source: Path | None = None,
    corpus: Path | None = None,
) -> SourceIndex:
    """Parse a SWAT+ checkout into a queryable index of facts."""
    source_dir = resolve_source(source)
    corpus_src = find_corpus(corpus)
    if str(corpus_src) not in sys.path:
        sys.path.insert(0, str(corpus_src))

    try:
        from swatplus_reference.parser.schema_config import BuildConfig
        from swatplus_reference.parser.schema_fortran import FortranScanner
    except ImportError as exc:  # pragma: no cover - defensive
        raise IndexError_(f"could not import swatplus_reference from {corpus_src}: {exc}")

    # scan() leaves called_by/call_paths/resolved empty; analyze_project fills
    # them. See tamandua/index/analyze.py.
    scanner = FortranScanner(BuildConfig(source_dir=source_dir))
    scanner_files = list(scanner.iter_source_files())
    scanner_warnings = scan_source_warnings(source_dir, scanner_files)
    ranges_by_file: dict[str, list[LoopScope] | None] = {}
    for raw_path in scanner_files:
        path = Path(raw_path)
        if not path.is_absolute():
            path = source_dir / path
        try:
            relative = path.resolve().relative_to(source_dir.resolve()).as_posix()
        except ValueError:
            relative = path.name
        ranges = loop_ranges(path)
        ranges_by_file[relative] = ranges
        # The parser normally reports a basename. Retain the relative key too
        # so nested source layouts remain unambiguous when they occur.
        ranges_by_file.setdefault(path.name, ranges)
    project = analyze_project(scanner.scan())
    units = output_unit_filenames(source_dir)
    inputs = input_filenames(project)
    # Every call statement, by the routine it calls: what argument_filenames
    # needs to follow a filename passed in as a dummy argument.
    call_sites: dict[str, list[str]] = defaultdict(list)
    # And by line, with where each caller opens and closes each unit: what
    # caller_opened_files needs for a routine reading a unit it never opened.
    call_lines: dict[str, list[tuple[str, int]]] = defaultdict(list)
    call_statements: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    unit_events: dict[str, list[tuple[int, str, str, str]]] = defaultdict(list)
    for caller in project.procedures:
        caller_arguments = {name.lower() for name in caller.args}
        for call in caller.calls:
            call_sites[call.name.lower()].append(call.raw)
            if getattr(call, "kind", "subroutine") == "subroutine":
                call_lines[call.name.lower()].append(
                    (caller.name.lower(), call.location.line))
                call_statements[call.name.lower()].append(
                    (caller.name.lower(), call.location.line, call.raw))
        for op in caller.io:
            if op.kind not in ("open", "close") or not op.unit:
                continue
            opened = (op.file_resolved or op.file_expr or "").strip().strip("'\"")
            opened = inputs.get(opened.lower(), opened)
            if op.kind == "open" and (not opened or opened.startswith("unit_")
                                      or opened.lower() in caller_arguments):
                # Not a name this build can vouch for; the unit counts as
                # closed, so a routine reading it is left alone.
                opened = ""
            unit_events[caller.name.lower()].append(
                (op.location.line, op.kind, op.unit.lower(), opened))
    index = SourceIndex(
        provenance=_provenance(source_dir, corpus_src),
        scanner_warnings=scanner_warnings,
    )

    for derived in project.types:
        fields: list[Field] = []
        for component in derived.components:
            fields.append(Field(
                type_name=derived.name,
                name=component.name,
                vartype=component.vartype,
                declaration=getattr(component, "declaration", None),
                location=component.location.label(),
                **split_field_doc(component.doc),
            ))
        index.types[derived.name.lower()] = DerivedType(
            name=derived.name, module=derived.module,
            location=derived.location.label(), fields=fields,
        )

    for variable in module_variables(project):
        # Keyed on the pair: a bare-name key would silently drop one of the
        # two declarations wherever a name is reused across modules.
        index.module_variables[
            (variable.module.lower(), variable.name.lower())] = variable
    module_names = {name for _, name in index.module_variables}
    #: Per procedure: every name in scope, and its own arguments and locals.
    scopes: dict[str, tuple[set[str], set[str]]] = {}

    for proc in project.procedures:
        argument_names = {name.lower() for name in proc.args}
        variables = {variable.name.lower(): variable for variable in proc.variables}

        def declaration(variable: Any | None, fallback_name: str) -> VariableDeclaration:
            doc = split_field_doc(getattr(variable, "doc", None))
            location = getattr(variable, "location", None)
            return VariableDeclaration(
                name=getattr(variable, "name", fallback_name),
                declaration=getattr(variable, "declaration", None),
                line=getattr(location, "line", proc.location.line),
                vartype=getattr(variable, "vartype", None),
                initial=getattr(variable, "initial", None),
                **doc,
            )

        arguments = [
            declaration(variables.get(name.lower()), name)
            for name in proc.args
        ]
        locals_ = [
            declaration(variable, variable.name)
            for variable in proc.variables
            if variable.name.lower() not in argument_names
        ]
        index.procedures[proc.name.lower()] = Procedure(
            name=proc.name,
            module=proc.module,
            location=proc.location.label(),
            path=proc.location.path,
            called_by=list(proc.called_by),
            callees=sorted({c.name for c in proc.calls if c.resolved}),
            uses=[
                Use(
                    module=use.module,
                    only=tuple(use.only or ()),
                    line=use.location.line,
                    intrinsic=bool(getattr(use, "intrinsic", False)),
                )
                for use in proc.uses
            ],
            arguments=arguments,
            locals=locals_,
            select_cases=[
                SelectCase(
                    subject=select.subject,
                    cases=tuple(str(case) for case in select.cases),
                    line=select.location.line,
                )
                for select in proc.select_cases
            ],
        )
        if proc.call_paths:
            index.call_paths[proc.name.lower()] = [list(p) for p in proc.call_paths]

        # The file(s) each unit was last seen on in this procedure, for the
        # positioning statement that names only a unit.
        bound: dict[str, list[str]] = {}
        for op in proc.io:
            unit = op.unit
            bare = _BARE_POSITIONING_RE.match(op.raw or "") if unit is None else None
            if bare:
                # `backspace 107` without parentheses: the scanner reports no
                # unit, so the statement was dropped. There are two in 62.0.0,
                # and without the one in soil_db_read its peek-then-reread of
                # every soil record reads as two records.
                unit = bare.group(1)
            name = (op.file_resolved or op.file_expr or "").strip().strip("'\"")
            # The scanner labels an unresolved write target `unit_2520`; the
            # helper map turns that back into the real output filename.
            if (not name or name.startswith("unit_")) and unit in units:
                name = units[unit]
            # Defence in depth around the parser contract: a compatible parser
            # may still report `in_aqu%aqu` instead of its default filename.
            name = inputs.get(name.lower(), name)
            names = [name] if name else []
            if name.lower() in argument_names:
                names = argument_filenames(
                    proc.name, list(proc.args), name,
                    call_sites.get(proc.name.lower(), []), inputs,
                ) or names
            if not names and bare:
                names = bound.get(unit, [])
            if names == [f"unit_{unit}"] and unit not in bound:
                # A unit this routine reads but never opens: the file its
                # callers have open on it, when every call site agrees.
                names = caller_opened_files(proc.name, unit, call_lines,
                                            unit_events) or names
            if not names:
                continue
            if unit:
                bound[unit] = names
            for resolved in names:
                use = IOUse(file=resolved, op=op.kind, unit=unit,
                            procedure=proc.name, line=op.location.line,
                            fields=tuple(op.fields))
                index.io_by_file[resolved.lower()].append(use)
                if unit:
                    index.io_by_unit[unit].append(use)

        for step in proc.assignments:
            match = _ASSIGN_RE.match(step.raw)
            if not match:
                continue
            path = field_path(match.group(1))
            if path:
                index.writers[path].append(f"{proc.name}:{step.location.line}")
                if "%" in path:
                    index.writer_statements[path].append(WriterStatement(
                        procedure=proc.name,
                        line=step.location.line,
                        raw=step.raw.strip(),
                    ))

        proc_path = proc.location.path.replace("\\", "/")
        file_ranges = ranges_by_file.get(proc_path)
        if proc_path not in ranges_by_file:
            file_ranges = ranges_by_file.get(Path(proc_path).name)
        if file_ranges is None:
            index.unresolved_loop_files.add(proc_path)
        range_by_start = {
            item.start: item for item in (file_ranges or [])
        }
        # A comparison operand must be a variable, not an intrinsic called
        # with an argument (`len_trim(name)` reduces to a path just as well).
        in_scope = argument_names | set(variables) | module_names
        scopes[proc.name.lower()] = (in_scope, argument_names | set(variables))
        for step in proc.control_steps:
            if step.kind == "loop":
                resolved = range_by_start.get(step.location.line)
                index.loops[proc.name.lower()].append(
                    Loop(procedure=proc.name, line=step.location.line,
                         header=step.raw.strip()[:90],
                         end_line=resolved.end if resolved else None,
                         index=resolved.index if resolved else None)
                )
            elif step.kind in ("if", "else"):
                condition = if_condition(step.raw)
                if condition is None:
                    continue
                line = step.location.line
                for left, op, right in condition_comparisons(condition):
                    roots = [path.split("%")[0] for path in
                             (field_path(left) or "", field_path(right) or "")]
                    if not all(root in in_scope for root in roots):
                        continue
                    index.comparisons[proc.name.lower()].append(Comparison(
                        procedure=proc.name, line=line, left=left, op=op,
                        right=right, loops=_loops_at(index, proc.name, line)))

    _add_call_comparisons(index, project, call_statements, scopes)
    _add_copies(index, project, scopes)
    return index
