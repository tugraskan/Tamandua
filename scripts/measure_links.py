#!/usr/bin/env python3
"""Measure derived input-file links against the dataselector's static schema.

Tamandua derives a link where the Fortran shows a search -- a column's value
compared with ``==`` against rows of another file's array (see
``tamandua.index.layouts.input_links``). The dataselector's
``swatplus-editor-schema.json`` carries ``foreign_keys`` extracted once from
swatplus-editor's database models. This compares the two, file pair by file
pair, then column by column where both have the pair.

    python scripts/measure_links.py \\
        --schema <swatplus-dataselector>/resources/schema/swatplus-editor-schema.json \\
        [--facts swatplus-facts.json] [--dataset refdata/Ames_sub1] \\
        [--source <swatplus>/src] [--json out.json]

The schema is a comparison baseline only: nothing in it reaches Tamandua.

File names are compared the way the dataselector itself matches files to
tables -- lowercased, ``-`` read as ``_`` -- and nothing more. A second view
then reads two of the editor's names as the files SWAT+ opens, each for a
stated reason (``ALIASES``); both views are printed.

``--dataset`` maps each column to the name the file's own header gives it at
the same position (``land_use_mgt`` is headed ``lu_mgt``), which is how the
editor names columns. ``--source`` re-checks, on the Fortran itself, the one
blind spot of following copies: an assignment inside a one-line ``if``, which
the parser does not record as an assignment.

See docs/links_experiment.md.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from tamandua.index.build import field_path
from tamandua.index.layouts import _raw_layouts, _Reads, file_layouts, input_links
from tamandua.index.layouts_cli import _load

#: Editor file names read as the files SWAT+ opens, in the second view only.
ALIASES = {
    # The editor keeps every decision table in one model
    # (project.decision_table.D_table_dtl); SWAT+ opens four files for them.
    "d_table.dtl": ["lum.dtl", "res_rel.dtl", "scen_lu.dtl", "flo_con.dtl"],
    # Ames, which the editor wrote, holds cons_practice.lum and no
    # cons-prac.lum; SWAT+ opens cons_practice.lum.
    "cons_prac.lum": ["cons_practice.lum"],
}


def canonical(name: str) -> str:
    return name.lower().replace("-", "_")


def editor_links(schema: dict, aliased: bool) -> dict[tuple[str, str], list[str]]:
    """(source file, target file) -> the editor's foreign-key columns."""
    tables = schema["tables"]
    file_of = {table["table_name"]: table["file_name"] for table in tables.values()}
    pairs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for name, table in tables.items():
        for key in table.get("foreign_keys") or []:
            referenced = key["references"]["table"]
            target = file_of.get(referenced)
            if target is None:
                if not aliased:
                    target = f"?{referenced}"
                else:
                    # An unresolved reference names a table the schema lacks;
                    # the editor names tables file-name-with-underscores.
                    stem, _, extension = referenced.rpartition("_")
                    target = f"{stem}.{extension}"
            sources = [canonical(table["file_name"])]
            targets = [canonical(target)]
            if aliased:
                sources = [canonical(item) for s in sources for item in ALIASES.get(s, [s])]
                targets = [canonical(item) for t in targets for item in ALIASES.get(t, [t])]
            for source in sources:
                for target_file in targets:
                    columns = pairs[(source, target_file)]
                    if key["column"] not in columns:
                        columns.append(key["column"])
    return dict(pairs)


def header_names(dataset: Path | None, layouts: dict) -> dict[str, list[str]]:
    """Each dataset file's header line, by lowercased file name."""
    if dataset is None:
        return {}
    names: dict[str, list[str]] = {}
    for key, layout in layouts.items():
        path = next((p for p in dataset.iterdir() if p.name.lower() == key), None)
        if path is None or not layout.preamble:
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        at = len(layout.preamble) - 1
        if at < len(lines):
            names[key] = lines[at].split()
    return names


def relied_on(index, links) -> set[str]:
    """Every path a link through copies relies on: its operands, and each
    path a copy rewrites them to. A write to any of these, or to a structure
    holding one, could add a value the link does not account for."""
    leaves: set[str] = set()
    for link in links:
        if not link.through:
            continue
        for side in link.compares.split(" == "):
            path = field_path(side)
            if path:
                leaves.add(path)
    for _ in range(4):
        grown = set()
        for leaf in leaves:
            parts = leaf.split("%")
            for count in range(1, len(parts) + 1):
                prefix = "%".join(parts[:count])
                for copy in index.copies_to(prefix):
                    if copy.source_path:
                        grown.add(copy.source_path + leaf[len(prefix):])
        if grown <= leaves:
            break
        leaves |= grown
    return leaves


def one_line_if_writers(source: Path, paths: set[str]) -> list[str]:
    """One-line ``if`` assignments to a path a link relies on, or to a
    structure holding one."""
    found: list[str] = []
    for path in sorted(source.rglob("*.f90")):
        for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            match = re.match(r"^\s*(?:else\s*)?if\s*\(.*\)\s*([A-Za-z_][\w%()\s,:+\-*]*?)\s*=(?![=>])",
                             line.split("!")[0], re.IGNORECASE)
            if not match:
                continue
            target = re.sub(r"\([^()]*\)", "", match.group(1))
            target = re.sub(r"\([^()]*\)", "", target).replace(" ", "").lower()
            if any(target == p or p.startswith(target + "%") for p in paths):
                found.append(f"{path.name}:{number}: {line.strip()}")
    return found


