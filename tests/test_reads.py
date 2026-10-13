"""Tests for where a value is read -- the reverse of ``writers``.

The statement analysis is plain text work with no parser behind it, so these
run with neither a SWAT+ checkout nor the corpus. The fixtures are real SWAT+
statements, each the reason the case it covers exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tamandua.index import (
    Procedure,
    Provenance,
    Read,
    SourceIndex,
    load_snapshot,
    save_rhs,
    save_snapshot,
)
from tamandua.index.reads import Scope, Span, file_reads, logical_statements, statement_reads
from tamandua.mcp.server import render_compact, t_uses

SCOPE = Scope(
    variables=frozenset({
        "sedyld", "hru", "j", "bmpuser_db", "istr", "b", "pestdb", "jpst",
        "temp_wat", "decay_t", "mgt", "aphu", "mode", "lim", "ic", "flo_m3",
        "d_tbl", "idtbl", "aqu_d", "iaq", "time", "titldum", "pst", "i", "eof",
        "x", "k", "n", "line", "a", "in_aqu", "i_exist", "soil", "ly", "sp_ob",
        "ierr", "chg_par", "v", "t", "amin1", "c", "m", "name", "p", "y",
        "msk", "db_mx", "ielem",
    }),
    procedures=frozenset({
        "decay_temp_adjust", "cond_real", "chg_par", "wet_all_initial", "f",
    }),
    # Declared `real :: chg_par` / `real :: amin1`: a bracket after either is
    # a function reference, not a subscript.
    scalars=frozenset({"chg_par", "amin1"}),
)


def reads(statement: str) -> list[tuple[str, str, str | None]]:
    return statement_reads(statement, SCOPE)


# ------------------------------------------------------------ assignments

def test_a_formula_is_an_assignment_read_into_its_target() -> None:
    """smp_bmpfixed.f90:46 -- `bmp_sed` documented in percent, used as a fraction."""
    found = reads("sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)")
    assert ("hru%lumv%bmp_sed", "assignment", "sedyld") in found
    assert ("sedyld", "assignment", "sedyld") in found
    # The subscript selects an element; it is not computed with.
    assert ("j", "index", "hru%lumv%bmp_sed") in found
    assert ("j", "index", "sedyld") in found
    assert not any(kind == "copy" for _, kind, _ in found)


def test_a_whole_right_hand_side_is_a_copy() -> None:
    """structure_set_parms.f90:108 -- the only read `bmp_bac` ever gets."""
    found = reads("hru(j)%lumv%bmp_bac = bmpuser_db(istr)%bmp_bac")
    assert ("bmpuser_db%bmp_bac", "copy", "hru%lumv%bmp_bac") in found
    assert not any(path == "bmpuser_db%bmp_bac" and kind != "copy"
                   for path, kind, _ in found)
    # The target is written, not read; only its subscript is.
    assert ("j", "index", "hru%lumv%bmp_bac") in found
    assert not any(path == "hru%lumv%bmp_bac" for path, _, _ in found)


def test_a_copy_may_be_parenthesised_trimmed_or_a_pointer() -> None:
    assert ("x", "copy", "name") in reads("name = trim(adjustl(x))")
    assert ("x", "copy", "y") in reads("y = (x)")
    assert ("hru", "copy", "p") in reads("p => hru(j)")


def test_anything_more_than_the_value_is_computed_with() -> None:
    assert ("x", "assignment", "y") in reads("y = -x")
    assert ("x", "assignment", "y") in reads("y = real(x)")
    assert ("x", "assignment", "y") in reads("y = x + 1.")


# -------------------------------------------------------------- conditions

def test_if_and_else_if_conditions() -> None:
    assert reads("if (b <= 0.) then") == [("b", "condition", "if")]
    assert ("mode", "condition", "else if") in reads("else if (mode == 2) then")
    assert ("mode", "condition", "else if") in reads("elseif (mode == 2) then")


def test_a_one_line_if_reads_its_condition_and_its_statement() -> None:
    """structure_set_parms.f90:96, which the scanner records no assignment for."""
    found = reads("if (b <= 0.) hru(j)%lumv%grwat_d = 3. / 64. * hru(j)%lumv%grwat_w")
    assert ("b", "condition", "if") in found
    assert ("hru%lumv%grwat_w", "assignment", "hru%lumv%grwat_d") in found
    found = reads("if (db_mx%wet_dat > 0) call wet_all_initial")
    assert found == [("db_mx%wet_dat", "condition", "if")]


def test_a_do_while_condition() -> None:
    """mgt_operatn.f90:71 -- `husc` read twice on one line, recorded once."""
    found = reads("do while (mgt%husc > 0. .and. aphu > mgt%husc)")
    assert found == [("mgt%husc", "condition", "do while"),
                     ("aphu", "condition", "do while")]


def test_select_case_and_case_and_where() -> None:
    assert reads("select case (mode)") == [("mode", "condition", "select case")]
    assert reads("case (lim)") == [("lim", "condition", "case")]
    assert reads('case ("plant")') == []
    found = reads("where (msk > 0.) y = x")
    assert ("msk", "condition", "where") in found
    assert ("x", "copy", "y") in found


def test_counted_loop_bounds_are_read_and_its_index_is_not() -> None:
    found = reads("do ly = 1, soil(j)%nly")
    assert ("soil%nly", "loop_bounds", "ly") in found
    assert ("j", "index", "soil%nly") in found
    assert not any(path == "ly" for path, _, _ in found)
    assert ("n", "loop_bounds", "k") in reads("outer: do k = 1, n")
    assert reads("do, k = 1, n") == [("n", "loop_bounds", "k")]
    assert reads("do 10, while (aphu > 0.)") == [("aphu", "condition", "do while")]


# --------------------------------------------------------------- arguments

def test_call_arguments_name_the_routine() -> None:
    found = reads("call cond_real (ic, flo_m3, d_tbl%cond(ic)%lim_const, idtbl)")
    assert ("d_tbl%cond%lim_const", "call_argument", "cond_real") in found
    assert ("flo_m3", "call_argument", "cond_real") in found
    assert ("ic", "index", "d_tbl%cond%lim_const") in found


def test_a_keyword_names_an_argument_and_is_not_read() -> None:
    found = reads("call f(x=a)")
    assert found == [("a", "call_argument", "f")]


def test_a_function_in_the_tree_takes_its_arguments() -> None:
    """ch_rtpest.f90:124 -- the read is in decay_temp_adjust's hands."""
    found = reads("decay_t = decay_temp_adjust(pestdb(jpst)%aq_hlife, temp_wat)")
    assert ("pestdb%aq_hlife", "function_argument", "decay_temp_adjust") in found
    assert ("temp_wat", "function_argument", "decay_temp_adjust") in found
    assert ("jpst", "index", "pestdb%aq_hlife") in found


