"""What SWAT+ reads from each input file, column by column, in read order.

An input file's layout is not declared anywhere in SWAT+. It is implied by the
statement that reads it: ``read (113,*) k, hru_db(i)%dbsc`` in ``hru_read``
reads an integer and then, list-directed, every component of
``hru_databases_char`` in declaration order -- ``name, topo, hyd, soil, ...``.
The facts already hold both halves: the read statement and its variables
(``IOUse.fields``), and each derived type's components with their types, units
and inline descriptions (``DerivedType.fields``). This module joins them.

Nothing here is inferred from a data file or written by a model. Every column
carries the declaration it came from and every record the statement that reads
it, so a consumer -- the dataselector's schema, first -- can show its evidence.

What the Fortran cannot say is left unsaid rather than guessed:

- **Header names.** The file's header says ``lu_mgt`` where the Fortran says
  ``land_use_mgt``. Columns are named by their Fortran component; matching to a
  header is the consumer's job, and position is safe to match on because this
  *is* the read order.
- **Columns SWAT+ never reads.** A list-directed read stops once its variables
  are filled, so ``fertilizer.frt``'s trailing ``pathogens`` and
  ``description`` do not appear. The layout is what the model reads, which is
  the question it answers; what a file may additionally hold is not in the
  source.
- **Foreign keys.** A column naming a row in another file is matched at run
  time by comparing strings; no declaration says so.

Works on any :class:`~tamandua.index.build.SourceIndex`, including one loaded
from the bundled snapshot, so it needs neither the parser nor a checkout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tamandua.index.build import Field, IOUse, Procedure, SourceIndex

#: Bumped whenever the JSON shape changes, independent of the facts format.
LAYOUT_FORMAT = "1"

_TYPE_RE = re.compile(r"^\s*(?:type|class)\s*\(\s*(\w+)\s*\)", re.IGNORECASE)
_DIMENSION_RE = re.compile(r"\bdimension\s*\(", re.IGNORECASE)
_DEFERRED_RE = re.compile(r"\b(allocatable|pointer)\b", re.IGNORECASE)
_KEYWORD_ARG_RE = re.compile(r"^\s*(\w+)\s*=(?!=)")


@dataclass
class Column:
    """One value a read statement consumes, with the declaration behind it."""

    #: The Fortran name, subscripted when expanded from an array: ``cn(2)``.
    name: str
    #: Where the value lands, subscripts removed: ``hru_db%dbsc%topo``.
    path: str
    vartype: str | None
    units: str | None
    description: str | None
    #: ``file.f90:line`` for a component or local, ``module:line`` for a
    #: module variable -- the same convention the MCP server uses.
    declared_at: str | None
    #: Set on a column that repeats: the implied-do count as the source writes
    #: it (``nout``), an array's non-constant extent, or ``*`` for an array
    #: allocated at run time. Consecutive columns with a repeat form a group.
    repeat: str | None = None


@dataclass
class Record:
    """One read statement that consumes a line of data."""

    #: ``main`` -- the record read once per object; ``child`` -- read in a
    #: loop nested inside it (soil layers, monthly weather); ``alternative``
    #: -- another read of the same record start, on a different branch;
    #: ``line`` -- another line at the main record's depth.
    role: str
    procedure: str
    at: str
    #: Enclosing loops, outermost first, by index variable (or header).
    loops: list[str]
    columns: list[Column]
    #: Fields that could not be expanded, with why. Non-empty means the column
    #: list is incomplete, so a consumer must not treat it as the whole record.
    unresolved: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unresolved


@dataclass
class PreambleLine:
    """A line read before the first record: a title, a header, a count."""

    at: str
    kind: str  # "text" (one character value) or "values"
    reads: list[str]


@dataclass
class FileLayout:
    file: str
    #: False when the filename is an expression, not a source default -- the
    #: weather data files (``pcp(i)%filename``) are named inside another file.
    filename_is_default: bool
    readers: list[str]
    preamble: list[PreambleLine]
    records: list[Record]

    @property
    def main(self) -> Record | None:
        return next((r for r in self.records if r.role == "main"), None)


# ------------------------------------------------------------ declarations

def _split_top(text: str) -> list[str]:
    """Split on commas outside parentheses and quotes."""
    parts: list[str] = []
    depth, quote, current = 0, "", []
    for char in text:
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
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _balanced(text: str, open_at: int) -> str:
    """The text inside the parenthesis that opens at ``open_at``."""
    depth = 0
    for position in range(open_at, len(text)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:position]
    return text[open_at + 1:]


def _strip_subscripts(text: str) -> str:
    previous = None
    while text != previous:
        previous = text
        text = re.sub(r"\([^()]*\)", "", text)
    return re.sub(r"\s+", "", text)


def declared_extents(declaration: str | None, name: str) -> list[str] | None:
    """The declared extents of ``name``, e.g. ``["4"]``; ``None`` if scalar.

    Reads both spellings: ``real, dimension(4) :: cn`` and ``real :: cn(4)``.
    A declaration line may list several entities, so the entity form is looked
    up by name rather than taken from the first one.
    """
    if not declaration or "::" not in declaration:
        return None
    head, _, tail = declaration.partition("::")
    match = _DIMENSION_RE.search(head)
    if match:
        return _split_top(_balanced(head, match.end() - 1))
    for entity in _split_top(tail):
        entity = entity.split("=")[0].strip()
        found = re.match(rf"{re.escape(name)}\s*\(", entity, re.IGNORECASE)
        if found:
            return _split_top(_balanced(entity, found.end() - 1))
    return None


def _is_deferred(declaration: str | None) -> bool:
    head = (declaration or "").partition("::")[0]
    return bool(_DEFERRED_RE.search(head))


def _type_name(vartype: str | None) -> str | None:
    match = _TYPE_RE.match(vartype or "")
    return match.group(1).lower() if match else None


def _location_name(proc: Procedure) -> str:
    return Path(proc.path.replace("\\", "/")).name


# ------------------------------------------------------------ resolution

@dataclass
class _Entity:
    """A named thing a read can land in: a local, a module variable, a field."""

    name: str
    vartype: str | None
    declaration: str | None
    units: str | None
    description: str | None
    declared_at: str | None


class _Resolver:
    """Expand one procedure's read fields into columns."""

    def __init__(self, index: SourceIndex, proc: Procedure):
        self.index = index
        self.proc = proc
        self.problems: list[str] = []

    # -- extents

    def _integer(self, text: str) -> int | None:
        text = text.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        # A named constant: `dimension(mlyr)` with `integer, parameter ::
        # mlyr = 10` in a module the procedure can see.
        for variable in self.index.module_variables_named(text):
            if variable.is_parameter and variable.initial and \
                    re.fullmatch(r"[+-]?\d+", variable.initial.strip()):
                return int(variable.initial.strip())
        return None

    def _extent(self, dimension: str) -> int | None:
        if ":" in dimension:
            low, _, high = dimension.partition(":")
            if not high.strip():
                return None
            low_value = self._integer(low) if low.strip() else 1
            high_value = self._integer(high)
            if low_value is None or high_value is None:
                return None
            return high_value - low_value + 1
        return self._integer(dimension)

    def _elements(self, extents: list[str]) -> list[str] | None:
        """Subscripts of every element, first index fastest (Fortran order)."""
        sizes = [self._extent(extent) for extent in extents]
        if any(size is None or size < 1 for size in sizes):
            return None
        subscripts = [[]]
        for size in sizes:
            subscripts = [prefix + [i] for i in range(1, size + 1) for prefix in subscripts]
        return ["(" + ",".join(str(i) for i in item) + ")" for item in subscripts]

    # -- names

    def _root(self, name: str) -> _Entity | None:
        wanted = name.lower()
        for item in [*self.proc.arguments, *self.proc.locals]:
            if item.name.lower() == wanted:
                return _Entity(item.name, item.vartype, item.declaration,
                               item.units, item.description,
                               f"{_location_name(self.proc)}:{item.line}")
        candidates = self.index.module_variables_named(wanted)
        if len(candidates) > 1:
            # 15 names are declared in more than one module; the procedure's
            # own `use` statements say which one it means.
            visible = {
                use.module.lower() for use in self.proc.uses
                if not use.only or wanted in {
                    only.split("=>")[0].strip().lower() for only in use.only}
            }
            candidates = [c for c in candidates if c.module.lower() in visible]
        if len(candidates) != 1:
            return None
        variable = candidates[0]
        return _Entity(variable.name, variable.vartype, variable.declaration,
                       variable.units, variable.description,
                       f"{variable.module}:{variable.line}")

    def _component(self, type_name: str, name: str) -> Field | None:
        derived = self.index.derived_type(type_name)
        if derived is None:
            return None
        wanted = name.lower()
        return next((f for f in derived.fields if f.name.lower() == wanted), None)

    # -- expansion

    def _expand(self, entity: _Entity, path: str, subscripted: bool,
                depth: int = 0) -> list[Column] | None:
        """Every value a list-directed read of ``entity`` consumes."""
        if depth > 8:
            self.problems.append(f"{path}: nesting deeper than 8")
            return None
        extents = declared_extents(entity.declaration, entity.name)
        if extents and not subscripted:
            elements = None if _is_deferred(entity.declaration) else self._elements(extents)
            if elements is None:
                # Sized at run time -- one value per salt ion, per constituent.
                # The shape is known and the count is not, so the element's
                # columns are kept and marked to repeat: `*` for an allocated
                # array, the extent as written for an explicit one.
                deferred = _is_deferred(entity.declaration) or all(
                    not extent.replace(":", "").strip() for extent in extents)
                repeat = "*" if deferred else ",".join(extents)
                element = _Entity(entity.name, entity.vartype, None,
                                  entity.units, entity.description,
                                  entity.declared_at)
                expanded = self._expand(element, path, True, depth + 1)
                if expanded is None:
                    return None
                for column in expanded:
                    column.repeat = column.repeat or repeat
                return expanded
            columns: list[Column] = []
            for subscript in elements:
                element = _Entity(entity.name + subscript, entity.vartype,
                                  None, entity.units, entity.description,
                                  entity.declared_at)
                expanded = self._expand(element, path, True, depth + 1)
                if expanded is None:
                    return None
                columns.extend(expanded)
            return columns
        type_name = _type_name(entity.vartype)
        if type_name is None:
            return [Column(name=entity.name, path=path, vartype=entity.vartype,
                           units=entity.units, description=entity.description,
                           declared_at=entity.declared_at)]
        derived = self.index.derived_type(type_name)
        if derived is None:
            self.problems.append(f"{path}: type {type_name} is not in the index")
            return None
        columns = []
        for component in derived.fields:
            child = _Entity(component.name, component.vartype,
                            component.declaration, component.units,
                            component.description, component.location)
            expanded = self._expand(child, f"{path}%{component.name}", False, depth + 1)
            if expanded is None:
                return None
            columns.extend(expanded)
        return columns

    def reference(self, text: str) -> list[Column] | None:
        """Columns for one field of a read list: ``hru_db(i)%dbsc``, ``k``."""
        text = text.strip()
        if text.startswith("(") and text.endswith(")"):
            return self._implied_do(text)
        parts = []
        depth, current = 0, []
        for char in text:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "%" and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
            current.append(char)
        parts.append("".join(current).strip())
        names = [re.match(r"\w+", part) for part in parts]
        if not all(names):
            self.problems.append(f"{text}: not a variable reference")
            return None
        entity = self._root(names[0].group(0))
        if entity is None:
            self.problems.append(f"{text}: {names[0].group(0)} is not declared "
                                 "here or in exactly one visible module")
            return None
        for name in names[1:]:
            type_name = _type_name(entity.vartype)
            component = self._component(type_name, name.group(0)) if type_name else None
            if component is None:
                self.problems.append(f"{text}: no component {name.group(0)}")
                return None
            entity = _Entity(component.name, component.vartype,
                             component.declaration, component.units,
                             component.description, component.location)
        subscripted = "(" in parts[-1]
        return self._expand(entity, _strip_subscripts(text).lower(), subscripted)

    def _implied_do(self, text: str) -> list[Column] | None:
        """``(a(i), b(i), i = 1, n)``: the group's columns, marked to repeat."""
        items = _split_top(text[1:-1])
        control = next((k for k, item in enumerate(items)
                        if _KEYWORD_ARG_RE.match(item)), None)
        if control is None or control == 0:
            self.problems.append(f"{text}: not an implied-do list")
            return None
        bounds = items[control + 1:]
        repeat = bounds[0].strip() if bounds else "?"
        start = _KEYWORD_ARG_RE.sub("", items[control]).strip()
        if start != "1":
            repeat = f"{start}..{repeat}"
        columns: list[Column] = []
        for item in items[:control]:
            expanded = self.reference(item)
            if expanded is None:
                return None
            for column in expanded:
                column.repeat = repeat
            columns.extend(expanded)
        return columns

    def read(self, use: IOUse) -> tuple[list[Column], list[str]]:
        self.problems = []
        columns: list[Column] = []
        for item in use.fields:
            expanded = self.reference(item)
            if expanded is not None:
                columns.extend(expanded)
        return columns, list(self.problems)


