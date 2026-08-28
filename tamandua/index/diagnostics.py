"""Cheap structural warnings for source that may not compile.

These checks deliberately stop short of compiler validation.  They run while a
facts file is built, never at query time, and report only damage that can be
identified from source structure without resolving Fortran types, interfaces,
modules, or build flags.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_SUBROUTINE_OPEN = re.compile(
    r"^\s*(?:(?:recursive|pure|elemental|module)\s+)*"
    r"subroutine\s+([a-z_]\w*)\s*(?:\((.*)\))?",
    re.IGNORECASE,
)
_FUNCTION_OPEN = re.compile(
    r"^\s*(?:(?:recursive|pure|elemental|module)\s+)*"
    r"(?:(?:[\w(),=*]+\s+)+)?function\s+([a-z_]\w*)\s*(?:\((.*)\))?",
    re.IGNORECASE,
)
_MODULE_OPEN = re.compile(r"^\s*module\s+(?!procedure\b)([a-z_]\w*)\b", re.IGNORECASE)
_PROGRAM_OPEN = re.compile(r"^\s*program\s+([a-z_]\w*)\b", re.IGNORECASE)
_TYPE_OPEN = re.compile(
    r"^\s*type\s*(?:,\s*[^:]+)?::\s*([a-z_]\w*)\b|"
    r"^\s*type\s+([a-z_]\w*)\b",
    re.IGNORECASE,
)
_END_NAMED = re.compile(
    r"^\s*end\s*(subroutine|function|module|program|type)\b"
    r"(?:\s+([a-z_]\w*))?",
    re.IGNORECASE,
)
_END_CONTROL = re.compile(r"^\s*end\s*(if|do|select)\b", re.IGNORECASE)
_IF_OPEN = re.compile(r"^\s*(?:\w+\s*:\s*)?if\s*\(.*\)\s*then\b", re.IGNORECASE)
_DO_OPEN = re.compile(r"^\s*(?:\w+\s*:\s*)?do\b(?!\s*\d)", re.IGNORECASE)
_SELECT_OPEN = re.compile(r"^\s*(?:\w+\s*:\s*)?select\s+case\b", re.IGNORECASE)
_CASE = re.compile(r"^\s*case\s*(?:\((.*)\)|default\b)", re.IGNORECASE)
_ELSE = re.compile(r"^\s*(?:else(?:\s*if\b)?|elseif\b)", re.IGNORECASE)
_IMPLICIT_NONE = re.compile(r"^\s*implicit\s+none\b", re.IGNORECASE)
_DECLARATION = re.compile(
    r"^\s*(?:integer|real|double\s+precision|logical|"
    r"character(?:\s*\([^)]*\))?|type\s*\([^)]+\)|class\s*\([^)]+\))"
    r"(?:\s*\*\s*\d+)?(?=\s|,|::)(.*)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"^\s*[a-z_]\w*(?:\([^()]*\))?(?:%[a-z_]\w*(?:\([^()]*\))?)*"
    r"\s*=\s*(?!=)(.*)$",
    re.IGNORECASE,
)
_TRAILING_OPERATOR = re.compile(
    r"(?:\+|-|\*|/|//|\*\*|,|=>|\.and\.|\.or\.|\.eqv\.|\.neqv\.)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScannerWarning:
    """One advisory finding tied to inspectable source evidence."""

    code: str
    message: str
    file: str
    line: int
    procedure: str | None = None


@dataclass
class _Block:
    kind: str
    line: int
    name: str | None = None
    procedure: str | None = None
    args: list[str] = field(default_factory=list)
    declarations: list[tuple[str, int]] = field(default_factory=list)
    implicit_none: bool = False
    case_labels: dict[str, int] = field(default_factory=dict)
    default_line: int | None = None


@dataclass(frozen=True)
class _LogicalLine:
    start: int
    end: int
    text: str
    quote: str | None = None


def scan_source_warnings(source_root: Path, files: Iterable[Path]) -> list[ScannerWarning]:
    """Return deterministic advisory warnings for the scanner's exact inputs."""

    warnings: list[ScannerWarning] = []
    seen_symbols: dict[tuple[str, str, str], tuple[str, int]] = {}
    for path in files:
        warnings.extend(_scan_file(source_root, path, seen_symbols))
    return sorted(
        warnings,
        key=lambda item: (item.file, item.line, item.code, item.message),
    )