#: The simple grep the work started from: `if (<path> == <array>(<i>)%name)`,
#: either way round, `==` or `.eq.`.
_GREP = [
    re.compile(r"\bif\s*\(\s*[\w%()]+\s*(?:==|\.eq\.)\s*\w+\s*\(\s*\w+\s*\)\s*%\s*name\s*\)",
               re.IGNORECASE),
    re.compile(r"\bif\s*\(\s*\w+\s*\(\s*\w+\s*\)\s*%\s*name\s*(?:==|\.eq\.)\s*[\w%()]+\s*\)",
               re.IGNORECASE),
]


def grep_baseline(source: Path, index, links, unlinked) -> dict:
    """What happened to each site the simple grep finds."""
    sites = []
    for path in sorted(source.rglob("*.f90")):
        for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            code = line.split("!")[0]
            if any(pattern.search(code) for pattern in _GREP):
                sites.append(f"{path.name}:{number}")
    linked = {link.at for link in links}
    why = {item.at: item.reason for item in unlinked}
    stored = set()
    for name, items in index.comparisons.items():
        proc = index.procedure(name)
        for item in items:
            if proc is not None and item.via is None:
                stored.add(f"{Path(proc.path).name}:{item.line}")
    outcome: dict[str, list[str]] = defaultdict(list)
    for site in sites:
        if site in linked:
            outcome["linked"].append(site)
        elif site in why:
            outcome[why[site]].append(site)
        elif site in stored:
            outcome["stored, no outcome"].append(site)
        else:
            outcome["not stored"].append(site)
    return {"sites": len(sites), "files": len({s.rpartition(":")[0] for s in sites}),
            "outcome": {k: v for k, v in sorted(outcome.items(), key=lambda kv: -len(kv[1]))}}


