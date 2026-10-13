"""Where a value is read: the reverse of ``writers``.

``writers`` answers "what assigns this variable". Nothing answered "where does
the code *use* this value" -- the question documentation review turns on: is
an input ever read at all, is it treated as a fraction or a percent, in which
formula. ``hru(j)%lumv%bmp_sed`` is documented in percent and used at
smp_bmpfixed.f90:46 as ``sedyld(j) * (1. - hru(j)%lumv%bmp_sed)``, a fraction;
only the reading site shows that.

Every executable statement inside a procedure is examined once, at build time,
and every variable reference it reads is recorded with the kind of read:

==================  ========================================================
``assignment``      on the right-hand side of ``=``, computed with
``copy``            the whole right-hand side, so the value is only moved:
                    ``a%x = b(i)%x`` (also ``=>``, and ``trim(b)``)
``condition``       in an ``if``, ``else if``, ``do while``, ``select case``,
                    ``case`` or ``where`` test
``loop_bounds``     a counted ``do`` loop's bounds or step
``call_argument``   in the argument list of a ``call``
``function_argument`` in the argument list of a function defined in the tree
``output``          in a ``write`` or ``print`` output list
``io_control``      in an I/O statement's control list: a unit, a ``file=``
``index``           a subscript, selecting an element of another variable
``allocate``        a bound in an ``allocate``
==================  ========================================================

A reference inside a subscript is an ``index`` read whatever the statement;
otherwise the innermost routine in the tree whose argument list holds it
decides, and the statement decides the rest. Intrinsics such as ``exp`` or
``max`` are transparent: ``x = exp(-k)`` computes with ``k``.

Not reads, and so not recorded: declarations, the input list of a ``read``
(the statement that *fills* the value), a ``do`` loop's index, an implied-do
index, a keyword argument's name, and comments. A name counts as a variable
only where Fortran puts it in scope -- the procedure's own arguments and
declarations, its host's, and module variables reached through ``use`` -- so
an intrinsic, a type constructor or an undeclared name is never mistaken for
one.

Known limits, each deliberate rather than guessed at: a structure read whole
(``call f(hru(j))``, ``a = hru(j)``) is recorded under the structure's own
path, not under every field it holds; a value read through a pointer is
recorded under the pointer; and an implicitly typed, undeclared variable is
not seen.

Everything here is text-level static analysis over the statements, with no
parser dependency of its own, so it is testable without the corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable

#: Every kind of read, in the order the module docstring explains them.
READ_KINDS: tuple[str, ...] = (
    "assignment", "copy", "condition", "loop_bounds", "call_argument",
    "function_argument", "output", "io_control", "index", "allocate",
)


@dataclass(frozen=True)
class Read:
    """One statement reading one variable.

    ``detail`` says what the read feeds, by kind: the assigned path for
    ``assignment``, ``copy`` and an ``index`` into an assignment's target; the
    indexed path for any other ``index``; the routine for ``call_argument`` and
    ``function_argument``; the construct (``if``, ``do while``, ...) for
    ``condition``; the loop index for ``loop_bounds``; the file, or the unit
    as written, for ``output``; the statement keyword for ``io_control``; the
    allocated object for ``allocate``.
    """

    procedure: str
    line: int
    kind: str
    detail: str | None = None

    @property
    def site(self) -> str:
        """``procedure:line``, the way ``writers`` names a site."""
        return f"{self.procedure}:{self.line}"

    def encode(self) -> str:
        """``procedure:line:kind[:detail]`` -- one compact string per read."""
        head = f"{self.procedure}:{self.line}:{self.kind}"
        return f"{head}:{self.detail}" if self.detail else head

    @classmethod
    def decode(cls, text: str) -> "Read":
        procedure, line, kind, *rest = text.split(":", 3)
        return cls(procedure=procedure, line=int(line), kind=kind,
                   detail=rest[0] if rest else None)


@dataclass(frozen=True)
class Scope:
    """What a name means inside one procedure.

    ``variables`` are the names Fortran puts in scope there, lowercased;
    ``procedures`` every routine defined in the tree, so a call to one can be
    told from an intrinsic. ``scalars`` are the variables declared numeric or
    logical with no dimension: a bracket after one of those is a function
    reference, since ``real :: amin1`` types the intrinsic and ``real ::
    chg_par`` the result of an external, and neither can be subscripted.
    """

    variables: frozenset[str]
    procedures: frozenset[str] = field(default_factory=frozenset)
    scalars: frozenset[str] = field(default_factory=frozenset)

    def is_variable(self, found: "_Designator") -> bool:
        """A reference to a variable, as opposed to a function reference."""
        root = found.names[0]
        if len(found.names) > 1:
            # Only a variable has components; one whose root is not in scope
            # here (a missed declaration) is still a variable.
            return True
        return root in self.variables and not (found.groups and root in self.scalars)


# ------------------------------------------------------------ statement text

def _strip_comment(text: str, quote: str | None = None) -> tuple[str, str | None]:
    """The code before any ``!`` outside a literal, and the quote still open."""
    if quote is None and "'" not in text and '"' not in text:
        marker = text.find("!")
        return (text if marker < 0 else text[:marker]), None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "!":
            return text[:index], quote
        index += 1
    return text, quote


def split_statements(code: str) -> list[str]:
    """The ``;``-separated statements on one logical line, outside literals."""
    parts: list[str] = []
    start, quote = 0, None
    for index, char in enumerate(code):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ";":
            parts.append(code[start:index])
            start = index + 1
    parts.append(code[start:])
    return [part.strip() for part in parts if part.strip()]


def logical_statements(text: str) -> list[tuple[int, str]]:
    """Every free-form statement as ``(first line, text)``.

    ``&`` continuations are joined with one space, comments removed, and a line
    carrying several ``;``-separated statements yields each of them under that
    line. A blank or comment-only line inside a continued statement does not
    end it -- the same contract as the corpus scanner's logical lines, so a
    statement starts on the line the scanner reports for it.
    """
    found: list[tuple[int, str]] = []
    pieces: list[str] = []
    start = 0
    quote: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        if pieces and (not raw.strip() or raw.lstrip().startswith("!")):
            continue
        continuing_quote = quote if pieces else None
        code, quote = _strip_comment(raw, continuing_quote)
        piece = code.strip()
        if not pieces:
            if not piece:
                quote = None
                continue
            start = number
        elif piece.startswith("&"):
            piece = piece[1:] if continuing_quote else piece[1:].lstrip()
        continued = piece.endswith("&")
        if continued:
            piece = piece[:-1] if quote else piece[:-1].rstrip()
        pieces.append(piece)
        if continued:
            continue
        joined = " ".join(item for item in pieces if item)
        pieces, quote = [], None
        found.extend((start, statement) for statement in split_statements(joined))
    if pieces:
        joined = " ".join(item for item in pieces if item)
        found.extend((start, statement) for statement in split_statements(joined))
    return found


# --------------------------------------------------------------- expressions

_NAME = re.compile(r"[A-Za-z_]\w*")
#: A literal number. The ``.`` of ``1.and.`` belongs to the operator.
_NUMBER = re.compile(
    r"(?:\d+(?:\.(?![A-Za-z]+\.)\d*)?|\.\d+)(?:[EeDdQq][+-]?\d+)?(?:_\w+)?")
#: ``.and.``, ``.eq.``, ``.true.`` and the rest.
_DOT_WORD = re.compile(r"\.[A-Za-z]+\.(?:_\w+)?")
#: Intrinsics that change a string's padding and nothing else, so an
#: assignment through one still only copies the value.
_PADDING = re.compile(r"^(?:trim|adjustl|adjustr)\s*\(", re.IGNORECASE)


def _closing(text: str, open_at: int) -> int | None:
    """Where the bracket opening at ``open_at`` closes, outside literals."""
    depth, quote = 0, ""
    for position in range(open_at, len(text)):
        char = text[position]
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
            if depth == 0:
                return position
    return None


def _skip_space(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t":
        position += 1
    return position


def _skip_literal(text: str, position: int) -> int:
    quote = text[position]
    position += 1
    while position < len(text):
        if text[position] == quote:
            if position + 1 < len(text) and text[position + 1] == quote:
                position += 2
                continue
            return position + 1
        position += 1
    return position


def split_top_level(text: str) -> list[str]:
    """Split on commas outside brackets and literals."""
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


@dataclass(frozen=True)
class _Designator:
    """``a(i)%b(j)%c``: the names, every bracketed group's span, the end."""

    names: tuple[str, ...]
    groups: tuple[tuple[int, int], ...]
    end: int

    @property
    def path(self) -> str:
        return "%".join(self.names)