def test_an_intrinsic_is_transparent() -> None:
    assert reads("y = exp(-k)") == [("k", "assignment", "y")]


def test_a_declared_scalar_followed_by_a_bracket_is_a_function() -> None:
    """`real :: chg_par` types an external's result; `real :: amin1` an
    intrinsic. Read as arrays, every argument became a subscript."""
    found = reads("pestdb(ielem)%aq_hlife = chg_par(pestdb(ielem)%aq_hlife, t)")
    assert ("pestdb%aq_hlife", "function_argument", "chg_par") in found
    assert ("t", "function_argument", "chg_par") in found
    found = reads("c = amin1(c, m)")
    assert ("c", "assignment", "c") in found and ("m", "assignment", "c") in found
    # Without a bracket the same name is the variable it declares.
    assert ("chg_par", "assignment", "chg_par") in reads("chg_par = Max(chg_par, v)")


# --------------------------------------------------------------------- I/O

def test_an_output_list_is_read() -> None:
    found = reads("write (2520,*) aqu_d(iaq)%rchrg, time%day")
    assert ("aqu_d%rchrg", "output", "unit=2520") in found
    assert ("time%day", "output", "unit=2520") in found
    assert ("iaq", "index", "aqu_d%rchrg") in found
    assert reads("print *, x") == [("x", "output", "unit=*")]


def test_the_read_that_fills_a_value_is_not_a_read_of_it() -> None:
    found = reads("read (107,*,iostat=eof) titldum, pst(i)%name")
    assert found == [("i", "index", "pst%name")]
    found = reads("read (107,*) (x(k), k = 1, n)")
    assert found == [("k", "index", "x"), ("n", "index", None)]


