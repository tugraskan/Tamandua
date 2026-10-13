"""Save and load a built index as a single JSON file.

Building an index needs a SWAT+ checkout
*and* an importable ``swatplus_reference``; the latter is a separate
repository, so without this module nobody outside the team can run the MCP
server at all -- not because the facts are unavailable, but because the parser
that extracts them is.

A snapshot separates the two halves. Someone with the parser builds one; the
snapshot is published as a release asset; everyone else loads it and serves the
same facts with no parser and no Fortran source present.

The round trip is lossless for everything a query reads. It deliberately is not
a pickle: the file is plain JSON, so it can be diffed, inspected, and read by
something that is not this program.

    from tamandua.index import build_source_index, save_snapshot
    save_snapshot(build_source_index(), Path("swatplus-facts.json"))

    from tamandua.index import load_snapshot
    index = load_snapshot(Path("swatplus-facts.json"))
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tamandua.index.build import (
    INDEX_FORMAT_VERSION,
    Comparison,
    Copy,
    DerivedType,
    Field,
    IOUse,
    IndexError_,
    Loop,
    ModuleVariable,
    Procedure,
    Provenance,
    Read,
    ScannerWarning,
    SelectCase,
    SourceIndex,
    Use,
    VariableDeclaration,
    WriterStatement,
    stored_fingerprint,
    stored_parser_commit,
)
from tamandua.index.install import RHS_NAME

#: Bumped when the *snapshot file's* own layout changes. Distinct from
#: ``INDEX_FORMAT_VERSION``, which describes the facts inside it: a snapshot can
#: gain a section without the extracted fields changing shape, and vice versa.
SNAPSHOT_FORMAT = "6"
#: Format 3 adds ``module_variables``. Format 4 adds ``declaration`` to
#: derived-type fields. Format 5 adds ``comparisons`` and ``copies``, the
#: equality tests the input-file links are derived from and the assignments
#: that carry a column's value to them. Format 6 adds each procedure's
#: ``kind`` -- a main program is now one -- and ``called_at``, the call sites
#: behind ``called_by``. Older files stay readable, and each
#: added section or key loads empty/``None`` from them -- so the guard against
#: quietly serving a snapshot that predates one is the assertion that the
#: *bundled* pair is current, not this allow-list. A format-1 bundle once loaded silently while
#: answering every `breakpoint` query with zero loops; see
#: ``tests/test_snapshot.py``.
READABLE_SNAPSHOT_FORMATS = {"1", "2", "3", "4", "5", SNAPSHOT_FORMAT}
#: Format 2 keeps the statement for every assignment, plain variables and
#: arrays included, and adds ``reads`` -- where each variable's value is used,
#: and how -- with ``statements``, the text at each read site. Both are about
#: right-hand sides and both are large, so they live here rather than in the
#: base facts every consumer loads. Format 1 still loads, with no reads.
RHS_FORMAT = "2"
READABLE_RHS_FORMATS = {"1", RHS_FORMAT}


def _io_to_json(use: IOUse) -> dict[str, Any]:
    return {
        "file": use.file, "op": use.op, "unit": use.unit,
        "procedure": use.procedure, "line": use.line,
        "fields": list(use.fields),
    }


def _io_from_json(raw: dict[str, Any]) -> IOUse:
    return IOUse(
        file=raw["file"], op=raw["op"], unit=raw["unit"],
        procedure=raw["procedure"], line=raw["line"],
        fields=tuple(raw.get("fields", ())),
    )


def save_snapshot(index: SourceIndex, path: Path) -> Path:
    """Write ``index`` to ``path`` as JSON. Returns the path written.

    ``io_by_unit`` is not stored: every one of its entries is already in
    ``io_by_file`` and is rebuilt on load by re-keying. Storing both would
    double the largest section of the file to hold nothing new, and would let
    the two disagree.
    """
    payload = {
        "snapshot_format": SNAPSHOT_FORMAT,
        "index_format": INDEX_FORMAT_VERSION,
        "provenance": {
            "source_path": index.provenance.source_path,
            "source_commit": index.provenance.source_commit,
            "source_describe": index.provenance.source_describe,
            "source_fingerprint": index.provenance.source_fingerprint,
            "generated_at": index.provenance.generated_at,
            "format_version": index.provenance.format_version,
            "parser_commit": index.provenance.parser_commit,
            "compile_status": index.provenance.compile_status,
        },
        "procedures": [
            {
                "name": p.name, "module": p.module, "location": p.location,
                "path": p.path, "called_by": list(p.called_by),
                "callees": list(p.callees),
                "uses": [
                    {"module": use.module, "only": list(use.only),
                     "line": use.line, "intrinsic": use.intrinsic}
                    for use in p.uses
                ],
                "arguments": [
                    {"name": item.name, "declaration": item.declaration,
                     "line": item.line, "vartype": item.vartype,
                     "initial": item.initial, "units": item.units,
                     "description": item.description}
                    for item in p.arguments
                ],
                "locals": [
                    {"name": item.name, "declaration": item.declaration,
                     "line": item.line, "vartype": item.vartype,
                     "initial": item.initial, "units": item.units,
                     "description": item.description}
                    for item in p.locals
                ],
                "select_cases": [
                    {"subject": select.subject, "cases": list(select.cases),
                     "line": select.line}
                    for select in p.select_cases
                ],
                "kind": p.kind,
                "called_at": list(p.called_at),
            }
            for p in index.procedures.values()
        ],
        "io": [_io_to_json(u) for uses in index.io_by_file.values() for u in uses],
        "writers": {k: list(v) for k, v in index.writers.items()},
        "loops": {
            name: [{"procedure": l.procedure, "line": l.line, "header": l.header,
                    "end_line": l.end_line, "index": l.index}
                   for l in items]
            for name, items in index.loops.items()
        },
        "unresolved_loop_files": sorted(index.unresolved_loop_files),
        "comparisons": {
            name: [{"procedure": c.procedure, "line": c.line, "left": c.left,
                    "op": c.op, "right": c.right,
                    "loops": None if c.loops is None else list(c.loops),
                    "via": c.via}
                   for c in items]
            for name, items in index.comparisons.items()
        },
        "copies": {
            path: [{"procedure": c.procedure, "line": c.line, "target": c.target,
                    "op": c.op, "source": c.source}
                   for c in items]
            for path, items in index.copies.items()
        },
        "types": [
            {
                "name": t.name, "module": t.module, "location": t.location,
                "fields": [
                    {"type_name": f.type_name, "name": f.name, "vartype": f.vartype,
                     "units": f.units, "description": f.description,
                     "location": f.location, "declaration": f.declaration}
                    for f in t.fields
                ],
            }
            for t in index.types.values()
        ],
        "call_paths": {k: [list(p) for p in v] for k, v in index.call_paths.items()},
        "module_variables": [
            {"name": item.name, "module": item.module, "vartype": item.vartype,
             "declaration": item.declaration, "line": item.line,
             "units": item.units, "description": item.description,
             "initial": item.initial, "is_parameter": item.is_parameter}
            for item in sorted(index.module_variables.values(),
                               key=lambda item: (item.module.lower(), item.line))
        ],
        "scanner_warnings": [
            {
                "code": warning.code,
                "message": warning.message,
                "file": warning.file,
                "line": warning.line,
                "procedure": warning.procedure,
            }
            for warning in index.scanner_warnings
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so a rebuild of an unchanged tree produces an identical file and
    # a release diff shows only real changes; "\n" so it is identical whichever
    # system wrote it.
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def rhs_path_for(facts_path: Path) -> Path:
    """The conventionally named optional RHS sidecar beside a base snapshot."""

    return facts_path.parent / RHS_NAME


def rhs_matches_snapshot(rhs_path: Path, facts_path: Path) -> bool:
    """True when a sidecar is the matching companion of ``facts_path``."""

    try:
        payload = json.loads(rhs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        str(payload.get("rhs_format", "missing")) == RHS_FORMAT
        and payload.get("source_fingerprint") == stored_fingerprint(facts_path)
        and payload.get("parser_commit") == stored_parser_commit(facts_path)
    )


def save_rhs(index: SourceIndex, path: Path) -> Path:
    """Write right-hand sides separately from the compact base facts.

    Three sections: ``assignments``, each variable's assignment statements;
    ``reads``, each variable's read sites as ``procedure:line:kind[:detail]``
    (:meth:`Read.encode`, one short string per read rather than an object,
    which is a third of the size); and ``statements``, the text at each read
    site, stored once however many variables it reads.
    """

    payload = {
        "rhs_format": RHS_FORMAT,
        "source_fingerprint": index.provenance.source_fingerprint,
        "parser_commit": index.provenance.parser_commit,
        "assignments": {
            variable: [
                {"procedure": item.procedure, "line": item.line, "raw": item.raw}
                for item in items
            ]
            for variable, items in index.writer_statements.items()
        },
        "reads": {
            variable: [item.encode() for item in items]
            for variable, items in index.reads.items()
        },
        "statements": dict(index.read_statements),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def load_rhs(index: SourceIndex, path: Path) -> SourceIndex:
    """Attach an RHS sidecar, refusing any pair built from different source."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return index
    except json.JSONDecodeError as exc:
        raise IndexError_(f"{path} is not valid RHS JSON: {exc}")

    found = str(payload.get("rhs_format", "missing"))
    if found not in READABLE_RHS_FORMATS:
        raise IndexError_(
            f"{path} is RHS format {found}, this build reads {RHS_FORMAT}; "
            "rebuild it together with the base facts"
        )
    sidecar_fingerprint = payload.get("source_fingerprint")
    if sidecar_fingerprint != index.provenance.source_fingerprint:
        raise IndexError_(
            f"RHS sidecar {path} does not match the base facts source fingerprint; "
            "rebuild or remove the sidecar"
        )
    sidecar_parser = payload.get("parser_commit")
    if (sidecar_parser and index.provenance.parser_commit
            and sidecar_parser != index.provenance.parser_commit):
        raise IndexError_(
            f"RHS sidecar {path} was built with a different parser revision; "
            "rebuild or remove the sidecar"
        )

    try:
        index.writer_statements = defaultdict(list, {
            variable: [WriterStatement(**item) for item in items]
            for variable, items in payload.get("assignments", {}).items()
        })
        # Absent before RHS format 2: no reads, and the index says so rather
        # than answering "read nowhere".
        if "reads" in payload:
            index.reads = defaultdict(list, {
                variable: [Read.decode(item) for item in items]
                for variable, items in payload["reads"].items()
            })
            index.read_statements = dict(payload.get("statements", {}))
            index.reads_indexed = True
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise IndexError_(f"RHS sidecar {path} is malformed: {exc}")
    return index


