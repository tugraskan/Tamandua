"""Read-only MCP server over the SWAT+ source index.

Serves :mod:`tamandua.index` -- it does no parsing of its own.

Two ways to start it, and the difference is who can run it:

    # builds from source; needs a SWAT+ checkout and swatplus-reference-corpus
    swatplus-mcp --source /path/to/swatplus

    # loads a published facts file and a matching sibling RHS sidecar if present
    swatplus-mcp --facts swatplus-facts.json

The second form makes these tools usable without either build-time checkout.
Build snapshots with ``swatplus-build`` and publish the base and sidecar as
separate release assets.

Tool responses are deliberately terse. The point is to spend fewer tokens than
the assistant would spend finding the answer itself, and a verbose response
gives that back. Tool *descriptions* state that results are exhaustive rather
than sampled -- without that, a model that gets one row back spends further
calls checking whether it was truncated.
"""

from __future__ import annotations

import argparse
from importlib import resources
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from tamandua.index import (
    IndexError_,
    SourceIndex,
    build_source_index,
    load_snapshot,
    source_fingerprint,
)
from tamandua.output.reader import OutputError, query as query_output

PROTOCOL_VERSION = "2024-11-05"
BUNDLED_FACTS = "data/swatplus-facts.json"

def _rows(items: list[Any]) -> list[dict]:
    return [asdict(i) if is_dataclass(i) else i for i in items]


def t_find_procedure(index: SourceIndex, name: str) -> Any:
    proc = index.procedure(name)
    if proc is None:
        return {"name": name, "found": "no"}
    answer = {
        "name": proc.name,
        "at": proc.location,
        "module": proc.module or "none",
        "uses": _rows(proc.uses) or "none",
        "arguments": _rows(proc.arguments) or "none",
        "locals": _rows(proc.locals) or "none",
    }
    if proc.select_cases:
        answer["select_cases"] = _rows(proc.select_cases)
    return answer


def t_callers(index: SourceIndex, procedure: str) -> Any:
    # Echo the question. A bare list renders as an unlabelled word -- one real
    # session read `command` as an empty response and fell back to grep.
    return {"procedure": procedure, "callers": index.callers_of(procedure) or "none"}


def t_callees(index: SourceIndex, procedure: str) -> Any:
    return {"procedure": procedure, "callees": index.callees_of(procedure) or "none"}


def t_file_io(index: SourceIndex, file: str) -> Any:
    return _rows(index.io_for_file(file))


def t_unit_users(index: SourceIndex, unit: str, op: str = "") -> Any:
    return _rows(index.io_for_unit(unit, op))


def t_writers(index: SourceIndex, variable: str) -> Any:
    return {
        "variable": variable,
        "assignments": index.writer_details(variable) or "none",
    }


def t_loops(index: SourceIndex, procedure: str) -> Any:
    return _rows(index.loops_in(procedure))


def t_provenance(index: SourceIndex) -> Any:
    return {
        **asdict(index.provenance),
        "scanner_warning_count": len(index.scanner_warnings),
    }


def _warning_rows(index: SourceIndex, procedures: set[str]) -> list[dict]:
    """Warnings relevant to the procedures named by one tool answer."""

    seen: set[tuple[str, int, str]] = set()
    rows: list[dict] = []
    for procedure in sorted(procedures):
        for warning in index.warnings_for_procedure(procedure):
            key = (warning.file, warning.line, warning.code)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "code": warning.code,
                "at": f"{warning.file}:{warning.line}",
                "procedure": warning.procedure or "file-level",
                "message": warning.message,
            })
    return rows


def _procedures_in_payload(name: str, args: dict, payload: Any) -> set[str]:
    """Find procedure names already present in a tool's question or answer."""

    procedures: set[str] = set()
    if isinstance(args.get("procedure"), str):
        procedures.add(args["procedure"])
    if name == "find_procedure" and isinstance(args.get("name"), str):
        procedures.add(args["name"])

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"procedure", "in"} and isinstance(item, str):
                    procedures.add(item)
                elif key == "assigned_at" and isinstance(item, list):
                    for site in item:
                        if isinstance(site, str) and ":" in site:
                            procedures.add(site.rsplit(":", 1)[0])
                elif key == "at" and isinstance(item, str) and ":" in item:
                    procedures.add(item.rsplit(":", 1)[0])
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return procedures


