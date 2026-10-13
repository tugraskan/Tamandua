#!/usr/bin/env python3
"""Measure format 6: the main program, call sites, and where values are read.

Builds the index of one SWAT+ tree and reports what format 6 adds: the main
program as a procedure and the call paths that now reach it, how many reads
of each kind the tree holds, what the two files cost on disk, and the answers
to the questions that motivated it.

    python scripts/measure_reads.py --source swatplus/src \
        [--corpus swatplus-reference-corpus] [--previous old-facts.json] [--json out.json]

``--previous`` is a facts file built from the same tree before format 6;
the procedures gaining a call path are counted against it. ``recall`` checks
every ``%component`` in executable code against the reads (see
:func:`recall`). See docs/reads_experiment.md.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from tamandua.index import build_source_index, load_snapshot, save_rhs, save_snapshot
from tamandua.index.reads import logical_statements
from tamandua.mcp.server import render_compact, t_call_path, t_callers, t_uses, t_writers

#: The questions format 6 was built to answer, as tool calls.
QUESTIONS = (
    (t_callers, "lsu_read_elements"),
    (t_callers, "recalldb_read"),
    (t_call_path, "lsu_read_elements"),
    (t_uses, "bmp_sed"),
    (t_uses, "bmp_bac"),
    (t_uses, "frsw_gro"),
    (t_uses, "inject_dep"),
    (t_uses, "husc"),
    (t_uses, "aq_hlife"),
    (t_writers, "sedyld"),
)


_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")
_COMPONENT = re.compile(r"%\s*([A-Za-z_]\w*)")
_NOT_EXECUTABLE = re.compile(
    r"^\s*(?:integer|real|double|logical|character|complex|type\s*[(,: ]|class\s*\(|"
    r"end\b|use\b|procedure\b)", re.IGNORECASE)
_TARGET = re.compile(
    r"^\s*(?:\d+\s+)?([A-Za-z_]\w*(?:\s*\([^=]*?\))?(?:\s*%\s*\w+(?:\s*\([^=]*?\))?)*)"
    r"\s*=(?!=)")


def recall(index, source: Path) -> dict:
    """Every ``%component`` in an executable statement, and what accounts for it.

    Accounted for: a read recorded at that line naming the component, the
    target of the line's assignment, or the input list of a ``read``. The rest
    are counted by the statement's first word, with examples, so each can be
    checked by eye -- an exclusion by design, or a read missed.
    """
    named: dict[tuple[str, int], set[str]] = defaultdict(set)
    for path, items in index.reads.items():
        for item in items:
            named[(item.procedure.lower(), item.line)].update(path.split("%"))
    spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for proc in index.procedures.values():
        start, _, end = proc.location.rpartition(":")[2].partition("-")
        spans[proc.path].append((int(start), int(end or start), proc.name.lower()))
    total = accounted = 0
    other: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for relative, procedures in spans.items():
        text = (source / relative).read_text(encoding="utf-8", errors="replace")
        for line, statement in logical_statements(text):
            owner = max((span for span in procedures if span[0] < line <= span[1]),
                        key=lambda span: span[0], default=None)
            if owner is None or _NOT_EXECUTABLE.match(statement):
                continue
            code = _LITERAL.sub("''", statement)
            target = _TARGET.match(code)
            filling = re.match(r"^\s*(?:\d+\s+)?read\b", code, re.IGNORECASE)
            for component in _COMPONENT.findall(code):
                total += 1
                if (component.lower() in named.get((owner[2], line), ())
                        or filling or (target and re.search(
                            rf"%\s*{component}\b", target.group(1), re.IGNORECASE))):
                    accounted += 1
                    continue
                first = code.split("(")[0].split()[0].lower()
                other[first] += 1
                if len(examples[first]) < 3:
                    examples[first].append(f"{relative}:{line} [{component}] {statement[:100]}")
    return {"component_references": total, "accounted_for": accounted,
            "others_by_statement": dict(other.most_common()),
            "examples": dict(examples)}


def _section_bytes(path: Path) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {key: len(json.dumps({key: value}, indent=1, sort_keys=True))
            for key, value in payload.items() if isinstance(value, (dict, list))}


def measure(source: Path, corpus: Path | None, previous: Path | None) -> dict:
    index = build_source_index(source, corpus)
    report: dict = {
        "source_commit": index.provenance.source_commit,
        "procedures": len(index.procedures),
        "kinds": dict(Counter(p.kind for p in index.procedures.values())),
        "programs": sorted(p.location for p in index.procedures.values()
                           if p.kind == "program"),
        "with_call_path": sum(1 for name in index.procedures if index.paths_to(name)),
        "rooted_at_a_program": sum(
            1 for name in index.procedures
            if index.paths_to(name) and index.procedure(
                index.paths_to(name)[0][0]).kind == "program"),
        "reads": sum(len(items) for items in index.reads.values()),
        "paths_read": len(index.reads),
        "read_sites": len(index.read_statements),
        "reads_by_kind": dict(Counter(
            item.kind for items in index.reads.values() for item in items).most_common()),
        "assignment_statements": sum(len(v) for v in index.writer_statements.values()),
        "assigned_paths_with_a_statement": len(index.writer_statements),
    }
    if previous is not None:
        before = load_snapshot(previous)
        report["gained_a_call_path"] = sorted(
            name for name in index.procedures
            if index.paths_to(name) and not before.paths_to(name))
        report["lost_a_call_path"] = sorted(
            name for name in before.procedures
            if before.paths_to(name) and not index.paths_to(name))
    with tempfile.TemporaryDirectory() as scratch:
        facts = save_snapshot(index, Path(scratch) / "swatplus-facts.json")
        rhs = save_rhs(index, Path(scratch) / "swatplus-rhs.json")
        report["bytes"] = {
            "facts": facts.stat().st_size,
            "rhs": rhs.stat().st_size,
            "rhs_gzip": len(gzip.compress(rhs.read_bytes())),
            "rhs_sections": _section_bytes(rhs),
        }
    report["recall"] = recall(index, Path(index.provenance.source_path))
    report["answers"] = {
        f"{tool.__name__[2:]}({argument})": render_compact(tool(index, argument))
        for tool, argument in QUESTIONS
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    report = measure(args.source, args.corpus, args.previous)
    answers = report.pop("answers")
    for key, value in report.items():
        print(f"{key}: {value}")
    for question, answer in answers.items():
        print(f"\n## {question}\n{answer}")
    if args.json:
        args.json.write_text(json.dumps({**report, "answers": answers}, indent=1),
                             encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
