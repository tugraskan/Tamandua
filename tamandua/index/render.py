"""Render a :class:`SourceIndex` as a compact, greppable markdown file.

One record per line, ``|``-separated, so an assistant answers a question with a
single grep instead of reading the source tree. Measured at ~97% fewer bytes
than grepping raw Fortran (docs/index_experiment.md).
"""

from __future__ import annotations

from tamandua.index.build import SourceIndex

#: Assignment sites listed per variable before truncating.
MAX_WRITE_SITES = 24
#: I/O line numbers listed per (file, op, unit, routine) before truncating.
MAX_IO_LINES = 8
#: Statement variables listed per I/O record before truncating.
MAX_IO_FIELDS = 12


def render_index(index: SourceIndex) -> str:
    out: list[str] = []
    w = out.append

    w("# SWAT+ source index")
    w("")
    w("Generated from Fortran source by `scripts/build_index.py`. Facts only --")
    w("every line is static analysis; no prose, nothing written by a model.")
    w("")
    w("Grep this file before searching the source tree. One record per line,")
    w("`|`-separated: `grep '^name|'` finds a routine, `grep '^varname|'` in the")
    w("assignments section finds everything that writes a variable.")
    w("")
    w("## Provenance")
    w("")
    w("```")
    for line in index.provenance.as_lines():
        w(line)
    w("```")
    w("")

    procs = sorted(index.procedures.values(), key=lambda p: p.name.lower())

    w("## Procedures")
    w("")
    w("`name | file:line | module | calls | called-by`")
    w("")
    w("```")
    for proc in procs:
        w("|".join([
            proc.name,
            proc.location,
            proc.module or "-",
            ",".join(index.callees_of(proc.name)) or "-",
            ",".join(index.callers_of(proc.name)) or "-",
        ]))
    w("```")
    w("")

    w("## File I/O")
    w("")
    w("Which routine opens/reads/writes which file, on which unit. This is the")
    w("lookup grep cannot do: the filename is usually nowhere near the read, and")
    w("output files are opened via `open_output_file` rather than `open`.")
    w("")
    w("`file | op | unit=N | routine | lines | variables in the statement`")
    w("")
    w("The unit is written `unit=2520` rather than bare: a grep hit arrives")
    w("without this heading, and unit and line are otherwise two bare numbers")
    w("in adjacent columns -- which is exactly the confusion it caused.")
    w("")
    w("The trailing variables are what is in scope at that line -- the terms of")
    w("a conditional breakpoint.")
    w("")
    w("```")
    grouped: dict[tuple[str, str, str, str], tuple[list[int], list[str]]] = {}
    for uses in index.io_by_file.values():
        for use in uses:
            key = (use.file, use.op, use.unit or "-", use.procedure)
            lines, fields = grouped.setdefault(key, ([], []))
            lines.append(use.line)
            for name in use.fields:
                if name not in fields:
                    fields.append(name)
    for key in sorted(grouped):
        line_numbers, fields = grouped[key]
        unique_lines = sorted(set(line_numbers))
        shown = ",".join(str(n) for n in unique_lines[:MAX_IO_LINES])
        if len(unique_lines) > MAX_IO_LINES:
            shown += f",+{len(unique_lines) - MAX_IO_LINES}"
        scope = ",".join(fields[:MAX_IO_FIELDS])
        if len(fields) > MAX_IO_FIELDS:
            scope += f",+{len(fields) - MAX_IO_FIELDS}"
        name, op, unit, proc = key
        unit_label = f"unit={unit}" if unit != "-" else "-"
        w("|".join([name, op, unit_label, proc, shown, scope or "-"]))
    w("```")
    w("")

    w("## Assignments (who writes what)")
    w("")
    w("Reverse index: variable -> the routines that assign it, with lines.")
    w("Keys are full field paths with array subscripts stripped, so")
    w("`aqu_d(iaq)%rchrg = ...` is listed under `aqu_d%rchrg`. Grep `rchrg|` to")
    w("find one field wherever it lives, or `^aqu_d%` for every field of a")
    w("structure.")
    w("")
    w("`variable | routine:line[,routine:line...]`")
    w("")
    w("```")
    for symbol in sorted(index.writers):
        sites = index.writers_of(symbol)
        shown = sites[:MAX_WRITE_SITES]
        if len(sites) > MAX_WRITE_SITES:
            shown = shown + [f"...+{len(sites) - MAX_WRITE_SITES} more"]
        w(f"{symbol}|{','.join(shown)}")
    w("```")
    w("")

    w("## Loops")
    w("")
    w("Loop headers with their index variable and line -- what is in scope at a")
    w("given point, for setting a conditional breakpoint.")
    w("")
    w("`routine | line | loop header`")
    w("")
    w("```")
    for proc in procs:
        for loop in index.loops_in(proc.name):
            w(f"{loop.procedure}|{loop.line}|{loop.header}")
    w("```")

    return "\n".join(out) + "\n"
