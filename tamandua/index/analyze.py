"""Fill in the call graph the scanner leaves empty.

``swatplus_reference``'s schema scanner records every ``call`` site it sees,
but stops there: ``CallRef.resolved``, ``ProcedureDoc.called_by`` and
``ProcedureDoc.call_paths`` are declared on the model and never populated --
the corpus's own documentation pipeline has no use for a call graph. Tamandua's
``callers``, ``callees`` and ``call_path`` tools do, so it derives them here.

All three are graph work over facts the scanner already provides. Nothing in
this module reads source, and nothing here invents an edge the scanner did not
see.

Resolution is not a formality. The scanner cannot tell an array reference from
a function call, so a line like ``write (2520,*) aqu_d(iaq)%rchrg`` yields a
``CallRef`` named ``aqu_d``. Marking a call resolved only when it names a
procedure that actually exists is what keeps those out of ``callees``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

#: Distinct entry-point paths kept per procedure. A full enumeration is
#: exponential on a tree this size, and the question a path answers -- "how is
#: this reached from the top?" -- is served by a few short ones.
MAX_PATHS = 3

#: Depth cap, so a pathological cycle the cycle check somehow misses cannot
#: run away. SWAT+'s real call depth is nowhere near this.
MAX_DEPTH = 40


def analyze_project(project: Any) -> Any:
    """Populate ``resolved``, ``called_by`` and ``call_paths`` in place.

    Takes and returns the scanner's ``ProjectIndex`` so it can sit directly in
    front of ``FortranScanner.scan()``, the way the previous parser's own
    analyze step did.
    """
    _resolve_calls(project)
    _fill_called_by(project)
    _fill_call_paths(project)
    return project


def _resolve_calls(project: Any) -> None:
    """Mark a call resolved when its target is a procedure in this tree."""
    known = {proc.name.lower() for proc in project.procedures}
    for proc in project.procedures:
        for call in proc.calls:
            call.resolved = call.name.lower() in known


def _fill_called_by(project: Any) -> None:
    """Invert the resolved call edges."""
    callers: dict[str, set[str]] = defaultdict(set)
    for proc in project.procedures:
        for call in proc.calls:
            if call.resolved and call.name.lower() != proc.name.lower():
                callers[call.name.lower()].add(proc.name)
    for proc in project.procedures:
        proc.called_by = sorted(callers[proc.name.lower()])


def _fill_call_paths(project: Any) -> None:
    """Record how each procedure is reached from an entry point.

    Breadth-first from every procedure nothing calls, so the first path found
    to a procedure is a shortest one. Capped per procedure, which also bounds
    the walk: a node stops being expanded once it has its quota.
    """
    display = {proc.name.lower(): proc.name for proc in project.procedures}
    children = {
        proc.name.lower(): sorted({
            call.name.lower() for call in proc.calls
            if call.resolved and call.name.lower() != proc.name.lower()
        })
        for proc in project.procedures
    }

    paths: dict[str, list[list[str]]] = defaultdict(list)
    queue: deque[tuple[str, list[str]]] = deque(
        (proc.name.lower(), [proc.name])
        for proc in project.procedures if not proc.called_by
    )

    while queue:
        node, path = queue.popleft()
        if len(path) >= MAX_DEPTH:
            continue
        walked = {name.lower() for name in path}
        for child in children.get(node, ()):
            if child in walked:          # a cycle: stop rather than loop
                continue
            if len(paths[child]) >= MAX_PATHS:
                continue
            extended = path + [display[child]]
            paths[child].append(extended)
            queue.append((child, extended))

    for proc in project.procedures:
        proc.call_paths = paths.get(proc.name.lower(), [])