def measure(schema_path: Path, facts: Path | None, dataset: Path | None,
            source: Path | None) -> dict:
    index = _load(facts)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    layouts = file_layouts(index)
    links, unlinked = input_links(index)
    headers = header_names(dataset, layouts)

    ours: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for link in links:
        layout = layouts.get(link.source_file)
        header = None
        if layout is not None and layout.main is not None and link.source_file in headers:
            for position, column in enumerate(layout.main.columns):
                if column.path == link.source_path and not any(
                        c.repeat for c in layout.main.columns[:position + 1]):
                    names = headers[link.source_file]
                    header = names[position] if position < len(names) else None
                    break
        entry = {"path": link.source_path,
                 "column": link.source_path.rsplit("%", 1)[-1],
                 "header": header, "target_path": link.target_path,
                 "at": link.at, "through": link.through}
        key = (canonical(link.source_file), canonical(link.target_file))
        if not any(item["path"] == entry["path"] and item["target_path"] == entry["target_path"]
                   for item in ours[key]):
            ours[key].append(entry)

    read_files = {canonical(key) for key in layouts}
    editor_files = {canonical(table["file_name"]) for table in schema["tables"].values()}

    views = {}
    for view, aliased in (("strict", False), ("aliased", True)):
        theirs = editor_links(schema, aliased)
        agree = sorted(set(ours) & set(theirs))
        only_ours = sorted(set(ours) - set(theirs))
        only_theirs = sorted(set(theirs) - set(ours))
        # "name": every editor column is the Fortran name or the header name
        # of a column Tamandua links. "differ": the dataset gives every
        # linked column a header name and none matches. "unmapped": no header
        # to map by, so the names alone cannot say -- reviewed by hand in
        # docs/links_experiment.md.
        columns = {"name": 0, "differ": 0, "unmapped": 0}
        column_rows = []
        for pair in agree:
            names = {n.lower() for item in ours[pair]
                     for n in (item["column"], item["header"]) if n}
            editor = [c.lower() for c in theirs[pair]]
            if all(c in names for c in editor):
                verdict = "name"
            elif all(item["header"] for item in ours[pair]):
                verdict = "differ"
            else:
                verdict = "unmapped"
            columns[verdict] += 1
            column_rows.append({"pair": list(pair), "editor": theirs[pair],
                                "tamandua": [(i["column"], i["header"]) for i in ours[pair]],
                                "verdict": verdict})

        def why_editor_only(pair: tuple[str, str]) -> str:
            missing = [f for f in pair if f not in read_files]
            if missing:
                return "SWAT+ reads no file named " + " or ".join(missing)
            return "SWAT+ reads both; no comparison shown joins them"

        def why_ours_only(pair: tuple[str, str]) -> str:
            missing = [f for f in pair if f not in editor_files]
            if missing:
                return "the editor has no table named " + " or ".join(missing)
            return "the editor has both tables and no foreign key between them"

        views[view] = {
            "tamandua_pairs": len(ours), "editor_pairs": len(theirs),
            "agree": len(agree), "columns": columns, "column_rows": column_rows,
            "only_tamandua": [{"pair": list(p), "why": why_ours_only(p),
                               "links": ours[p]} for p in only_ours],
            "only_editor": [{"pair": list(p), "why": why_editor_only(p),
                             "columns": theirs[p]} for p in only_theirs],
        }

    # Every distinct target should be the column that names its rows. The
    # rule does not assume that; this checks it held.
    reads = _Reads(index, _raw_layouts(index))
    unusual = []
    for key in sorted({(link.target_file, link.target_path) for link in links}):
        read = next(r for r in reads.by_path[key[1]] if r.file == key[0])
        text = [c.path for c in read.record.columns
                if (c.vartype or "").lower().startswith("character")]
        if not text or text[0] != key[1]:
            unusual.append({"file": key[0], "path": key[1], "position": read.position})

    copy_paths = relied_on(index, links)
    reasons: dict[str, int] = defaultdict(int)
    for item in unlinked:
        reason = item.reason
        if "is not a column" in reason:
            reason = "<path> is not a column any input file is read into"
        elif reason.startswith("the loop over") and "reads" in reason:
            reason = "the loop over <i> reads <array>'s rows"
        elif reason.startswith("the loop over"):
            reason = "the loop over <i> runs over part of a row"
        elif "is assigned from" in reason:
            reason = "assigned from more than one column"
        elif "also assigned other than by a copy" in reason:
            reason = "also assigned other than by a copy"
        reasons[reason] += 1
    return {
        "comparisons": sum(len(items) for items in index.comparisons.values()),
        "called": sum(1 for items in index.comparisons.values() for c in items if c.via),
        "copies": sum(len(items) for items in index.copies.values()),
        "links": len({(l.source_file, l.source_path, l.target_file, l.target_path)
                      for l in links}),
        "link_sites": len({l.at for l in links}),
        "through_copies": len({(l.source_file, l.source_path, l.target_file, l.target_path)
                               for l in links if any(not s.startswith("search") for s in l.through)
                               and l.through}),
        "through_calls": len({(l.source_file, l.source_path, l.target_file, l.target_path)
                              for l in links if l.at and any(
                                  s.split(":")[0] == "search.f90" for s in l.through)}),
        "files_with_references": sum(
            1 for layout in layouts.values()
            if any(c.references for r in layout.records for c in r.columns)),
        "unlinked": len(unlinked),
        "unlinked_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "targets_not_first_text_column": unusual,
        "copy_paths": sorted(copy_paths),
        "one_line_if_writers": one_line_if_writers(source, copy_paths) if source else None,
        "grep_baseline": grep_baseline(source, index, links, unlinked) if source else None,
        "views": views,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--facts", type=Path, default=None)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    result = measure(args.schema, args.facts, args.dataset, args.source)

    print(f"comparisons stored   {result['comparisons']}  "
          f"({result['called']} restated at a call)")
    print(f"copies stored        {result['copies']}")
    print(f"links                {result['links']}  from {result['link_sites']} comparison sites")
    print(f"  through a copy     {result['through_copies']}")
    print(f"  through search()   {result['through_calls']}")
    print(f"files with a reference  {result['files_with_references']}")
    print(f"comparisons not linked  {result['unlinked']}")
    for reason, count in result["unlinked_reasons"].items():
        print(f"  {count:4d}  {reason}")
    print("targets that are not their record's first text column:",
          result["targets_not_first_text_column"])
    if result["one_line_if_writers"] is not None:
        print(f"one-line if writers of copied paths: {len(result['one_line_if_writers'])}")
        for line in result["one_line_if_writers"]:
            print("  ", line)
    if result["grep_baseline"] is not None:
        grep = result["grep_baseline"]
        print(f"grep baseline: {grep['sites']} sites in {grep['files']} files")
        for outcome, sites in grep["outcome"].items():
            print(f"  {len(sites):4d}  {outcome}" + ("" if outcome == "linked" else f": {', '.join(sites)}"))
    for view, data in result["views"].items():
        print(f"\n[{view}] file pairs: tamandua {data['tamandua_pairs']}, "
              f"editor {data['editor_pairs']}, agree {data['agree']}")
        print(f"  column agreement on shared pairs: {data['columns']}")
        for row in data["column_rows"]:
            if row["verdict"] != "name":
                print(f"    {row['verdict']:6s} {' -> '.join(row['pair'])}: editor "
                      f"{row['editor']} vs {row['tamandua']}")
        by_why: dict[str, list] = defaultdict(list)
        for item in data["only_editor"]:
            by_why[item["why"]].append(item)
        print(f"  only in the editor: {len(data['only_editor'])}")
        for why, items in sorted(by_why.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(items):3d}  {why}")
            if why.startswith("SWAT+ reads both"):
                for item in items:
                    print(f"         {' -> '.join(item['pair'])} {item['columns']}")
        by_why = defaultdict(list)
        for item in data["only_tamandua"]:
            by_why[item["why"]].append(item)
        print(f"  only in Tamandua: {len(data['only_tamandua'])}")
        for why, items in sorted(by_why.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(items):3d}  {why}")
    if args.json:
        args.json.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