def _scan_file(
    source_root: Path,
    path: Path,
    seen_symbols: dict[tuple[str, str, str], tuple[str, int]],
) -> list[ScannerWarning]:
    rel = path.relative_to(source_root).as_posix()
    warnings: list[ScannerWarning] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [ScannerWarning(
            code="unreadable_file",
            message=f"source file could not be read: {exc}",
            file=rel,
            line=1,
        )]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        warnings.append(ScannerWarning(
            code="invalid_utf8",
            message=f"invalid UTF-8 byte at offset {exc.start}; scanner used replacement text",
            file=rel,
            line=text_line(raw, exc.start),
        ))
        text = raw.decode("utf-8", errors="replace")

    logical, continuation = _logical_lines(text.splitlines())
    if continuation is not None:
        warnings.append(ScannerWarning(
            code="unfinished_continuation",
            message="continued statement reaches end of file",
            file=rel,
            line=continuation,
        ))

    stack: list[_Block] = []
    for item in logical:
        code, quote = item.text, item.quote
        stripped = code.strip()
        if not stripped:
            continue
        if quote is not None:
            procedure = _current_procedure(stack)
            warnings.append(ScannerWarning(
                code="unbalanced_quote",
                message=f"unterminated {quote} string literal",
                file=rel,
                line=item.start,
                procedure=procedure,
            ))
        if _paren_balance(code) != 0:
            procedure = _current_procedure(stack)
            warnings.append(ScannerWarning(
                code="unbalanced_parentheses",
                message="statement has unbalanced parentheses",
                file=rel,
                line=item.start,
                procedure=procedure,
            ))

        lowered = stripped.lower()
        if lowered.startswith("end"):
            end_named = _END_NAMED.match(code)
            if end_named:
                _close_block(
                    stack, end_named.group(1).lower(), item.start, rel, warnings,
                    explicit_name=end_named.group(2),
                )
                continue
            end_control = _END_CONTROL.match(code)
            if end_control:
                kind = {"if": "if", "do": "do", "select": "select"}[
                    end_control.group(1).lower()
                ]
                _close_block(stack, kind, item.start, rel, warnings)
                continue
            if lowered == "end":
                _close_bare_end(stack, item.start, rel, warnings)
                continue

        module = _MODULE_OPEN.match(code) if lowered.startswith("module ") else None
        if module:
            name = module.group(1)
            _remember_symbol(seen_symbols, stack, "module", name, rel, item.start, warnings)
            stack.append(_Block("module", item.start, name=name))
            continue

        program = _PROGRAM_OPEN.match(code) if lowered.startswith("program ") else None
        if program:
            name = program.group(1)
            _remember_symbol(seen_symbols, stack, "program", name, rel, item.start, warnings)
            stack.append(_Block("program", item.start, name=name))
            continue

        dtype = _TYPE_OPEN.match(code) if lowered.startswith(("type ", "type,")) else None
        if dtype and not lowered.startswith("type("):
            procedure = _current_procedure(stack)
            name = dtype.group(1) or dtype.group(2)
            _remember_symbol(seen_symbols, stack, "type", name, rel, item.start, warnings)
            stack.append(_Block("type", item.start, name=name, procedure=procedure))
            continue

        proc = _SUBROUTINE_OPEN.match(code) if "subroutine" in lowered else None
        kind = "subroutine"
        if proc is None and "function" in lowered:
            proc = _FUNCTION_OPEN.match(code)
            kind = "function"
        if proc:
            name = proc.group(1)
            args = _split_names(proc.group(2) or "")
            _remember_symbol(seen_symbols, stack, kind, name, rel, item.start, warnings)
            _warn_duplicate_names(args, "duplicate_argument", name, rel, item.start, warnings)
            stack.append(_Block(kind, item.start, name=name, procedure=name, args=args))
            continue

        if "if" in lowered[:40] and _IF_OPEN.match(code):
            procedure = _current_procedure(stack)
            stack.append(_Block("if", item.start, procedure=procedure))
            continue
        if "select" in lowered[:40] and _SELECT_OPEN.match(code):
            procedure = _current_procedure(stack)
            stack.append(_Block("select", item.start, procedure=procedure))
            continue
        if "do" in lowered[:40] and _DO_OPEN.match(code):
            procedure = _current_procedure(stack)
            stack.append(_Block("do", item.start, procedure=procedure))
            continue

        if lowered.startswith(("else", "elseif")) and _ELSE.match(code) and not _nearest(stack, "if"):
            procedure = _current_procedure(stack)
            warnings.append(ScannerWarning(
                code="else_without_if",
                message="else/else if has no open if block",
                file=rel,
                line=item.start,
                procedure=procedure,
            ))

        case = _CASE.match(code) if lowered.startswith("case") else None
        if case:
            procedure = _current_procedure(stack)
            selector = _nearest(stack, "select")
            if selector is None:
                warnings.append(ScannerWarning(
                    code="case_without_select",
                    message="case has no open select case block",
                    file=rel,
                    line=item.start,
                    procedure=procedure,
                ))
            else:
                _check_case(case.group(1), selector, rel, item.start, warnings)

        declaration_prefixes = (
            "integer", "real", "double", "logical", "character", "type(",
            "type (", "class(", "class (",
        )
        if lowered.startswith("implicit") or lowered.startswith(declaration_prefixes):
            active_proc = _nearest_procedure(stack)
        else:
            active_proc = None
        if active_proc is not None:
            if lowered.startswith("implicit") and _IMPLICIT_NONE.match(code):
                active_proc.implicit_none = True
            inside_type = stack and stack[-1].kind == "type"
            declaration = None if inside_type else _DECLARATION.match(code)
            if declaration:
                active_proc.declarations.extend(
                    (name, item.start) for name in _declaration_names(declaration.group(1))
                )

        assignment = _ASSIGNMENT.match(code) if "=" in code else None
        if assignment and _TRAILING_OPERATOR.search(assignment.group(1)):
            procedure = _current_procedure(stack)
            warnings.append(ScannerWarning(
                code="incomplete_assignment",
                message="assignment right-hand side ends with an operator",
                file=rel,
                line=item.start,
                procedure=procedure,
            ))

    while stack:
        block = stack.pop()
        _finish_procedure(block, rel, warnings)
        warnings.append(ScannerWarning(
            code="unclosed_block",
            message=f"{_block_label(block)} opened here is not closed",
            file=rel,
            line=block.line,
            procedure=block.procedure,
        ))
    return warnings


