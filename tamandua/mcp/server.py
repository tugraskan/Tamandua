"""Read-only MCP server over the SWAT+ source index.

Serves :mod:`tamandua.index` -- it does no parsing of its own.

Two ways to start it, and the difference is who can run it:

    # builds from source; needs a SWAT+ checkout and swatplus-reference-corpus
    swatplus-mcp --source /path/to/swatplus

    # loads a published facts file; needs neither (docs/decisions.md D-8)
    swatplus-mcp --facts swatplus-facts.json

The parser lives in a private repository, so the second form is what makes
these tools usable by anyone outside the team. Build one with
``swatplus-build`` and publish it as a release asset.

Tool responses are deliberately terse. The point is to spend fewer tokens than
the assistant would spend finding the answer itself, and a verbose response
gives that back. Tool *descriptions* state that results are exhaustive rather
than sampled -- without that, a model that gets one row back spends further
calls checking whether it was truncated.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from tamandua.index import (
    IndexError_,
    SourceIndex,
    build_source_index,
    load_snapshot,
)
from tamandua.output.reader import OutputError, query as query_output

PROTOCOL_VERSION = "2024-11-05"

#: Assignment sites returned per variable, matching the rendered index.
MAX_WRITE_SITES = 40


def _rows(items: list[Any]) -> list[dict]:
    return [asdict(i) if is_dataclass(i) else i for i in items]


def t_find_procedure(index: SourceIndex, name: str) -> Any:
    proc = index.procedure(name)
    if proc is None:
        return {"name": name, "found": "no"}
    return {"name": proc.name, "at": proc.location,
            "module": proc.module or "none"}


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
    sites = index.writers_of(variable)
    return {
        "variable": variable,
        "assigned_at": sites[:MAX_WRITE_SITES] or "none",
        **({"truncated": len(sites) - MAX_WRITE_SITES} if len(sites) > MAX_WRITE_SITES else {}),
    }


def t_loops(index: SourceIndex, procedure: str) -> Any:
    return _rows(index.loops_in(procedure))


def t_provenance(index: SourceIndex) -> Any:
    return asdict(index.provenance)


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
            row["scope"] = "loop nesting unresolved in this file"
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
        return {"procedure": procedure, "scope": "loop nesting unresolved in this file"}
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
    ("find_procedure", "Locate a SWAT+ procedure: source file, line range, module.",
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
    ("writers", "Routines that assign a variable, with line numbers. Exhaustive "
                "over the whole tree, not truncated (a long list is capped and "
                "says so explicitly with a count of what was cut).",
     _one("variable", "Variable or derived-type root, e.g. sw_volume_begin"), t_writers),
    ("loops", "Loop headers in a procedure with their index variables and lines, "
              "for setting a conditional breakpoint. Exhaustive -- every loop in "
              "the procedure, not a sample.",
     _one("procedure", "Procedure name"), t_loops),
    ("provenance", "Which SWAT+ checkout and commit this index describes.",
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
                   "assignment, and their index variables.",
     _one("variable", "Variable or field path, e.g. aqu_d%rchrg"), t_breakpoint),
    ("scope_at", "Which loops enclose a given line of a procedure, outermost "
                 "first -- the variables live at that point.",
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
                "output file, call file_io first."
            ),
        }
    elif method == "tools/list":
        result = {"tools": tool_specs()}
    elif method == "tools/call":
        params = request.get("params", {})
        payload = dispatch(index, params["name"], params.get("arguments", {}))
        text = (render_compact(payload) if compact
                else json.dumps(payload, separators=(",", ":")))
        result = {"content": [{"type": "text", "text": text}]}
    elif rid is None:
        return None
    else:
        result = {}

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(index: SourceIndex, stdin=sys.stdin, stdout=sys.stdout,
          compact: bool = False) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        response = handle(index, json.loads(line), compact)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=None,
                        help="SWAT+ checkout (default: $SWATPLUS_SOURCE)")
    parser.add_argument("--corpus", type=Path, default=None,
                        help="swatplus-reference-corpus checkout (default: $SWATPLUS_REFERENCE_CORPUS)")
    parser.add_argument("--facts", type=Path, default=None, metavar="FILE",
                        help="serve a prebuilt facts JSON instead of parsing "
                             "source; needs no checkout and no reference-corpus")
    parser.add_argument("--compact", action="store_true",
                        help="return pipe-delimited rows instead of JSON")
    args = parser.parse_args(argv)
    try:
        index = (load_snapshot(args.facts) if args.facts is not None
                 else build_source_index(args.source, args.corpus))
    except IndexError_ as exc:
        parser.exit(2, f"error: {exc}\n")
    serve(index, compact=args.compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