def _designator(text: str, start: int) -> _Designator:
    match = _NAME.match(text, start)
    assert match is not None
    names = [match.group().lower()]
    groups: list[tuple[int, int]] = []
    position = match.end()
    while True:
        after = _skip_space(text, position)
        if after < len(text) and text[after] == "(":
            close = _closing(text, after)
            if close is None:
                break
            groups.append((after + 1, close))
            position = close + 1
            continue
        if after < len(text) and text[after] == "%":
            component = _NAME.match(text, _skip_space(text, after + 1))
            if component is None:
                break
            names.append(component.group().lower())
            position = component.end()
            continue
        break
    return _Designator(tuple(names), tuple(groups), position)


def _unwrap(text: str) -> str:
    """Remove enclosing parentheses and padding intrinsics around a whole."""
    text = text.strip()
    while True:
        if text.startswith("(") and _closing(text, 0) == len(text) - 1:
            text = text[1:-1].strip()
            continue
        padding = _PADDING.match(text)
        if padding and _closing(text, padding.end() - 1) == len(text) - 1:
            text = text[padding.end():-1].strip()
            continue
        return text


def _whole_variable(text: str, scope: Scope) -> tuple[str, _Designator] | None:
    """The text, unwrapped, when it is one variable reference and nothing else."""
    text = _unwrap(text)
    if not _NAME.match(text):
        return None
    found = _designator(text, 0)
    if found.end != len(text.rstrip()) or not scope.is_variable(found):
        return None
    return text, found