def _logical_lines(lines: list[str]) -> tuple[list[_LogicalLine], int | None]:
    """Join ordinary free-form ``&`` continuations without parsing Fortran."""

    found: list[_LogicalLine] = []
    pieces: list[str] = []
    start = 0
    for number, raw in enumerate(lines, start=1):
        code, quote = _strip_comment(raw)
        continued = code.rstrip().endswith("&")
        part = code.rstrip()
        if continued:
            part = part[:-1]
        if pieces:
            part = part.lstrip()
            if part.startswith("&"):
                part = part[1:].lstrip()
        elif continued:
            start = number
        if pieces or continued:
            pieces.append(part)
            if continued:
                continue
            joined, joined_quote = _strip_comment(" ".join(pieces))
            found.append(_LogicalLine(start, number, joined, joined_quote))
            pieces = []
            start = 0
        else:
            found.append(_LogicalLine(number, number, code, quote))
    if pieces:
        joined, joined_quote = _strip_comment(" ".join(pieces))
        found.append(_LogicalLine(start, len(lines), joined, joined_quote))
        return found, start
    return found, None


def _strip_comment(text: str) -> tuple[str, str | None]:
    if "'" not in text and '"' not in text:
        marker = text.find("!")
        return (text if marker < 0 else text[:marker]), None
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "!":
            return text[:index], quote
        index += 1
    return text, quote


