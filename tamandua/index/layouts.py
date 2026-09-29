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
- **Links no comparison shows.** A column naming a row in another file is
  matched at run time by comparing strings; no declaration says so. Where the
  source shows the search -- ``if (hru_db(i)%dbsc%land_use_mgt ==
  lum(ilum)%name)`` inside ``do ilum`` -- the column carries a
  :class:`Reference` citing it (:func:`input_links`). Where it does not, as
  when the value was copied or passed to a routine first, nothing is claimed.

Works on any :class:`~tamandua.index.build.SourceIndex`, including one loaded
from the bundled snapshot, so it needs neither the parser nor a checkout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tamandua.index.build import Field, IOUse, Procedure, SourceIndex, path_prefixes

#: Bumped whenever the JSON shape changes, independent of the facts format.
#: Format 2 adds ``references`` to columns: the row of another file a column's
#: value names, with the comparison that shows it.
LAYOUT_FORMAT = "2"

_TYPE_RE = re.compile(r"^\s*(?:type|class)\s*\(\s*(\w+)\s*\)", re.IGNORECASE)
_DIMENSION_RE = re.compile(r"\bdimension\s*\(", re.IGNORECASE)
_DEFERRED_RE = re.compile(r"\b(allocatable|pointer)\b", re.IGNORECASE)
_KEYWORD_ARG_RE = re.compile(r"^\s*(\w+)\s*=(?!=)")


@dataclass
class Reference:
    """The row of another file a column's value names, and how that is known.

    SWAT+ declares no foreign keys. A reference is kept only where the source
    searches the target's rows for the column's value: an ``==`` inside a
    loop over the target array, ``if (hru_db(i)%dbsc%land_use_mgt ==
    lum(ilum)%name)`` inside ``do ilum``. ``evidence`` is every such site.
    """

    #: The file whose rows are searched, and the column compared against.
    file: str
    column: str
    path: str
    #: The target column's record and 1-based position in it, so a consumer
    #: can map it to a header by position, as it maps any other column.
    role: str
    position: int
    #: ``file.f90:line`` of each comparison.
    evidence: list[str] = field(default_factory=list)
    #: What carries the value to a comparison made elsewhere: the called
    #: routine's own test (``search.f90:22``) and each copy followed
    #: (``cli_staread.f90:70``). Empty for a comparison on the column itself.
    through: list[str] = field(default_factory=list)


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
    #: Rows of other files this column's value names; see :class:`Reference`.
    references: list[Reference] = field(default_factory=list)


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


def _subscript_names(text: str) -> set[str]:
    """Every name used inside a subscript: ``{"ipesti"}`` for ``a(ipesti)%b``."""
    names: set[str] = set()
    for inside in re.findall(r"\(([^()]*)\)", text):
        names.update(name.lower() for name in re.findall(r"[A-Za-z_]\w*", inside))
    return names


def _loops(index: SourceIndex, proc: Procedure, line: int) -> list[str] | None:
    """What ``scope_at`` answers, from the procedure's own loops.

    Only a procedure's own loops can enclose its lines, and ``scope_at``
    scans every loop in the tree on each call -- which, once every text read
    was asked, was most of the time a full layout run took.
    """
    if proc.path.replace("\\", "/") in index.unresolved_loop_files:
        return None
    return [
        loop.index or loop.header
        for loop in sorted(index.loops_in(proc.name), key=lambda loop: loop.line)
        if loop.end_line is not None and loop.line <= line <= loop.end_line
    ]


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

    def per_row(use: IOUse) -> bool:
        """A value stored under an enclosing loop's index, once per row.

        ``read (107,*) pest_soil_ini(ipesti)%name`` inside ``do ipesti`` is
        each block's name line, not a title: taken for preamble, it left
        ``data_starts_after`` one line too deep and the name out of the layout.
        """
        loops = _loops(index, proc, use.line) or []
        return bool({loop.lower() for loop in loops} & _subscript_names(use.fields[0]))

    preamble: list[PreambleLine] = []
    data: list[tuple[IOUse, list[Column], list[str]]] = []
    for position, item in enumerate(resolved):
        use, columns, problems = item
        if is_probe(position):
            continue
        at = f"{_location_name(proc)}:{use.line}"
        if not data and _is_text(columns, use) and not per_row(use):
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