def test_an_io_control_list_is_read_but_its_answers_are_not() -> None:
    assert reads("open (107, file=in_aqu%aqu)") == [("in_aqu%aqu", "io_control", "open")]
    found = reads("inquire (file=in_aqu%aqu, exist=i_exist)")
    assert found == [("in_aqu%aqu", "io_control", "inquire")]
    # An internal read reads the variable it reads from.
    assert reads("read (line,*) a") == [("line", "io_control", "read")]


def test_allocate_bounds_are_read_and_the_object_is_not() -> None:
    found = reads("allocate (hru(0:sp_ob%hru), stat=ierr)")
    assert found == [("sp_ob%hru", "allocate", "hru")]
    assert reads("deallocate (hru)") == []


# -------------------------------------------------------------- exclusions

@pytest.mark.parametrize("statement", [
    "real :: x = 0.",
    "integer :: i",
    "type (hru_db), dimension(:), allocatable :: hru",
    "character(len=16) :: name = ''",
    "use hru_module, only: hru",
    "implicit none",
    "subroutine smp_bmpfixed (j, a)",
    "function chg_par (v, t)",
    "end subroutine smp_bmpfixed",
    "return",
    "1000 format (1x, a, f10.2)",
    "y = undeclared + 1",
])
def test_statements_that_read_nothing(statement: str) -> None:
    assert reads(statement) == []


def test_a_literal_and_a_logical_constant_are_not_variables() -> None:
    assert reads("if (name == 'x' .and. b > 1.e-6 .or. .not. .true.) then") == [
        ("name", "condition", "if"), ("b", "condition", "if")]


# ---------------------------------------------------------- statement text

def test_logical_statements_join_continuations_and_drop_comments() -> None:
    text = (
        "      x = a + &   ! first part\n"
        "      ! a comment inside the statement\n"
        "\n"
        "        & b     ! the rest\n"
        "      y = 'it!s'; z = 1\n"
        "      ! only a comment\n"
    )
    assert logical_statements(text) == [
        (1, "x = a + b"), (5, "y = 'it!s'"), (5, "z = 1"),
    ]


SOURCE = """\
      module m
        real :: shared = 0.
      contains
      subroutine smp_bmpfixed (j)
        integer, intent (in) :: j
        sedyld(j) = sedyld(j) * &
          (1. - hru(j)%lumv%bmp_sed)   ! the formula
        a = b; c = b
      end subroutine smp_bmpfixed
      end module m
"""


def test_a_file_files_each_read_under_the_procedure_that_holds_it() -> None:
    scope = Scope(variables=frozenset({"j", "sedyld", "hru", "a", "b", "c", "shared"}))
    found, statements = file_reads(SOURCE, [Span("smp_bmpfixed", 4, 9, scope)])
    sites = {(path, item.site, item.kind) for path, item in found}
    assert ("hru%lumv%bmp_sed", "smp_bmpfixed:6", "assignment") in sites
    assert ("b", "smp_bmpfixed:8", "copy") in sites
    # The module's own declarations and the header belong to no read.
    assert all(item.line > 4 for _, item in found)
    assert statements["smp_bmpfixed:6"] == "sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)"
    assert statements["smp_bmpfixed:8"] == "a = b; c = b"


def test_a_dimension_statement_makes_a_declared_scalar_an_array() -> None:
    """cs_rctn_aqu.f90 declares `k_rg` scalar, then `dimension k_rg(4,3)`."""
    text = (
        "      subroutine cs_rctn_aqu\n"
        "      real :: k_rg, rate\n"
        "      dimension conc_rg(3), k_rg(4,3)\n"
        "      rate = k_rg(1,2) * 2.\n"
        "      end subroutine cs_rctn_aqu\n"
    )
    scope = Scope(variables=frozenset({"k_rg", "rate", "conc_rg"}),
                  scalars=frozenset({"k_rg", "rate"}))
    found, _ = file_reads(text, [Span("cs_rctn_aqu", 1, 5, scope)])
    assert [(path, item.kind, item.detail) for path, item in found] == [
        ("k_rg", "assignment", "rate")]


def test_a_read_survives_its_compact_encoding() -> None:
    for item in (Read("ch_rtpest", 124, "function_argument", "decay_temp_adjust"),
                 Read("pest_parm_read", 69, "condition", "if"),
                 Read("x", 1, "index")):
        assert Read.decode(item.encode()) == item


# ------------------------------------------------------------ the index