Found = list[tuple[str, str, str | None]]


def _scan(text: str, scope: Scope, kind: str, detail: str | None, out: Found) -> None:
    """Record every variable reference in an expression as one kind of read."""
    position = 0
    while position < len(text):
        char = text[position]
        if char in "'\"":
            position = _skip_literal(text, position)
            continue
        if char.isdigit() or (char == "." and text[position + 1:position + 2].isdigit()):
            number = _NUMBER.match(text, position)
            if number:
                position = number.end()
                continue
        if char == ".":
            word = _DOT_WORD.match(text, position)
            position = word.end() if word else position + 1
            continue
        if not (char.isalpha() or char == "_"):
            position += 1
            continue
        found = _designator(text, position)
        after = _skip_space(text, found.end)
        if (not found.groups and len(found.names) == 1 and after < len(text)
                and text[after] == "=" and text[after + 1:after + 2] not in ("=", ">")):
            # `kw = value` in an argument list, `i = 1, n` in an implied do:
            # the name is named, not read.
            position = found.end
            continue
        root = found.names[0]
        if scope.is_variable(found):
            out.append((found.path, kind, detail))
            for begin, end in found.groups:
                _scan(text[begin:end], scope, "index", found.path, out)
        elif found.groups:
            # A function reference. One defined in the tree takes its
            # arguments; an intrinsic is transparent.
            begin, end = found.groups[0]
            if root in scope.procedures:
                _scan(text[begin:end], scope, "function_argument", root, out)
            else:
                _scan(text[begin:end], scope, kind, detail, out)
            for begin, end in found.groups[1:]:
                _scan(text[begin:end], scope, kind, detail, out)
        # Anything else is a bare name not in scope: not a variable here.
        position = found.end