def _file_layout(index: SourceIndex, file: str) -> FileLayout | None:
    """One file's layout, before references are attached."""
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

    # A reader that never opens the file, called by one that does, reads
    # inside its caller's record: `read_mgtops` reads each schedule's
    # operation lines for `mgt_read_mgtops`. Its records are children.
    openers = {use.procedure.lower() for use in uses if use.op == "open"}
    called = {
        name.lower() for name, _, _ in per_reader
        if name.lower() not in openers and any(
            name.lower() in {c.lower() for c in index.callees_of(opener)}
            for opener in openers)
    }
    if len(called) == len(per_reader):
        called = set()

    # Four files have more than one reader of their own in 62.0.0. The one
    # whose main record reads the most is the layout; the others' mains are
    # alternatives.
    def width(item: tuple[str, list[PreambleLine], list[Record]]) -> int:
        main = next((r for r in item[2] if r.role == "main"), None)
        return len(main.columns) if main else 0

    per_reader.sort(key=lambda item: (item[0].lower() in called, -width(item)))
    records: list[Record] = []
    for position, (name, _, reader_records) in enumerate(per_reader):
        for record in reader_records:
            if name.lower() in called:
                record.role = "child"
            elif position and record.role == "main":
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


def file_layout(index: SourceIndex, file: str) -> FileLayout | None:
    """The layout of one input file, or ``None`` if nothing reads it.

    Its columns carry their references, which takes every other file's
    layout to find: a reference names a column of the file searched.
    """
    return file_layouts(index).get(file.strip().lower())


def _raw_layouts(index: SourceIndex) -> dict[str, FileLayout]:
    return {key: layout for key in sorted(index.io_by_file)
            if (layout := _file_layout(index, key)) is not None}


def file_layouts(index: SourceIndex) -> dict[str, FileLayout]:
    """Every file the index shows being read, by lowercased file name."""
    layouts = _raw_layouts(index)
    _attach_references(index, layouts)
    return layouts


# ------------------------------------------------------------ links

@dataclass
class Link:
    """One comparison showing a column of one file names rows of another."""

    source_file: str
    source_path: str
    target_file: str
    target_path: str
    procedure: str
    #: ``file.f90:line`` of the comparison -- for one made in a called
    #: routine, the call.
    at: str
    #: The comparison as stored: ``hru_db(i)%dbsc%land_use_mgt ==
    #: lum(ilum)%name``.
    compares: str
    #: What carries a column's value to the comparison, when it is not
    #: compared where it was read: the called routine's comparison
    #: (``search.f90:22``) and each copy followed (``cli_staread.f90:70``).
    through: list[str] = field(default_factory=list)


@dataclass
class Unlinked:
    """A comparison between two variables that does not show a link, and why."""

    procedure: str
    at: str
    compares: str
    reason: str


@dataclass
class _Read:
    """One column as one read statement reads it."""

    file: str
    record: Record
    position: int
    column: Column
    #: The column's root is the reader's own local or argument, so the path
    #: means this variable only inside that reader.
    local: bool


def _is_local(proc: Procedure | None, root: str) -> bool:
    return proc is not None and any(
        item.name.lower() == root for item in [*proc.arguments, *proc.locals])


def _root_subscript_names(text: str) -> set[str]:
    match = re.match(r"\s*\w+\s*\(([^()]*)\)", text)
    if not match:
        return set()
    return {name.lower() for name in re.findall(r"[A-Za-z_]\w*", match.group(1))}


def _whole_array(text: str) -> bool:
    """``wst_n(:)``: an array a called routine searched, every element."""
    return bool(re.match(r"\s*\w+\s*\(\s*:\s*\)", text))


class _Reads:
    """Every column any read statement reads, by path.

    The layouts' records first, then every other read of the same files -- an
    earlier pass the layout leaves out, as ``pcp.cli`` reads each station's
    name into ``pcp_n(i)`` before reading the row into ``pcp(i)``. Those are
    role ``other``. Placeholders for a unit with no file (``unit_...``) are
    not files, so they are left out.
    """

    def __init__(self, index: SourceIndex, layouts: dict[str, FileLayout]):
        self.by_path: dict[str, list[_Read]] = {}
        for key, layout in layouts.items():
            if key.startswith("unit_"):
                continue
            seen: set[str] = set()
            for record in layout.records:
                seen.add(record.at)
                self._add(index, key, record)
            for use in index.io_for_file(key):
                proc = index.procedure(use.procedure)
                if use.op != "read" or not use.fields or proc is None:
                    continue
                at = f"{_location_name(proc)}:{use.line}"
                if at in seen:
                    continue
                seen.add(at)
                columns, problems = _Resolver(index, proc).read(use)
                self._add(index, key, Record(
                    role="other", procedure=proc.name, at=at, loops=[],
                    columns=columns, unresolved=problems))

    def _add(self, index: SourceIndex, file: str, record: Record) -> None:
        reader = index.procedure(record.procedure)
        for position, column in enumerate(record.columns, start=1):
            local = _is_local(reader, column.path.split("%")[0])
            self.by_path.setdefault(column.path, []).append(
                _Read(file, record, position, column, local))

    def find(self, path: str, procedure: str, local: bool) -> list[_Read]:
        """Columns read into the variable ``path`` names inside ``procedure``.

        A module variable is the same variable everywhere. A local is not:
        ``jday`` in one routine is unrelated to a ``jday`` another reads.
        """
        return [
            read for read in self.by_path.get(path, [])
            if read.local == local
            and (not local or read.record.procedure.lower() == procedure.lower())
        ]