# ------------------------------------------------------------ records

def _is_text(columns: list[Column], use: IOUse) -> bool:
    return (len(use.fields) == 1 and len(columns) == 1
            and (columns[0].vartype or "").lower().startswith("character"))


def _loops(index: SourceIndex, proc: Procedure, line: int) -> list[str] | None:
    scopes = index.scope_at(proc.path, line)
    if scopes is None:
        return None
    return [scope.index or scope.header for scope in scopes]


def _procedure_records(
    index: SourceIndex, proc: Procedure, uses: list[IOUse],
) -> tuple[list[PreambleLine], list[Record]]:
    """Preamble and records one procedure reads from one file."""
    uses = sorted(uses, key=lambda use: use.line)
    rewinds = [use.line for use in uses if use.op == "rewind"]
    reads = [use for use in uses if use.op == "read" and use.fields]
    # SWAT+ commonly counts records in a first pass, rewinds, and reads them
    # in a second. Only the second pass describes the layout.
    if rewinds and any(use.line > rewinds[-1] for use in reads):
        reads = [use for use in reads if use.line > rewinds[-1]]
    backspaces = [use.line for use in uses if use.op == "backspace"]

    resolver = _Resolver(index, proc)
    resolved = [(use, *resolver.read(use)) for use in reads]

    def is_probe(position: int) -> bool:
        """A read that only peeks at a line the next read consumes again."""
        if position + 1 >= len(resolved):
            return False
        use, following = resolved[position][0], resolved[position + 1][0]
        return any(use.line < line < following.line for line in backspaces)

    preamble: list[PreambleLine] = []
    data: list[tuple[IOUse, list[Column], list[str]]] = []
    for position, item in enumerate(resolved):
        use, columns, problems = item
        if is_probe(position):
            continue
        at = f"{_location_name(proc)}:{use.line}"
        if not data and _is_text(columns, use):
            preamble.append(PreambleLine(at=at, kind="text", reads=list(use.fields)))
            continue
        data.append(item)

    loop_lists = [_loops(index, proc, use.line) for use, _, _ in data]
    depths: list[int] = [len(loops) if loops is not None else 0
                         for loops in loop_lists]

    # A scalar beside a wider record in a deeper loop is file metadata, not
    # the table row. SWAT+ commonly writes a count and a header
    # before ``do i = 1, count; read (...) row``. Treating the count as data
    # made it the shallowest ``main`` record and demoted the real row to a
    # child. ``PreambleLine`` has always allowed value lines for this case.
    metadata = {
        position
        for position, (_, columns, _) in enumerate(data)
        if len(columns) == 1 and any(
            len(other_columns) > 1 and other_depth > depths[position]
            for (_, other_columns, _), other_depth in zip(data, depths)
        )
    }
    prefix = 0
    while prefix < len(data) and prefix in metadata:
        use, columns, _ = data[prefix]
        preamble.append(PreambleLine(
            at=f"{_location_name(proc)}:{use.line}",
            kind="text" if _is_text(columns, use) else "values",
            reads=list(use.fields),
        ))
        prefix += 1
    if prefix:
        data = data[prefix:]
        loop_lists = loop_lists[prefix:]
        depths = depths[prefix:]
        metadata = {position - prefix for position in metadata if position >= prefix}

    records: list[Record] = []
    for (use, columns, problems), loops in zip(data, loop_lists):
        records.append(Record(
            role="", procedure=proc.name,
            at=f"{_location_name(proc)}:{use.line}",
            loops=loops or [], columns=columns, unresolved=problems,
        ))
    if not records:
        return preamble, []
    candidates = [k for k in range(len(records)) if k not in metadata]
    if not candidates:
        candidates = list(range(len(records)))
    main_depth = min(depths[k] for k in candidates)
    top = [k for k in candidates if depths[k] == main_depth]
    main = max(top, key=lambda k: (len(records[k].columns), -k))
    main_fields = [item.lower() for item in data[main][0].fields]
    for k, record in enumerate(records):
        fields = [item.lower() for item in data[k][0].fields]
        shared = min(len(fields), len(main_fields))
        if k == main:
            record.role = "main"
        elif k in metadata:
            record.role = "line"
        elif depths[k] > main_depth:
            record.role = "child"
        elif fields[:shared] == main_fields[:shared]:
            # One read's list begins the other's: the same record read two
            # ways, as `plants.plt` is with and without `pl_class` depending on
            # `bsn_cc%nam1`. Which branch applies is not in the facts.
            record.role = "alternative"
        else:
            record.role = "line"
    return preamble, records