def _scan_subscripts(found: _Designator, text: str, scope: Scope, detail: str,
                     out: Found) -> None:
    for begin, end in found.groups:
        _scan(text[begin:end], scope, "index", detail, out)


# ---------------------------------------------------------------- statements

_LABEL = re.compile(r"^\d+\s+")
_CONSTRUCT_NAME = re.compile(
    r"^[A-Za-z_]\w*\s*:(?!:)\s*(?=(?:do|if|select|where|forall|block|associate)\b)",
    re.IGNORECASE)
_IF = re.compile(r"^(else\s*)?if\s*\(", re.IGNORECASE)
_WHERE = re.compile(r"^(?:else\s*)?where\s*\(", re.IGNORECASE)
#: `do while`, `do 10 while`, `do, while`: an optional label, then a comma.
_DO_WHILE = re.compile(r"^do\s*(?:\d+\s*)?,?\s*while\s*\(", re.IGNORECASE)
_DO_COUNTED = re.compile(r"^do\s*(?:\d+\s*)?,?\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$",
                         re.IGNORECASE | re.DOTALL)
_SELECT = re.compile(r"^select\s*case\s*\(", re.IGNORECASE)
_CASE = re.compile(r"^case\s*\(", re.IGNORECASE)
_CALL = re.compile(r"^call\s+([A-Za-z_]\w*(?:\s*%\s*[A-Za-z_]\w*)*)", re.IGNORECASE)
_IO = re.compile(
    r"^(read|write|print|open|close|inquire|rewind|backspace|endfile|flush|wait)\b",
    re.IGNORECASE)
_ALLOCATE = re.compile(r"^allocate\s*\(", re.IGNORECASE)
_DIMENSION = re.compile(r"^\s*dimension\b\s*(?:::)?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_KEYWORD = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?![=>])\s*", re.DOTALL)
#: Control-list specifiers an I/O statement writes into rather than reads.
_IO_OUTPUTS = frozenset({"iostat", "iomsg", "size", "newunit"})
#: In an ``inquire``, everything but these is an answer, not a question.
_INQUIRE_INPUTS = frozenset({"unit", "file", "id"})
_ALLOCATE_OUTPUTS = frozenset({"stat", "errmsg"})


def _assignment(text: str) -> tuple[str, str, str] | None:
    """``(target, operator, expression)`` when the statement assigns."""
    if not _NAME.match(text):
        return None
    target = _designator(text, 0)
    after = _skip_space(text, target.end)
    if text[after:after + 2] == "=>":
        return text[:target.end].strip(), "=>", text[after + 2:].strip()
    if text[after:after + 1] == "=" and text[after + 1:after + 2] != "=":
        return text[:target.end].strip(), "=", text[after + 1:].strip()
    return None


def _io_reads(keyword: str, text: str, scope: Scope, out: Found) -> None:
    rest = text[len(keyword):].strip()
    keyword = keyword.lower()
    control, items = "", rest
    if rest.startswith("("):
        close = _closing(rest, 0)
        if close is None:
            return
        control, items = rest[1:close], rest[close + 1:].strip()
    elif keyword in ("read", "print"):
        # `read 100, a` / `print *, a`: the format comes first, unbracketed.
        format_, _, items = rest.partition(",")
        _scan(format_, scope, "io_control", keyword, out)
    else:
        control, items = rest, ""
    unit: str | None = None
    for position, spec in enumerate(split_top_level(control)):
        keyword_arg = _KEYWORD.match(spec)
        name = keyword_arg.group(1).lower() if keyword_arg else None
        value = spec[keyword_arg.end():] if keyword_arg else spec
        if name in _IO_OUTPUTS or (keyword == "inquire" and name is not None
                                   and name not in _INQUIRE_INPUTS):
            continue
        if name == "unit" or (name is None and position == 0):
            unit = value.strip()
        _scan(value, scope, "io_control", keyword, out)
    if not items:
        return
    if keyword in ("write", "print"):
        target = f"unit={unit}" if unit else ("unit=*" if keyword == "print" else None)
        _scan(items, scope, "output", target, out)
    elif keyword == "read":
        _fill_reads(items, scope, out)