def _line_of(site: str) -> int:
    line = site.rpartition(":")[2]
    return int(line) if line.isdigit() else 0


def _reaching(index: SourceIndex, found: list[_Read], path: str, procedure: str,
              line: int) -> list[_Read] | str:
    """The read of a local that reaches ``line``: the nearest one before it.

    A routine may reuse one local for several files -- gwflow_read reads
    ``dum_id`` from ponds.gw and then from pond_cell.gw -- so every read of
    it is not every value it holds at a given line. The nearest read before
    the line is taken, in source order; an assignment to the local in
    between means the value tested is not the one read.
    """
    before = [read for read in found if _line_of(read.record.at) < line]
    if not before:
        return f"{path} is read only after line {line}"
    last = max(_line_of(read.record.at) for read in before)
    for site in index.writers_of(path):
        name, _, at = site.rpartition(":")
        if name.lower() == procedure.lower() and at.isdigit() and last < int(at) < line:
            return f"{path} is assigned between its read and line {line}"
    return [read for read in before if _line_of(read.record.at) == last]


class _Sources:
    """Where the value a comparison tests was read, following copies."""

    def __init__(self, index: SourceIndex, reads: _Reads):
        self.index = index
        self.reads = reads

    def _site(self, procedure: str, line: int) -> str:
        proc = self.index.procedure(procedure)
        return f"{_location_name(proc) if proc else procedure}:{line}"

    def resolve(self, path: str, procedure: str, line: int,
                ) -> tuple[list[_Read], list[str]] | str:
        """The columns ``path`` holds inside ``procedure`` at ``line``, and
        the copies that carried them there; or why they cannot be shown."""
        found = self._resolve(path, procedure, line, [])
        if isinstance(found, str):
            return found
        terminals, through = found
        paths = sorted({read.column.path for read in terminals})
        if not terminals:
            return f"{path} is not a column any input file is read into"
        if len(paths) > 1:
            return f"{path} is assigned from {len(paths)} columns: {', '.join(paths)}"
        return terminals, through

    def _resolve(self, path: str, procedure: str, line: int, visiting: list,
                 ) -> tuple[list[_Read], list[str]] | str:
        proc = self.index.procedure(procedure)
        local = _is_local(proc, path.split("%")[0])
        scope = (procedure.lower() if local else "", path)
        if scope in visiting:
            # Copied back from itself (`hru_init = hru`, later `hru =
            # hru_init`): no value arrives this way that was not already there.
            return [], []
        direct = self.reads.find(path, procedure, local)
        if direct and local:
            reaching = _reaching(self.index, direct, path, procedure, line)
            return reaching if isinstance(reaching, str) else (reaching, [])
        if direct:
            return direct, []
        if len(visiting) >= 4:
            return f"{path} is copied more than four times over"
        terminals: list[_Read] = []
        through: list[str] = []
        assigned = False
        for prefix in path_prefixes(path):
            sites = [site for site in self.index.writers_of(prefix)
                     if not local or site.rpartition(":")[0].lower() == procedure.lower()]
            if not sites:
                continue
            assigned = True
            copies = {f"{copy.procedure}:{copy.line}": copy
                      for copy in self.index.copies_to(prefix)}
            for site in sites:
                copy = copies.get(site)
                if copy is None:
                    name, _, line = site.rpartition(":")
                    return (f"{prefix} is also assigned other than by a copy, "
                            f"at {self._site(name, int(line))}")
                source = copy.source_path
                if source is None:
                    return f"{prefix} is copied from {copy.source}, not a variable"
                found = self._resolve(source + path[len(prefix):], copy.procedure,
                                      copy.line, [*visiting, scope])
                if isinstance(found, str):
                    return found
                terminals.extend(found[0])
                site_at = self._site(copy.procedure, copy.line)
                for item in [site_at, *found[1]]:
                    if item not in through:
                        through.append(item)
        if not assigned:
            return f"{path} is not a column any input file is read into"
        return terminals, through


