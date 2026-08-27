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
from tamandua.index.scope import LoopScope, condition_for, scope_at

#: Bumped when the extracted fields change shape, so a stale index is
#: recognisable as stale rather than silently mis-read.
INDEX_FORMAT_VERSION = "1"

# Assignment targets: `name`, `name(i)`, `name%comp`, `a%b(i)%c = ...`.
# The negative lookahead keeps `==` comparisons out.
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


class IndexError_(RuntimeError):
    """Raised with an actionable message when the index cannot be built."""


#: Prefix of the fingerprint line in a rendered index's provenance block.
FINGERPRINT_KEY = "source_fingerprint: "
#: Prefix of the parser revision in a rendered index's provenance block.
PARSER_COMMIT_KEY = "parser_commit: "


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

    def as_lines(self) -> list[str]:
        return [
            f"source_path: {self.source_path}",
            f"source_commit: {self.source_commit or 'unknown'}",
            f"source_version: {self.source_describe or 'unknown'}",
            f"{FINGERPRINT_KEY}{self.source_fingerprint}",
            f"generated_at: {self.generated_at}",
            f"index_format: {self.format_version}",
            f"parser_commit: {self.parser_commit or 'unknown'}",
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
    ``swatplus_reference`` present at all (docs/decisions.md D-8): the parser
    is a build-time dependency, not a runtime one.
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


@dataclass
class SourceIndex:
    """Facts extracted from one SWAT+ checkout."""

    provenance: Provenance
    procedures: dict[str, Procedure] = field(default_factory=dict)
    io_by_file: dict[str, list[IOUse]] = field(default_factory=lambda: defaultdict(list))
    io_by_unit: dict[str, list[IOUse]] = field(default_factory=lambda: defaultdict(list))
    writers: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    loops: dict[str, list[Loop]] = field(default_factory=lambda: defaultdict(list))
    types: dict[str, DerivedType] = field(default_factory=dict)
    call_paths: dict[str, list[list[str]]] = field(default_factory=lambda: defaultdict(list))

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

    def loops_in(self, procedure: str) -> list[Loop]:
        return self.loops.get(procedure.strip().lower(), [])

    def derived_type(self, name: str) -> DerivedType | None:
        return self.types.get(name.strip().lower())

    def paths_to(self, procedure: str) -> list[list[str]]:
        """Execution paths from an entry point down to this procedure."""
        return self.call_paths.get(procedure.strip().lower(), [])

    def scope_at(self, source_file: str, line: int) -> list[LoopScope] | None:
        """Loops enclosing a line, outermost first; ``None`` if unrecoverable."""
        root = Path(self.provenance.source_path)
        return scope_at(root / Path(source_file).name, line)

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

    Order: an explicit path, then an already-importable install, then the
    ``SWATPLUS_REFERENCE_CORPUS`` checkout convention shared with the rest of
    the package. Raises with the fix rather than returning something unusable.
    """
    if explicit is not None:
        src = explicit / "src" if (explicit / "src").is_dir() else explicit
        if not (src / "swatplus_reference").is_dir():
            raise IndexError_(
                f"no swatplus_reference package under {explicit}. Pass the "
                "repository root or its src/ directory."
            )
        return src

    try:  # already installed
        import swatplus_reference  # noqa: F401
    except ImportError:
        pass
    else:
        return Path(swatplus_reference.__file__).resolve().parent.parent

    checkout = resolve_checkout("reference_corpus")
    if checkout is None:
        raise IndexError_(
            "cannot find swatplus-reference-corpus. Set SWATPLUS_REFERENCE_CORPUS "
            "to its checkout, pip install it, or pass --corpus."
        )
    src = checkout / "src"
    if not (src / "swatplus_reference").is_dir():
        raise IndexError_(
            f"SWATPLUS_REFERENCE_CORPUS={checkout} has no src/swatplus_reference."
        )
    return src


def looks_like_swatplus(path: Path) -> bool:
    """True when ``path`` is a SWAT+ checkout or its ``src/`` directory."""
    if not path.is_dir():
        return False
    return any(path.glob("*.f90")) or any((path / "src").glob("*.f90"))


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
    candidate = Path(candidate)
    if not candidate.is_dir():
        raise IndexError_(f"source path does not exist or is not a directory: {candidate}")
    if (candidate / "src").is_dir() and not any(candidate.glob("*.f90")):
        candidate = candidate / "src"
    if not any(candidate.rglob("*.f90")):
        raise IndexError_(f"no .f90 files found under {candidate}")
    return candidate


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

    ~14 ms against a ~6.2 s parse, so checking is effectively free and a rebuild
    can be made conditional on it. Content rather than mtime: an editor can
    touch a file without changing it, and a checkout can change a file without
    advancing its mtime.
    """
    digest = hashlib.blake2b(digest_size=16)
    for path in sorted(source.rglob("*.f90")):
        digest.update(path.name.encode())
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
    return bool(stored_source and stored_parser and current_parser) and (
        stored_source == source_fingerprint(source)
        and stored_parser == current_parser
    )


def stored_fingerprint(path: Path) -> str | None:
    """The source fingerprint an existing artifact was built from, if any."""
    if path.suffix == ".json":
        # Parsing a few MB costs tens of milliseconds against a ~6 s reparse.
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
    for path in sorted(source.rglob("*.f90")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for unit, name in _OPEN_HELPER_RE.findall(text):
            mapping[unit] = name
    return mapping


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
    project = analyze_project(FortranScanner(BuildConfig(source_dir=source_dir)).scan())
    units = output_unit_filenames(source_dir)
    index = SourceIndex(provenance=_provenance(source_dir, corpus_src))

    for derived in project.types:
        fields: list[Field] = []
        for component in derived.components:
            fields.append(Field(
                type_name=derived.name,
                name=component.name,
                vartype=component.vartype,
                location=component.location.label(),
                **split_field_doc(component.doc),
            ))
        index.types[derived.name.lower()] = DerivedType(
            name=derived.name, module=derived.module,
            location=derived.location.label(), fields=fields,
        )

    for proc in project.procedures:
        index.procedures[proc.name.lower()] = Procedure(
            name=proc.name,
            module=proc.module,
            location=proc.location.label(),
            path=proc.location.path,
            called_by=list(proc.called_by),
            callees=sorted({c.name for c in proc.calls if c.resolved}),
        )
        if proc.call_paths:
            index.call_paths[proc.name.lower()] = [list(p) for p in proc.call_paths]

        for op in proc.io:
            name = (op.file_resolved or op.file_expr or "").strip().strip("'\"")
            # The scanner labels an unresolved write target `unit_2520`; the
            # helper map turns that back into the real output filename.
            if (not name or name.startswith("unit_")) and op.unit in units:
                name = units[op.unit]
            if not name:
                continue
            use = IOUse(file=name, op=op.kind, unit=op.unit,
                        procedure=proc.name, line=op.location.line,
                        fields=tuple(op.fields))
            index.io_by_file[name.lower()].append(use)
            if op.unit:
                index.io_by_unit[op.unit].append(use)

        for step in proc.assignments:
            match = _ASSIGN_RE.match(step.raw)
            if not match:
                continue
            path = field_path(match.group(1))
            if path:
                index.writers[path].append(f"{proc.name}:{step.location.line}")

        for step in proc.control_steps:
            if step.kind == "loop":
                index.loops[proc.name.lower()].append(
                    Loop(procedure=proc.name, line=step.location.line,
                         header=step.raw.strip()[:90])
                )

    return index