def t_search_fields(index: SourceIndex, text: str) -> Any:
    """Ordinary words to an identifier, from what the source says a field means."""
    hits = index.search_fields(text)
    if not hits:
        return {"query": text, "matches": "none"}
    return [
        {"field": f.path, "type": f.vartype or "none", "units": f.units or "none",
         "means": f.description or "none", "at": f.location}
        for f in hits
    ]


def t_describe_type(index: SourceIndex, name: str) -> Any:
    """Every field of a derived type -- what `aqu_d(iaq)` actually contains."""
    derived = index.derived_type(name)
    if derived is None:
        return {"type": name, "found": "no"}
    return [
        {"field": f.name, "type": f.vartype or "none", "units": f.units or "none",
         "means": f.description or "none"}
        for f in derived.fields
    ]


def t_call_path(index: SourceIndex, procedure: str) -> Any:
    """How execution reaches a routine, from an entry point down."""
    paths = index.paths_to(procedure)
    if not paths:
        return {"procedure": procedure, "paths": "none resolved"}
    return [{"path": " -> ".join(p)} for p in paths]


def t_breakpoint(index: SourceIndex, variable: str) -> Any:
    """Where to stop to watch a variable, and on what condition.

    The chain a debugging question needs: who assigns it, what loops enclose
    that line, and -- when the routine has no loop of its own -- which caller
    supplies the index. A loop listing alone cannot answer the last case, which
    is the common one for per-object routines.
    """
    result = index.breakpoint_for(variable)
    if not result.get("stops"):
        return {"variable": variable, "assigned": "nowhere found"}
    rows = []
    for stop in result["stops"]:
        row = {"break_at": stop["at"], "in": stop["procedure"]}
        if stop.get("scope") == "unresolved":
            row["scope"] = "loop nesting unresolved when the index was built"
        elif stop.get("loops"):
            row["indexes"] = ",".join(
                loop["index"] or "while" for loop in stop["loops"])
            row["condition"] = stop.get("condition", "")
        else:
            row["indexes"] = "none here"
            row["index_from"] = ",".join(stop.get("callers", [])) or "unknown"
        rows.append(row)
    return rows


def t_scope_at(index: SourceIndex, procedure: str, line: str) -> Any:
    """Which loops enclose a given line of a procedure."""
    proc = index.procedure(procedure)
    if proc is None:
        return {"procedure": procedure, "found": "no"}
    if not str(line).strip().isdigit():
        return {"procedure": procedure, "error": f"line must be a number, got {line!r}"}
    scopes = index.scope_at(proc.path, int(line))
    if scopes is None:
        return {"procedure": procedure,
                "scope": "loop nesting unresolved when the index was built"}
    if not scopes:
        return {"procedure": procedure, "line": line, "loops": "none",
                "index_from": ",".join(index.callers_of(procedure)) or "unknown"}
    return [{"index": s.index or "while", "lines": f"{s.start}-{s.end}",
             "header": s.header} for s in scopes]


def t_read_output(index: SourceIndex, file: str, column: str,
                  where: str = "", label_by: str = "") -> Any:
    """Summarise a column of a run's output.

    The one question no index can answer: a run's numbers exist only on the
    machine that produced them. Returns the shape of the series rather than the
    series -- and declines on files whose header does not line up with their
    data, where indexing a column by position answers confidently and wrongly.
    """
    path = Path(file)
    if not path.is_absolute():
        path = Path(index.provenance.source_path).parent / file
    filters: dict[str, str] = {}
    for clause in filter(None, (c.strip() for c in where.split(","))):
        key, _, value = clause.partition("=")
        if value:
            filters[key.strip()] = value.strip()
    try:
        summary = query_output(path, column, where=filters or None,
                               label_by=label_by or None)
    except OutputError as exc:
        return {"file": file, "error": str(exc)}
    return {"result": summary.render()}