def _reads_rows_in_loop(index: SourceIndex, proc: Procedure, line: int,
                        loop: str, root: str) -> bool:
    """Whether the loop over ``loop`` enclosing ``line`` reads ``root(loop)``."""
    enclosing = [item for item in index.loops_in(proc.name)
                 if (item.index or "").lower() == loop and item.end_line is not None
                 and item.line <= line <= item.end_line]
    if not enclosing:
        return False
    start, end = enclosing[-1].line, enclosing[-1].end_line
    row = re.compile(rf"^\s*{re.escape(root)}\s*\(\s*{re.escape(loop)}\s*\)", re.IGNORECASE)
    return any(
        use.op == "read" and use.procedure.lower() == proc.name.lower()
        and start <= use.line <= end and any(row.match(item) for item in use.fields)
        for uses in index.io_by_file.values() for use in uses)


def _derive(
    index: SourceIndex, layouts: dict[str, FileLayout],
) -> tuple[list[Link], list[Unlinked], list[tuple[_Read, _Read, str, list[str]]]]:
    """Links, the comparisons that show none, and which columns each joins."""
    reads = _Reads(index, layouts)
    sources = _Sources(index, reads)
    links: list[Link] = []
    unlinked: list[Unlinked] = []
    joined: list[tuple[_Read, _Read, str, list[str]]] = []
    for name in sorted(index.comparisons):
        proc = index.procedure(name)
        for comparison in index.comparisons[name]:
            where = (f"{_location_name(proc)}:{comparison.line}" if proc
                     else f"{comparison.procedure}:{comparison.line}")
            compares = f"{comparison.left} {comparison.op} {comparison.right}"
            called: list[str] = []
            if comparison.via:
                callee, _, line = comparison.via.rpartition(":")
                callee_proc = index.procedure(callee)
                called = [f"{_location_name(callee_proc) if callee_proc else callee}:{line}"]

            def skip(reason: str) -> None:
                unlinked.append(Unlinked(comparison.procedure, where, compares, reason))

            if comparison.op != "==":
                skip("an inequality, not a search")
                continue
            sides = [(comparison.left, comparison.left_path, comparison.right_path),
                     (comparison.right, comparison.right_path, comparison.left_path)]
            searched = None
            loop_var = None
            whole = [side for side in sides if _whole_array(side[0])]
            if len(whole) == 1:
                # A called routine searched an array it was handed whole.
                searched = whole[0]
            elif comparison.loops is None:
                skip("the loops in this file could not be resolved")
                continue
            else:
                for loop in reversed(comparison.loops):
                    loop = loop.lower()
                    hits = [side for side in sides if loop in _subscript_names(side[0])]
                    if len(hits) == 1:
                        searched, loop_var = hits[0], loop
                        target_root = (searched[1] or "").split("%")[0]
                        if loop not in _root_subscript_names(searched[0]):
                            skip(f"the loop over {loop} runs over part of "
                                 f"{target_root}, not its rows")
                            searched = False
                        break
            if searched is False:
                continue
            if searched is None:
                skip("no enclosing loop runs over one side only")
                continue
            if loop_var and proc and _reads_rows_in_loop(
                    index, proc, comparison.line, loop_var,
                    (searched[1] or "").split("%")[0]):
                # Checking each row as it is read is not looking one up; the
                # link, if any, runs the other way.
                skip(f"the loop over {loop_var} reads "
                     f"{(searched[1] or '').split('%')[0]}'s rows; a test "
                     "inside it is not a search")
                continue
            _, target_path, source_path = searched
            if not target_path or not source_path:
                skip("an operand is not a field path")
                continue
            # The searched array may be a copy of a column: `wst_n(i) =
            # wst(i)%name`, element by element.
            found = sources.resolve(target_path, comparison.procedure, comparison.line)
            if isinstance(found, str):
                skip(found)
                continue
            targets, target_through = found
            found = sources.resolve(source_path, comparison.procedure, comparison.line)
            if isinstance(found, str):
                skip(found)
                continue
            source_reads, source_through = found
            if {read.column.path for read in source_reads} & \
                    {read.column.path for read in targets}:
                # `pcom(j)%pl(ipl) == plts_bsn(iplt)`, where plts_bsn is built
                # from the same plant names: a column tested against itself.
                skip("both sides hold the same column")
                continue
            through = [*called, *target_through,
                       *[site for site in source_through if site not in target_through]]
            seen: set[tuple[str, str, str, str]] = set()
            for source_read in source_reads:
                for target_read in targets:
                    joined.append((source_read, target_read, where, through))
                    key = (source_read.file, source_read.column.path,
                           target_read.file, target_read.column.path)
                    if key in seen:
                        continue
                    seen.add(key)
                    links.append(Link(
                        source_file=source_read.file,
                        source_path=source_read.column.path,
                        target_file=target_read.file,
                        target_path=target_read.column.path,
                        procedure=comparison.procedure, at=where,
                        compares=compares, through=list(through)))
    return links, unlinked, joined