def _paren_balance(text: str) -> int:
    if "(" not in text and ")" not in text:
        return 0
    if "'" not in text and '"' not in text:
        return text.count("(") - text.count(")")
    code = text
    quote: str | None = None
    balance = 0
    index = 0
    while index < len(code):
        char = code[index]
        if quote:
            if char == quote:
                if index + 1 < len(code) and code[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            balance += 1
        elif char == ")":
            balance -= 1
        index += 1
    return balance


def _close_block(
    stack: list[_Block],
    kind: str,
    line: int,
    rel: str,
    warnings: list[ScannerWarning],
    explicit_name: str | None = None,
) -> None:
    procedure = _current_procedure(stack)
    match_at = next((i for i in range(len(stack) - 1, -1, -1) if stack[i].kind == kind), None)
    if match_at is None:
        warnings.append(ScannerWarning(
            code="closer_without_opener",
            message=f"end {kind} has no matching open block",
            file=rel,
            line=line,
            procedure=procedure,
        ))
        return
    if match_at != len(stack) - 1:
        warnings.append(ScannerWarning(
            code="block_closed_out_of_order",
            message=f"end {kind} closes across an open {_block_label(stack[-1])}",
            file=rel,
            line=line,
            procedure=procedure,
        ))
    while len(stack) - 1 > match_at:
        orphan = stack.pop()
        _finish_procedure(orphan, rel, warnings)
    block = stack.pop()
    if explicit_name and block.name and explicit_name.lower() != block.name.lower():
        warnings.append(ScannerWarning(
            code="mismatched_end_name",
            message=f"end {kind} names {explicit_name}, but {block.name} was opened",
            file=rel,
            line=line,
            procedure=block.procedure,
        ))
    _finish_procedure(block, rel, warnings)


def _close_bare_end(
    stack: list[_Block], line: int, rel: str, warnings: list[ScannerWarning]
) -> None:
    units = {"subroutine", "function", "module", "program"}
    match_at = next((i for i in range(len(stack) - 1, -1, -1) if stack[i].kind in units), None)
    if match_at is None:
        warnings.append(ScannerWarning(
            code="closer_without_opener",
            message="bare end has no matching program unit",
            file=rel,
            line=line,
            procedure=_current_procedure(stack),
        ))
        return
    _close_block(stack, stack[match_at].kind, line, rel, warnings)


def _finish_procedure(block: _Block, rel: str, warnings: list[ScannerWarning]) -> None:
    if block.kind not in {"subroutine", "function"}:
        return
    declarations = [name.lower() for name, _ in block.declarations]
    counts = Counter(declarations)
    duplicate_lines: dict[str, int] = {}
    for name, line in block.declarations:
        lowered = name.lower()
        if counts[lowered] > 1:
            duplicate_lines.setdefault(lowered, line)
    for name, line in duplicate_lines.items():
        warnings.append(ScannerWarning(
            code="duplicate_declaration",
            message=f"{name} is declared more than once in {block.name}",
            file=rel,
            line=line,
            procedure=block.name,
        ))
    if block.implicit_none:
        declared = set(declarations)
        for arg in block.args:
            if arg.lower() not in declared:
                warnings.append(ScannerWarning(
                    code="undeclared_argument",
                    message=f"dummy argument {arg} has no declaration under implicit none",
                    file=rel,
                    line=block.line,
                    procedure=block.name,
                ))


def _remember_symbol(
    seen: dict[tuple[str, str, str], tuple[str, int]],
    stack: list[_Block],
    kind: str,
    name: str,
    rel: str,
    line: int,
    warnings: list[ScannerWarning],
) -> None:
    scope = "/".join(
        f"{block.kind}:{(block.name or '').lower()}"
        for block in stack
        if block.kind in {"module", "program", "subroutine", "function"}
    )
    key = (scope, kind, name.lower())
    previous = seen.get(key)
    if previous:
        warnings.append(ScannerWarning(
            code="duplicate_symbol",
            message=f"duplicate {kind} {name} in the same scope; first at {previous[0]}:{previous[1]}",
            file=rel,
            line=line,
            procedure=_current_procedure(stack),
        ))
    else:
        seen[key] = (rel, line)


def _warn_duplicate_names(
    names: list[str], code: str, procedure: str, rel: str, line: int,
    warnings: list[ScannerWarning],
) -> None:
    for name, count in Counter(value.lower() for value in names).items():
        if count > 1:
            warnings.append(ScannerWarning(
                code=code,
                message=f"dummy argument {name} appears more than once in {procedure}",
                file=rel,
                line=line,
                procedure=procedure,
            ))


def _declaration_names(tail: str) -> list[str]:
    text = tail.split("::", 1)[-1]
    names: list[str] = []
    for item in _split_top_level(text):
        name = item.split("=", 1)[0].split("=>", 1)[0].strip()
        match = re.match(r"([a-z_]\w*)", name, re.IGNORECASE)
        if match:
            names.append(match.group(1))
    return names


def _split_names(text: str) -> list[str]:
    return [
        match.group(1)
        for item in _split_top_level(text)
        if (match := re.match(r"\s*([a-z_]\w*)", item, re.IGNORECASE))
    ]


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _check_case(
    body: str | None,
    selector: _Block,
    rel: str,
    line: int,
    warnings: list[ScannerWarning],
) -> None:
    if body is None:
        if selector.default_line is not None:
            warnings.append(ScannerWarning(
                code="duplicate_case_default",
                message=f"second case default; first is at line {selector.default_line}",
                file=rel,
                line=line,
                procedure=selector.procedure,
            ))
        else:
            selector.default_line = line
        return
    for label in _split_top_level(body):
        normalized = re.sub(r"\s+", "", label).lower()
        previous = selector.case_labels.get(normalized)
        if previous is not None:
            warnings.append(ScannerWarning(
                code="duplicate_case_label",
                message=f"case label {label.strip()} repeats line {previous}",
                file=rel,
                line=line,
                procedure=selector.procedure,
            ))
        else:
            selector.case_labels[normalized] = line


def _nearest(stack: list[_Block], kind: str) -> _Block | None:
    return next((block for block in reversed(stack) if block.kind == kind), None)


def _nearest_procedure(stack: list[_Block]) -> _Block | None:
    return next(
        (block for block in reversed(stack) if block.kind in {"subroutine", "function"}),
        None,
    )


def _current_procedure(stack: list[_Block]) -> str | None:
    block = _nearest_procedure(stack)
    return block.name if block else None


def _block_label(block: _Block) -> str:
    return f"{block.kind} {block.name}" if block.name else block.kind


def text_line(raw: bytes, offset: int) -> int:
    return raw[:offset].count(b"\n") + 1