def _fill_reads(items: str, scope: Scope, out: Found) -> None:
    """A ``read`` input list: the items are filled, their subscripts are read."""
    for item in split_top_level(items):
        if item.startswith("(") and _closing(item, 0) == len(item) - 1:
            inner = split_top_level(item[1:-1])
            # An implied do: `(x(i), i = 1, n)`. The trailing `i = 1, n`
            # names the index and reads the bounds.
            loop_at = next((index for index, part in enumerate(inner)
                            if _KEYWORD.match(part)), len(inner))
            _fill_reads(", ".join(inner[:loop_at]), scope, out)
            bounds = inner[loop_at:]
            if bounds:
                first = _KEYWORD.match(bounds[0])
                rest = [bounds[0][first.end():]] if first else []
                _scan(", ".join(rest + bounds[1:]), scope, "index", None, out)
            continue
        if _NAME.match(item):
            found = _designator(item, 0)
            if found.end == len(item.rstrip()) and scope.is_variable(found):
                _scan_subscripts(found, item, scope, found.path, out)
                continue
        _scan(item, scope, "index", None, out)


def _call_reads(text: str, callee: str, end: int, scope: Scope, out: Found) -> None:
    after = _skip_space(text, end)
    if after >= len(text) or text[after] != "(":
        return
    close = _closing(text, after)
    if close is None:
        return
    _scan(text[after + 1:close], scope, "call_argument", callee, out)


def _allocate_reads(text: str, scope: Scope, out: Found) -> None:
    open_at = text.index("(")
    close = _closing(text, open_at)
    if close is None:
        return
    for item in split_top_level(text[open_at + 1:close]):
        keyword_arg = _KEYWORD.match(item)
        if keyword_arg:
            if keyword_arg.group(1).lower() not in _ALLOCATE_OUTPUTS:
                _scan(item[keyword_arg.end():], scope, "allocate", None, out)
            continue
        if _NAME.match(item):
            found = _designator(item, 0)
            # The object allocated is not read; its bounds are.
            for begin, end in found.groups[-1:] or ():
                _scan(item[begin:end], scope, "allocate", found.path, out)
            for begin, end in found.groups[:-1]:
                _scan(item[begin:end], scope, "index", found.path, out)
            continue
        _scan(item, scope, "allocate", None, out)


def statement_reads(statement: str, scope: Scope) -> Found:
    """Every ``(path, kind, detail)`` one statement reads, without repeats.

    ``statement`` is one logical statement with its comment removed, as
    :func:`logical_statements` returns it. A statement this does not
    recognise -- a declaration, ``use``, ``end``, ``format`` and the like --
    reads nothing.
    """
    out: Found = []
    _statement(statement, scope, out)
    seen: set[tuple[str, str, str | None]] = set()
    return [item for item in out if not (item in seen or seen.add(item))]