def file_layout(index: SourceIndex, file: str) -> FileLayout | None:
    """The layout of one input file, or ``None`` if nothing reads it."""
    uses = index.io_for_file(file)
    readers: dict[str, list[IOUse]] = {}
    for use in uses:
        readers.setdefault(use.procedure.lower(), []).append(use)
    per_reader: list[tuple[str, list[PreambleLine], list[Record]]] = []
    for name, reader_uses in readers.items():
        if not any(use.op == "read" and use.fields for use in reader_uses):
            continue
        proc = index.procedure(name)
        if proc is None:
            continue
        preamble, records = _procedure_records(index, proc, reader_uses)
        if records:
            per_reader.append((proc.name, preamble, records))
    if not per_reader:
        return None

    # Four files have more than one reader in 62.0.0. The one whose main
    # record reads the most is the layout; the others' mains are alternatives.
    def width(item: tuple[str, list[PreambleLine], list[Record]]) -> int:
        main = next((r for r in item[2] if r.role == "main"), None)
        return len(main.columns) if main else 0

    per_reader.sort(key=width, reverse=True)
    records: list[Record] = []
    for position, (_, _, reader_records) in enumerate(per_reader):
        for record in reader_records:
            if position and record.role == "main":
                record.role = "alternative"
            records.append(record)
    name = uses[0].file
    return FileLayout(
        file=name,
        filename_is_default=bool(re.fullmatch(r"[\w\-]+\.[\w.\-]+", name)),
        readers=[item[0] for item in per_reader],
        preamble=per_reader[0][1],
        records=records,
    )


