#!/usr/bin/env python3
"""Measure derived input-file layouts against a real dataset.

For every file in the dataset that the facts show SWAT+ reading, compare the
main record's column count with the file itself: the header line's names and
the first data row's values. A layout that reads *fewer* values than a row
holds is expected -- a list-directed read stops once its variables are
filled, so trailing columns such as ``description`` go unread. One that reads
*more* than the row holds would carry on onto the next line, and is the case
worth looking at.

    python scripts/measure_layouts.py --dataset refdata/Ames_sub1 \
        [--facts swatplus-facts.json] [--schema editor-schema.json] [--json out.json]

``--schema`` also reports how many of the dataset's files a dataselector
schema covers, for comparison. See docs/layouts_experiment.md.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tamandua.index.layouts import file_layouts
from tamandua.index.layouts_cli import _load


def _tokens(path: Path, line: int) -> list[str] | None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[line].split() if line < len(lines) else None


def measure(dataset: Path, facts: Path | None, schema: Path | None) -> dict:
    index = _load(facts)
    layouts = file_layouts(index)
    files = sorted(p for p in dataset.iterdir() if p.is_file())
    rows = []
    for path in files:
        layout = layouts.get(path.name.lower())
        if layout is None:
            continue
        main = layout.main
        row = {"file": path.name, "readers": layout.readers,
               "data_starts_after": len(layout.preamble)}
        if main is None:
            rows.append({**row, "outcome": "no main record"})
            continue
        row.update(reads=len(main.columns), complete=main.complete,
                   repeating=any(c.repeat for c in main.columns),
                   children=sum(r.role == "child" for r in layout.records))
        start = len(layout.preamble)
        header = _tokens(path, start - 1) if start >= 1 else None
        first = _tokens(path, start)
        row["header_names"] = len(header) if header else None
        row["row_values"] = len(first) if first else None
        if header:
            names = [c.name.split("(")[0].lower() for c in main.columns]
            row["same_name_by_position"] = sum(
                a == b.lower() for a, b in zip(names, header))
        if not main.complete:
            outcome = "incomplete"
        elif row["repeating"]:
            outcome = "repeating group"
        elif first is None:
            outcome = "no data row"
        elif len(main.columns) == len(first):
            outcome = "reads every value"
        elif len(main.columns) < len(first):
            outcome = "file has unread trailing values"
        else:
            outcome = "reads past the row"
        rows.append({**row, "outcome": outcome})

    result = {
        "dataset_files": len(files),
        "files_with_layout": len(rows),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "rows": rows,
    }
    if schema is not None:
        tables = {name.lower() for name in json.loads(schema.read_text())["tables"]}
        variants = lambda n: {n, n.replace("-", "_"), n.replace("_", "-")}
        result["schema_covers"] = sum(
            1 for p in files if variants(p.name.lower()) & tables)
        result["layouts_cover"] = sum(
            1 for p in files if p.name.lower() in layouts)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--facts", type=Path, default=None)
    parser.add_argument("--schema", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    result = measure(args.dataset, args.facts, args.schema)
    if args.json:
        args.json.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"dataset files        {result['dataset_files']}")
    print(f"with a layout        {result['files_with_layout']}")
    if "schema_covers" in result:
        print(f"schema covers        {result['schema_covers']}")
    for outcome, count in sorted(result["outcomes"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:3d}  {outcome}")
    for row in result["rows"]:
        if row["outcome"] not in ("reads every value", "file has unread trailing values"):
            print(f"       {row['file']}: {row['outcome']} "
                  f"(reads {row.get('reads')}, row has {row.get('row_values')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
