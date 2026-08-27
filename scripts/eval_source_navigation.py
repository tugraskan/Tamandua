#!/usr/bin/env python3
"""Score the frozen source-navigation questions against the index.

Answers the mechanical half of the evaluation: for each frozen question, does
the index *contain* the right answer, and what does retrieving it cost? Both
are deterministic, so this runs in CI and needs no model.

It deliberately does NOT answer the adoption question -- whether an assistant
reliably reaches for the index when it is available. That needs real assistant
sessions and a scorer who is not the assistant being scored; docs/status.md
records what those sessions showed.

    python scripts/eval_source_navigation.py [--json report.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tamandua.index import IndexError_, build_source_index  # noqa: E402

CASES = Path(__file__).resolve().parent.parent / "evaluation" / "source_navigation.jsonl"


def answer(index, probe: dict) -> tuple[list[str], int]:
    """Resolve one probe against the index. Returns (answer rows, bytes)."""
    kind = probe["kind"]
    if kind == "file_io":
        uses = index.io_for_file(probe["file"])
        if op := probe.get("op"):
            uses = [u for u in uses if u.op == op]
        rows = [f"{u.procedure}:{u.line}" for u in uses]
    elif kind == "unit_users":
        rows = [f"{u.procedure}:{u.line}"
                for u in index.io_for_unit(probe["unit"], probe.get("op", ""))]
    elif kind == "callers":
        rows = index.callers_of(probe["procedure"])
    elif kind == "callees":
        rows = index.callees_of(probe["procedure"])
    elif kind == "find_procedure":
        proc = index.procedure(probe["procedure"])
        rows = [proc.location] if proc else []
    elif kind == "writers":
        rows = index.writers_of(probe["variable"])
    elif kind == "loops":
        rows = [f"{loop.line}:{loop.header}" for loop in index.loops_in(probe["procedure"])]
    else:
        raise ValueError(f"unknown probe kind: {kind}")
    return rows, len("\n".join(rows).encode())


def score(rows: list[str], expect: dict) -> bool:
    """A case passes when every expected token appears somewhere in the rows."""
    blob = "\n".join(rows).lower()
    wanted = (expect.get("procedures", []) + expect.get("locations", [])
              + expect.get("substrings", []))
    return bool(wanted) and all(w.lower() in blob for w in wanted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None, help="write a report")
    args = parser.parse_args(argv)

    try:
        index = build_source_index(args.source, args.corpus)
    except IndexError_ as exc:
        parser.exit(2, f"error: {exc}\n")

    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    results, by_phrasing = [], {}

    print(f"{'id':<8} {'phrasing':<11} {'bytes':>7}  result")
    print("-" * 60)
    for case in cases:
        rows, size = answer(index, case["probe"])
        ok = score(rows, case["expect"])
        results.append({"id": case["id"], "phrasing": case["phrasing"],
                        "passed": ok, "bytes": size, "rows": len(rows)})
        tally = by_phrasing.setdefault(case["phrasing"], [0, 0])
        tally[0] += int(ok)
        tally[1] += 1
        print(f"{case['id']:<8} {case['phrasing']:<11} {size:>6,}B  {'pass' if ok else 'FAIL'}")

    passed = sum(r["passed"] for r in results)
    print("-" * 60)
    print(f"{passed}/{len(results)} passed, {sum(r['bytes'] for r in results):,} bytes total")
    for phrasing, (hit, total) in sorted(by_phrasing.items()):
        print(f"  {phrasing:<11} {hit}/{total}")

    if args.json:
        args.json.write_text(json.dumps({
            "provenance": {
                "source_commit": index.provenance.source_commit,
                "generated_at": index.provenance.generated_at,
            },
            "passed": passed,
            "total": len(results),
            "results": results,
        }, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