def _one(prop: str, desc: str) -> dict:
    return {
        "type": "object",
        "properties": {prop: {"type": "string", "description": desc}},
        "required": [prop],
    }


TOOLS: list[tuple[str, str, dict, Callable]] = [
    ("find_procedure", "Locate a SWAT+ procedure: source file, line range, module, "
                       "module imports and only lists, every argument and local "
                       "declaration with source line/type/initial/inline units and "
                       "meaning, and select-case vocabularies. All stored fields "
                       "are returned, never sampled or truncated. "
                       "Returns advisory scanner warnings in a second content "
                       "block when this procedure has any; these are not compiler errors.",
     _one("name", "Procedure name, e.g. aqu_read"), t_find_procedure),
    ("callers", "Routines that call this procedure. Exhaustive over the whole "
                "tree, never sampled or truncated -- one result means exactly "
                "one caller, not a partial list.",
     _one("procedure", "Procedure name"), t_callers),
    ("callees", "Routines this procedure calls. Exhaustive, not truncated.",
     _one("procedure", "Procedure name"), t_callees),
    ("file_io", "Which routines open/read/write a file, with unit numbers and "
                "lines. Resolves output files opened via open_output_file. "
                "Exhaustive over the whole tree, not truncated.",
     _one("file", "File name, e.g. aquifer.aqu or aquifer_day.txt"), t_file_io),
    ("unit_users", "Routines using a Fortran unit number, and the file each use "
                   "targets. Optionally filter by operation. Exhaustive, not "
                   "truncated.",
     {"type": "object",
      "properties": {"unit": {"type": "string", "description": "Unit number, e.g. 107"},
                     "op": {"type": "string", "description": "Optional: open, read, or write"}},
      "required": ["unit"]}, t_unit_users),
    ("writers", "Routines that assign a variable, with line numbers and the "
                "complete logical assignment statement for derived-type paths "
                "when the RHS sidecar is installed. Exhaustive over the whole "
                "tree, never sampled or truncated. Relevant "
                "scanner warnings are returned separately when present.",
     _one("variable", "Variable or derived-type root, e.g. sw_volume_begin"), t_writers),
    ("loops", "Loop headers in a procedure with their index variables and start/end lines, "
              "for setting a conditional breakpoint. Exhaustive -- every loop in "
              "the procedure, not a sample. Relevant scanner warnings are "
              "returned separately when present.",
     _one("procedure", "Procedure name"), t_loops),
    ("provenance", "Which SWAT+ checkout and commit this index describes, its "
                   "compile-check status, and scanner-warning count.",
     {"type": "object", "properties": {}}, t_provenance),
    ("search_fields", "Find a variable from ordinary words. Searches what the "
                      "source says each field means, e.g. 'recharge' or "
                      "'lateral flow', and returns identifiers with units.",
     _one("text", "Words to look for, e.g. lateral flow"), t_search_fields),
    ("describe_type", "Every field of a derived type, with units and meaning -- "
                      "what a state object like aqu_d actually contains.",
     _one("name", "Type name, e.g. aquifer_dynamic"), t_describe_type),
    ("call_path", "How execution reaches a procedure, from an entry point down.",
     _one("procedure", "Procedure name, e.g. aqu_1d_control"), t_call_path),
    ("read_output", "Summarise a column of a run's output file: count, first, "
                    "last, min and max with where they occurred, and how many "
                    "values went negative.",
     {"type": "object",
      "properties": {
          "file": {"type": "string", "description": "Output file, e.g. hru_wb_aa.txt"},
          "column": {"type": "string", "description": "Column name, e.g. surq_gen"},
          "where": {"type": "string", "description": "Optional filter, e.g. name=hru0001"},
          "label_by": {"type": "string", "description": "Column naming where min/max occurred"},
      },
      "required": ["file", "column"]}, t_read_output),
    ("breakpoint", "Where to stop to watch a variable, and on what condition: "
                   "the routines that assign it, the loops enclosing each "
                   "assignment, and their index variables. Relevant scanner "
                   "warnings are returned separately when present.",
     _one("variable", "Variable or field path, e.g. aqu_d%rchrg"), t_breakpoint),
    ("scope_at", "Which loops enclose a given line of a procedure, outermost "
                 "first -- the variables live at that point. Relevant scanner "
                 "warnings are returned separately when present.",
     {"type": "object",
      "properties": {"procedure": {"type": "string", "description": "Procedure name"},
                     "line": {"type": "string", "description": "Line number"}},
      "required": ["procedure", "line"]}, t_scope_at),
]