def input_links(
    index: SourceIndex, layouts: dict[str, FileLayout] | None = None,
) -> tuple[list[Link], list[Unlinked]]:
    """Every comparison that shows an input-file link, and every one that does not.

    A comparison shows a link when all of these hold:

    - it tests ``==``;
    - one side is searched: an enclosing loop's index subscripts exactly that
      side, and the innermost such loop subscripts its root array --
      ``lum(ilum)`` under ``do ilum`` -- or a called routine was handed it as
      a whole array (``wst_n(:)``, see :class:`~tamandua.index.build.Comparison`);
    - the searched side is a column some input file is read into, and so is
      the other side -- directly, or through copies (``mgt =
      sched(isched)%mgt_ops(...)``) when every assignment to it is a copy and
      all of them lead back to one column. A local matches only a column its
      own routine reads.

    Each part is a fact in the index: operands, loops and calls are stored
    with the comparison, copies with the assignments, columns with the
    layouts'. Nothing is matched by name.
    """
    links, unlinked, _ = _derive(
        index, _raw_layouts(index) if layouts is None else layouts)
    return links, unlinked


def _attach_references(index: SourceIndex, layouts: dict[str, FileLayout]) -> None:
    """Give every layout column the references its comparisons show."""
    _, _, joined = _derive(index, layouts)
    by_column: dict[int, tuple[Column, dict[tuple[str, str], tuple[_Read, list[str], list[str]]]]] = {}
    for source, target, where, through in joined:
        if source.record.role == "other":
            continue  # not a column of any layout
        _, targets = by_column.setdefault(id(source.column), (source.column, {}))
        key = (target.file, target.column.path)
        # The first read the target column appears in stands for it.
        _, evidence, carried = targets.setdefault(key, (target, [], []))
        if where not in evidence:
            evidence.append(where)
        carried.extend(site for site in through if site not in carried)
    for column, targets in by_column.values():
        column.references = [
            Reference(file=target.file, column=target.column.name,
                      path=target.column.path, role=target.record.role,
                      position=target.position,
                      evidence=sorted(evidence, key=_site_order),
                      through=sorted(carried, key=_site_order))
            for (target, evidence, carried) in (
                targets[key] for key in sorted(targets))
        ]


def _site_order(site: str) -> tuple[str, int]:
    name, _, line = site.rpartition(":")
    return (name, int(line) if line.isdigit() else 0)


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
    # Always present, so an empty list says "no comparison shows one" rather
    # than "this file predates references".
    item["references"] = [
        {"file": ref.file, "column": ref.column, "path": ref.path,
         "role": ref.role, "position": ref.position, "evidence": ref.evidence,
         "through": ref.through}
        for ref in column.references
    ]
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


def links_json(index: SourceIndex) -> dict[str, Any]:
    """Every link, grouped by the two columns it joins, and every comparison
    between two variables that shows none, with why."""
    links, unlinked = input_links(index)
    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    for link in links:
        key = (link.source_file, link.source_path, link.target_file, link.target_path)
        grouped.setdefault(key, []).append(
            {"at": link.at, "procedure": link.procedure, "compares": link.compares,
             "through": link.through})
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
        "links": [
            {"file": source_file, "path": source_path,
             "target_file": target_file, "target_path": target_path,
             "evidence": evidence}
            for (source_file, source_path, target_file, target_path), evidence
            in sorted(grouped.items())
        ],
        "not_linked": [
            {"at": item.at, "procedure": item.procedure,
             "compares": item.compares, "reason": item.reason}
            for item in unlinked
        ],
    }