def file_layouts(index: SourceIndex) -> dict[str, FileLayout]:
    """Every file the index shows being read, by lowercased file name."""
    layouts: dict[str, FileLayout] = {}
    for key in sorted(index.io_by_file):
        layout = file_layout(index, key)
        if layout is not None:
            layouts[key] = layout
    return layouts


# ------------------------------------------------------------ JSON

def _column_json(position: int, column: Column) -> dict[str, Any]:
    item: dict[str, Any] = {
        "position": position,
        "name": column.name,
        "path": column.path,
        "vartype": column.vartype,
        "units": column.units,
        "description": column.description,
        "declared_at": column.declared_at,
    }
    if column.repeat is not None:
        item["repeat"] = column.repeat
    return item


def layout_json(layout: FileLayout) -> dict[str, Any]:
    return {
        "file": layout.file,
        "filename_is_default": layout.filename_is_default,
        "readers": layout.readers,
        "preamble": [
            {"at": line.at, "kind": line.kind, "reads": line.reads}
            for line in layout.preamble
        ],
        "data_starts_after": len(layout.preamble),
        "records": [
            {
                "role": record.role,
                "procedure": record.procedure,
                "at": record.at,
                "loops": record.loops,
                "complete": record.complete,
                "unresolved": record.unresolved,
                "columns": [
                    _column_json(position, column)
                    for position, column in enumerate(record.columns, start=1)
                ],
            }
            for record in layout.records
        ],
    }


def layouts_json(index: SourceIndex) -> dict[str, Any]:
    """Every layout, with the provenance of the facts it was derived from."""
    provenance = index.provenance
    return {
        "layout_format": LAYOUT_FORMAT,
        "provenance": {
            "source_commit": provenance.source_commit,
            "source_describe": provenance.source_describe,
            "source_fingerprint": provenance.source_fingerprint,
            "parser_commit": provenance.parser_commit,
            "format_version": provenance.format_version,
        },
        "files": {
            key: layout_json(layout)
            for key, layout in file_layouts(index).items()
        },
    }