def tool_specs() -> list[dict]:
    return [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]


def dispatch(index: SourceIndex, name: str, args: dict) -> Any:
    for tool_name, _, _, fn in TOOLS:
        if tool_name == name:
            return fn(index, **args)
    raise ValueError(f"unknown tool: {name}")


def _cell(key: str, value: Any) -> str:
    """One field of a compact row.

    Nested sequences are joined rather than repr'd -- `str(("a", "b"))` emits a
    Python tuple, quotes and all, which is noise the caller pays for. The unit
    number carries its name because unit and line are otherwise two bare numbers
    in adjacent columns; that exact ambiguity cost eight tool calls when the
    rendered index had it.
    """
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) if value else "none"
    if key == "unit" and value not in (None, "", "-"):
        return f"unit={value}"
    # An empty cell reads as a broken response rather than an absent value.
    return "none" if value is None or value == "" else str(value)


def render_compact(payload: Any) -> str:
    """Pipe-delimited rows instead of JSON.

    For the tabular results these tools return, a JSON envelope repeats every
    key on every row -- real cost, given the whole point is to spend fewer
    tokens than searching would.
    """
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            # Union of keys in first-seen order. Taking them from the first row
            # silently drops any column later rows add -- `breakpoint` returns a
            # condition only for the stops that sit inside a loop, and those
            # conditions vanished entirely.
            keys: list[str] = []
            for row in payload:
                for key in row:
                    if key not in keys:
                        keys.append(key)
            return "\n".join(
                ["|".join(keys)]
                + ["|".join(_cell(k, row.get(k)) for k in keys) for row in payload]
            )
        return "\n".join(str(x) for x in payload)
    if isinstance(payload, dict):
        return "|".join(f"{k}={_cell(k, v)}" for k, v in payload.items())
    return str(payload)