def load_snapshot(path: Path) -> SourceIndex:
    """Rebuild a ``SourceIndex`` from a snapshot file.

    Needs neither the SWAT+ source nor ``swatplus_reference``. Loop ends are
    stored facts, so ``scope_at`` and ``breakpoint`` are fully snapshot-backed
    too; no path from the build machine is opened while answering a query.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise IndexError_(f"no snapshot at {path}")
    except json.JSONDecodeError as exc:
        raise IndexError_(f"{path} is not valid JSON: {exc}")

    found = str(payload.get("snapshot_format", "missing"))
    if found not in READABLE_SNAPSHOT_FORMATS:
        raise IndexError_(
            f"{path} is snapshot format {found}, this build reads "
            f"{SNAPSHOT_FORMAT}; rebuild it with `swatplus-build`")

    provenance = dict(payload["provenance"])
    provenance.setdefault("compile_status", "not_checked")
    # Reads live in the RHS sidecar; until one carrying them attaches, the
    # index has none to answer from.
    index = SourceIndex(provenance=Provenance(**provenance), reads_indexed=False)

    for raw in payload["procedures"]:
        values = dict(raw)
        values["uses"] = [
            Use(module=item["module"], only=tuple(item.get("only", ())),
                line=item["line"], intrinsic=bool(item.get("intrinsic", False)))
            for item in raw.get("uses", [])
        ]
        values["arguments"] = [
            VariableDeclaration(**item) for item in raw.get("arguments", [])
        ]
        values["locals"] = [
            VariableDeclaration(**item) for item in raw.get("locals", [])
        ]
        values["select_cases"] = [
            SelectCase(subject=item["subject"], cases=tuple(item.get("cases", ())),
                       line=item["line"])
            for item in raw.get("select_cases", [])
        ]
        index.procedures[raw["name"].lower()] = Procedure(**values)

    index.io_by_file = defaultdict(list)
    index.io_by_unit = defaultdict(list)
    for raw in payload["io"]:
        use = _io_from_json(raw)
        index.io_by_file[use.file.lower()].append(use)
        if use.unit:
            index.io_by_unit[use.unit].append(use)

    index.writers = defaultdict(list, {k: list(v) for k, v in payload["writers"].items()})
    index.loops = defaultdict(list, {
        name: [Loop(**item) for item in items]
        for name, items in payload["loops"].items()
    })
    index.unresolved_loop_files = set(payload.get("unresolved_loop_files", ()))
    # Absent before format 5, so read with a default rather than by key.
    index.comparisons = defaultdict(list, {
        name: [Comparison(**{**item, "loops": None if item["loops"] is None
                             else tuple(item["loops"])})
               for item in items]
        for name, items in payload.get("comparisons", {}).items()
    })
    index.copies = defaultdict(list, {
        path: [Copy(**item) for item in items]
        for path, items in payload.get("copies", {}).items()
    })
    index.call_paths = defaultdict(list, {
        k: [list(p) for p in v] for k, v in payload.get("call_paths", {}).items()
    })
    index.scanner_warnings = [
        ScannerWarning(**raw) for raw in payload.get("scanner_warnings", [])
    ]
    for raw in payload["types"]:
        index.types[raw["name"].lower()] = DerivedType(
            name=raw["name"], module=raw["module"], location=raw["location"],
            fields=[Field(**f) for f in raw["fields"]],
        )
    # Absent from formats 1 and 2, so read with a default rather than by key.
    for raw in payload.get("module_variables", []):
        item = ModuleVariable(**raw)
        index.module_variables[(item.module.lower(), item.name.lower())] = item

    sidecar = rhs_path_for(path)
    if sidecar.is_file():
        load_rhs(index, sidecar)
    return index