def _statement(text: str, scope: Scope, out: Found) -> None:
    text = _LABEL.sub("", text.strip())
    text = _CONSTRUCT_NAME.sub("", text)
    if not text:
        return

    assigned = _assignment(text)
    if assigned:
        target, operator, expression = assigned
        assigned_to = _designator(target, 0)
        path = assigned_to.path
        _scan_subscripts(assigned_to, target, scope, path, out)
        whole = _whole_variable(expression, scope) if expression else None
        if whole is not None:
            source, found = whole
            out.append((found.path, "copy", path))
            _scan_subscripts(found, source, scope, found.path, out)
        elif operator == "=":
            _scan(expression, scope, "assignment", path, out)
        return

    for pattern, construct in ((_IF, None), (_WHERE, "where")):
        match = pattern.match(text)
        if match:
            close = _closing(text, match.end() - 1)
            if close is None:
                return
            if construct is None:
                construct = "else if" if match.group(1) else "if"
            _scan(text[match.end():close], scope, "condition", construct, out)
            action = text[close + 1:].strip()
            # `then`, an arithmetic if's labels, or nothing: no statement.
            if action and action.lower() != "then" and not action[0].isdigit():
                _statement(action, scope, out)
            return

    match = _DO_WHILE.match(text)
    if match:
        close = _closing(text, match.end() - 1)
        if close is not None:
            _scan(text[match.end():close], scope, "condition", "do while", out)
        return
    match = _DO_COUNTED.match(text)
    if match:
        _scan(match.group(2), scope, "loop_bounds", match.group(1).lower(), out)
        return
    for pattern, construct in ((_SELECT, "select case"), (_CASE, "case")):
        match = pattern.match(text)
        if match:
            close = _closing(text, match.end() - 1)
            if close is not None:
                _scan(text[match.end():close], scope, "condition", construct, out)
            return
    match = _CALL.match(text)
    if match:
        callee = re.sub(r"\s+", "", match.group(1)).lower()
        _call_reads(text, callee, match.end(), scope, out)
        return
    match = _IO.match(text)
    if match:
        _io_reads(match.group(1), text, scope, out)
        return
    if _ALLOCATE.match(text):
        _allocate_reads(text, scope, out)


# ------------------------------------------------------------------- a file

@dataclass(frozen=True)
class Span:
    """One procedure's lines in a file, and the names in scope there."""

    procedure: str
    start: int
    end: int
    scope: Scope


def file_reads(
    text: str, spans: Iterable[Span],
) -> tuple[list[tuple[str, Read]], dict[str, str]]:
    """Every read in one file, and the statement text at each site.

    A statement belongs to the innermost procedure whose lines hold it; one
    outside every procedure -- a module's declarations -- reads nothing. The
    procedure's own header line is not one of its statements.

    The statement text is keyed by ``procedure:line``; several statements on
    one line share the key and are joined with ``; ``, as written.
    """
    ordered = sorted(spans, key=lambda span: (span.start, -span.end))
    owned: list[tuple[int, str, Span]] = []
    arrays: dict[int, set[str]] = {}
    for line, statement in logical_statements(text):
        owner = None
        for span in ordered:
            if span.start <= line <= span.end and (
                    owner is None or span.start >= owner.start):
                owner = span
        if owner is None or line == owner.start:
            continue
        owned.append((line, statement, owner))
        dimension = _DIMENSION.match(statement)
        if dimension:
            # `real :: k_rg` then `dimension k_rg(4,3)`: an array after all.
            arrays.setdefault(id(owner), set()).update(
                item.group().lower() for part in split_top_level(dimension.group(1))
                if (item := _NAME.match(part)))
    scopes = {id(span): replace(span.scope, scalars=span.scope.scalars - arrays[id(span)])
              for span in ordered if id(span) in arrays}
    reads: list[tuple[str, Read]] = []
    statements: dict[str, str] = {}
    for line, statement, owner in owned:
        found = statement_reads(statement, scopes.get(id(owner), owner.scope))
        if not found:
            continue
        site = f"{owner.procedure}:{line}"
        statements[site] = (f"{statements[site]}; {statement}"
                            if site in statements else statement)
        for path, kind, detail in found:
            reads.append((path, Read(owner.procedure, line, kind, detail)))
    return reads, statements