def handle(index: SourceIndex, request: dict, compact: bool) -> dict | None:
    """Turn one JSON-RPC request into a response, or None for a notification."""
    method, rid = request.get("method"), request.get("id")

    if method == "initialize":
        result: dict = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "swatplus-source", "version": "0.1.0"},
            "instructions": (
                "Use these facts-only tools before shell or web search for "
                "questions about SWAT+ Fortran source. For a named input or "
                "output file, call file_io first. Scanner warnings are advisory "
                "structure checks, not proof that the source compiles; call "
                "provenance for compile-check status."
            ),
        }
    elif method == "tools/list":
        result = {"tools": tool_specs()}
    elif method == "tools/call":
        params = request.get("params", {})
        name = params["name"]
        args = params.get("arguments", {})
        payload = dispatch(index, name, args)
        text = (render_compact(payload) if compact
                else json.dumps(payload, separators=(",", ":")))
        content = [{"type": "text", "text": text}]
        warning_rows = _warning_rows(
            index, _procedures_in_payload(name, args, payload)
        )
        if warning_rows:
            warning_payload = {"scanner_warnings": warning_rows}
            warning_text = (
                render_compact(warning_payload) if compact
                else json.dumps(warning_payload, separators=(",", ":"))
            )
            content.append({"type": "text", "text": warning_text})
        result = {"content": content}
    elif rid is None:
        return None
    else:
        result = {}

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class Current:
    """Keep an explicitly live source or facts file current between requests.

    MCP clients normally keep one server process alive across many edits. A
    server that only loads at startup can therefore return a stale answer that
    looks completely valid. Explicit ``--facts`` mode watches the file's size
    and nanosecond mtime; source mode fingerprints the working Fortran tree.

    When neither path is supplied, the index is a package-bundled release
    snapshot and intentionally remains static. Its provenance path names the
    machine that built it and must never be treated as a live checkout.
    """

    def __init__(self, index: SourceIndex, *, facts: Path | None = None,
                 source: Path | None = None, corpus: Path | None = None) -> None:
        if facts is not None and source is not None:
            raise ValueError("facts and source modes are mutually exclusive")
        self._index = index
        self._facts = facts
        self._source = source
        self._corpus = corpus
        self._stamp = self._facts_stamp()
        self._stale_message: str | None = None

    @property
    def stale_message(self) -> str | None:
        """Why a live source answer is unavailable, if candidate rebuild failed."""

        return self._stale_message

    def _facts_stamp(self) -> tuple[int, int] | None:
        if self._facts is None:
            return None
        try:
            stat = self._facts.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def get(self) -> SourceIndex:
        """Return the newest complete index available right now."""
        try:
            if self._facts is not None:
                stamp = self._facts_stamp()
                if stamp is not None and stamp != self._stamp:
                    refreshed = load_snapshot(self._facts)
                    self._index = refreshed
                    self._stamp = stamp
            elif self._source is not None:
                fingerprint = self._index.provenance.source_fingerprint
                if source_fingerprint(self._source) != fingerprint:
                    self._index = build_source_index(self._source, self._corpus)
                    self._stale_message = None
                else:
                    self._stale_message = None
        except (IndexError_, OSError) as exc:
            # A rebuild can briefly expose a truncated file, and a checkout can
            # be between states. Keep the last complete index for recovery, but
            # never serve it as though it describes the changed live source.
            if self._source is not None:
                self._stale_message = (
                    "live source changed, but its replacement index could not "
                    f"be built: {exc}. No source answer was returned; retry "
                    "after the source is readable and structurally indexable."
                )
        return self._index


def serve(index: SourceIndex | Current, stdin=sys.stdin, stdout=sys.stdout,
          compact: bool = False) -> None:
    current = index if isinstance(index, Current) else Current(index)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        index = current.get()
        if current.stale_message and request.get("method") == "tools/call":
            rid = request.get("id")
            response = None if rid is None else {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": current.stale_message}],
                    "isError": True,
                },
            }
        else:
            response = handle(index, request, compact)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def load_bundled_snapshot() -> SourceIndex:
    """Load the release snapshot shipped inside the Tamandua package."""
    resource = resources.files("tamandua").joinpath(BUNDLED_FACTS)
    if not resource.is_file():
        raise IndexError_(
            "no bundled SWAT+ facts file is installed; pass --facts or "
            "--source"
        )
    with resources.as_file(resource) as path:
        return load_snapshot(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=None,
                        help="build from a SWAT+ checkout instead of the "
                             "bundled snapshot (default: $SWATPLUS_SOURCE)")
    parser.add_argument("--corpus", type=Path, default=None,
                        help="swatplus-reference-corpus checkout (default: $SWATPLUS_REFERENCE_CORPUS)")
    parser.add_argument("--facts", type=Path, default=None, metavar="FILE",
                        help="serve this prebuilt facts JSON (default: the "
                             "snapshot bundled with Tamandua)")
    parser.add_argument("--compact", action="store_true",
                        help="return pipe-delimited rows instead of JSON")
    args = parser.parse_args(argv)
    current: Current
    try:
        if args.facts is not None:
            index = load_snapshot(args.facts)
            current = Current(index, facts=args.facts)
        elif (args.source is not None or args.corpus is not None
              or os.environ.get("SWATPLUS_SOURCE")):
            index = build_source_index(args.source, args.corpus)
            # build_source_index resolves repo roots to the actual Fortran
            # directory; provenance records exactly what must be fingerprinted.
            current = Current(index, source=Path(index.provenance.source_path),
                              corpus=args.corpus)
        else:
            index = load_bundled_snapshot()
            current = Current(index)
    except IndexError_ as exc:
        parser.exit(2, f"error: {exc}\n")
    serve(current, compact=args.compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
