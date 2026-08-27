"""Tests for the call graph Tamandua derives itself.

``swatplus_reference``'s scanner records call sites but never fills
``resolved``, ``called_by`` or ``call_paths``. These cover the derivation,
including the cases that make it more than a loop: a scanner that cannot tell
an array reference from a call, and a call graph with cycles in it.

Stand-in objects rather than the real parser, so the unit suite keeps running
with no corpus checkout present.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tamandua.index import IndexError_, find_corpus
from tamandua.index.analyze import MAX_PATHS, analyze_project


@dataclass
class FakeCall:
    name: str
    resolved: bool = False


@dataclass
class FakeProc:
    name: str
    calls: list[FakeCall] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)
    call_paths: list[list[str]] = field(default_factory=list)


@dataclass
class FakeProject:
    procedures: list[FakeProc] = field(default_factory=list)


def project(**edges: list[str]) -> FakeProject:
    """Build a project from ``name=[callees]``, declaring every named proc."""
    names = set(edges) | {c for cs in edges.values() for c in cs}
    procs = {n: FakeProc(name=n) for n in sorted(names)}
    for caller, callees in edges.items():
        procs[caller].calls = [FakeCall(name=c) for c in callees]
    return FakeProject(procedures=list(procs.values()))


def by_name(proj: FakeProject) -> dict[str, FakeProc]:
    return {p.name: p for p in proj.procedures}


# ---------------------------------------------------------------- resolving

def test_a_call_to_a_known_procedure_resolves():
    procs = by_name(analyze_project(project(command=["hru_control"])))
    assert [c.resolved for c in procs["command"].calls] == [True]


def test_a_call_to_something_that_is_not_a_procedure_stays_unresolved():
    """The scanner cannot tell `aqu_d(iaq)%rchrg` from a function call.

    Left unresolved, that array reference would show up as a callee of
    aquifer_output, which is why callees_of filters on resolved.
    """
    proj = FakeProject(procedures=[
        FakeProc(name="aquifer_output", calls=[FakeCall(name="aqu_d")]),
    ])
    analyze_project(proj)
    assert proj.procedures[0].calls[0].resolved is False


def test_resolution_is_case_insensitive():
    proj = FakeProject(procedures=[
        FakeProc(name="hru_control", calls=[FakeCall(name="AQU_READ")]),
        FakeProc(name="aqu_read"),
    ])
    analyze_project(proj)
    assert proj.procedures[0].calls[0].resolved is True
    assert by_name(proj)["aqu_read"].called_by == ["hru_control"]


# ---------------------------------------------------------------- called_by

def test_called_by_inverts_the_call_edges():
    procs = by_name(analyze_project(project(
        command=["hru_control"], main=["hru_control"], hru_control=[],
    )))
    assert procs["hru_control"].called_by == ["command", "main"]
    assert procs["command"].called_by == []


def test_called_by_is_deduplicated():
    """A routine calling another twice is one caller, not two."""
    proj = FakeProject(procedures=[
        FakeProc(name="command", calls=[FakeCall(name="hru_control"),
                                        FakeCall(name="hru_control")]),
        FakeProc(name="hru_control"),
    ])
    analyze_project(proj)
    assert by_name(proj)["hru_control"].called_by == ["command"]


def test_unresolved_calls_contribute_no_caller():
    proj = FakeProject(procedures=[
        FakeProc(name="aquifer_output", calls=[FakeCall(name="aqu_d")]),
    ])
    analyze_project(proj)
    assert by_name(proj)["aquifer_output"].called_by == []


def test_direct_recursion_is_not_a_caller_of_itself():
    proj = FakeProject(procedures=[
        FakeProc(name="recurse", calls=[FakeCall(name="recurse")]),
    ])
    analyze_project(proj)
    assert by_name(proj)["recurse"].called_by == []


# -------------------------------------------------------------- call_paths

def test_paths_run_from_an_entry_point_down_to_the_procedure():
    procs = by_name(analyze_project(project(
        main=["command"], command=["hru_control"], hru_control=["pest_decay"],
    )))
    assert procs["pest_decay"].call_paths == [
        ["main", "command", "hru_control", "pest_decay"]
    ]
    assert procs["main"].call_paths == []       # an entry point has no path to it


def test_every_path_ends_at_its_own_procedure():
    procs = by_name(analyze_project(project(
        main=["a", "b"], a=["c"], b=["c"], c=[],
    )))
    for name, proc in procs.items():
        assert all(path[-1] == name for path in proc.call_paths)


def test_distinct_routes_are_both_recorded():
    procs = by_name(analyze_project(project(main=["a", "b"], a=["c"], b=["c"])))
    assert sorted(procs["c"].call_paths) == [
        ["main", "a", "c"], ["main", "b", "c"],
    ]


def test_a_cycle_terminates_and_does_not_repeat_a_node():
    """Mutual recursion must not walk forever."""
    procs = by_name(analyze_project(project(
        main=["a"], a=["b"], b=["a"],
    )))
    for proc in procs.values():
        for path in proc.call_paths:
            assert len(path) == len(set(path)), f"{path} revisits a procedure"


def test_paths_are_capped_per_procedure():
    """A wide graph must not produce an unbounded path list."""
    fan = {f"e{i}": ["target"] for i in range(MAX_PATHS + 4)}
    procs = by_name(analyze_project(project(**fan, target=[])))
    assert len(procs["target"].call_paths) == MAX_PATHS


def test_a_graph_that_is_all_cycle_has_no_entry_point_and_no_paths():
    """Nothing to start from is a legitimate shape, not a crash."""
    procs = by_name(analyze_project(project(a=["b"], b=["a"])))
    assert all(p.call_paths == [] for p in procs.values())


def test_empty_project_is_fine():
    assert analyze_project(FakeProject()).procedures == []


# ------------------------------------------------- against the real parser

def test_real_scanner_output_gets_a_usable_call_graph(tmp_path, monkeypatch):
    """End to end: the scanner leaves the graph empty, this fills it.

    The import guard is inside the test, not at module scope: everything above
    is pure graph work needing no corpus, and a module-level skip would take
    those with it.
    """
    try:
        corpus_src = find_corpus()
    except IndexError_:
        pytest.skip("no swatplus-reference-corpus checkout or installation")
    monkeypatch.syspath_prepend(str(corpus_src))
    from swatplus_reference.parser.schema_config import BuildConfig
    from swatplus_reference.parser.schema_fortran import FortranScanner

    (tmp_path / "a.f90").write_text(
        "      subroutine proc_aqu\n"
        "      call aqu_read\n"
        "      end subroutine proc_aqu\n"
        "      subroutine aqu_read\n"
        "      end subroutine aqu_read\n",
        encoding="utf-8",
    )
    scanned = FortranScanner(BuildConfig(source_dir=tmp_path)).scan()
    assert all(not p.called_by for p in scanned.procedures), "scanner filled it?"

    procs = {p.name: p for p in analyze_project(scanned).procedures}
    assert procs["aqu_read"].called_by == ["proc_aqu"]
    assert procs["aqu_read"].call_paths == [["proc_aqu", "aqu_read"]]
    assert [c.resolved for c in procs["proc_aqu"].calls] == [True]