def _index() -> SourceIndex:
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit="97ca231", source_describe=None,
        source_fingerprint="abc", generated_at="2026-10-13T00:00:00Z",
        format_version="6", parser_commit="110c2a2",
    ))
    for name, path in (("smp_bmpfixed", "smp_bmpfixed.f90"),
                       ("structure_set_parms", "structure_set_parms.f90")):
        index.procedures[name] = Procedure(name=name, module=None,
                                           location=f"{path}:1-90", path=path)
    index.reads["hru%lumv%bmp_sed"] = [Read("smp_bmpfixed", 46, "assignment", "sedyld")]
    index.reads["bmpuser_db%bmp_sed"] = [
        Read("structure_set_parms", 103, "copy", "hru%lumv%bmp_sed")]
    index.reads["bmpuser_db%bmp_sed2"] = [Read("structure_set_parms", 104, "copy", "x")]
    index.read_statements = {
        "smp_bmpfixed:46": "sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)",
        "structure_set_parms:103": "hru(j)%lumv%bmp_sed = bmpuser_db(istr)%bmp_sed",
        "structure_set_parms:104": "x = bmpuser_db(istr)%bmp_sed2",
    }
    return index


def test_a_bare_name_matches_under_any_parent_and_nothing_longer() -> None:
    index = _index()
    rows = index.read_details("bmp_sed")
    assert [(row["at"], row["kind"]) for row in rows] == [
        ("smp_bmpfixed.f90:46", "assignment"),
        ("structure_set_parms.f90:103", "copy"),
    ]
    assert rows[0]["statement"].endswith("hru(j)%lumv%bmp_sed)")


def test_a_full_path_matches_itself_and_what_it_holds() -> None:
    index = _index()
    assert [r["at"] for r in index.read_details("hru(j)%lumv%bmp_sed")] == [
        "smp_bmpfixed.f90:46"]
    assert [r["at"] for r in index.read_details("hru%lumv")] == ["smp_bmpfixed.f90:46"]
    assert index.read_details("lumv%bmp_sed") == []


def test_the_uses_tool_answers_with_rows_and_filters_by_kind() -> None:
    index = _index()
    rows = t_uses(index, "bmp_sed")
    assert rows[1] == {
        "path": "bmpuser_db%bmp_sed", "at": "structure_set_parms.f90:103",
        "in": "structure_set_parms", "kind": "copy", "detail": "hru%lumv%bmp_sed",
        "statement": "hru(j)%lumv%bmp_sed = bmpuser_db(istr)%bmp_sed",
    }
    assert [row["kind"] for row in t_uses(index, "bmp_sed", kind="copy")] == ["copy"]
    assert t_uses(index, "frsw_gro") == {"variable": "frsw_gro", "reads": "none"}
    text = render_compact(t_uses(index, "bmp_sed"))
    assert text.splitlines()[0] == "path|at|in|kind|detail|statement"


def test_facts_without_reads_never_answer_read_nowhere(tmp_path: Path) -> None:
    """Base facts alone carry no reads; "none" from them would be a lie."""
    index = _index()
    facts = save_snapshot(index, tmp_path / "swatplus-facts.json")
    back = load_snapshot(facts)
    assert back.reads_indexed is False
    assert "not indexed" in t_uses(back, "bmp_sed")["reads"]


def test_reads_round_trip_through_the_sidecar(tmp_path: Path) -> None:
    index = _index()
    facts = save_snapshot(index, tmp_path / "swatplus-facts.json")
    save_rhs(index, tmp_path / "swatplus-rhs.json")
    back = load_snapshot(facts)
    assert back.reads_indexed is True
    assert back.read_details("bmp_sed") == index.read_details("bmp_sed")


def test_a_format_one_sidecar_still_loads_with_no_reads(tmp_path: Path) -> None:
    index = _index()
    facts = save_snapshot(index, tmp_path / "swatplus-facts.json")
    rhs = save_rhs(index, tmp_path / "swatplus-rhs.json")
    payload = json.loads(rhs.read_text(encoding="utf-8"))
    payload["rhs_format"] = "1"
    del payload["reads"], payload["statements"]
    rhs.write_text(json.dumps(payload), encoding="utf-8")
    back = load_snapshot(facts)
    assert back.reads_indexed is False
    assert "not indexed" in t_uses(back, "bmp_sed")["reads"]
