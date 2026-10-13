"""Tests for indexing a main program kept in a CMake template.

SWAT+'s ``program main`` is in ``src/main.f90.in``. The scanner never read
the file (its suffix is ``.in``) and keeps no assignments, I/O or declarations
for a ``program`` unit, so every routine called only from it had no caller
and no call path: ``callers("lsu_read_elements")`` answered none although
``main.f90.in:69`` calls it.

The text rewrite and the suffix rule run without the parser; the builds below
need it and skip without it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tamandua.index import (
    build_source_index,
    is_fortran_source,
    looks_like_swatplus,
    program_as_subroutine,
    source_files,
    source_fingerprint,
)
from tamandua.mcp.server import t_call_path, t_callers, t_find_procedure, t_uses, t_writers

CORPUS = os.environ.get("SWATPLUS_REFERENCE_CORPUS")


def _corpus_available() -> bool:
    try:
        import swatplus_reference  # noqa: F401
    except ImportError:
        return bool(CORPUS and (Path(CORPUS) / "src").is_dir())
    return True


requires_corpus = pytest.mark.skipif(
    not _corpus_available(), reason="no swatplus-reference-corpus (set SWATPLUS_REFERENCE_CORPUS)"
)

# Shaped like SWAT+'s own: a bare `end`, CMake placeholders, a trailing comment.
MAIN = """\
      program main

      use hru_module

      implicit none

      external :: proc_read, lsu_read_elements
      integer :: iob = 0

      prog = " SWAT+ @TODAY@ Rev @SWAT_VERSION@"
      open (9003,file='simulation.out')
      call proc_read
      call lsu_read_elements        !defining landscape units by hru
      do ihru = 1, nhru
        isol = hru(ihru)%dbs%soil
      end do
      write (9003,*) iob
      stop
      end
"""

HRU_MODULE = """\
      module hru_module
        type hru_databases
          integer :: soil = 0
        end type hru_databases
        type hydrologic_response_unit
          type (hru_databases) :: dbs
          type (land_use_mgt_variables) :: lumv
        end type hydrologic_response_unit
        type land_use_mgt_variables
          real :: bmp_sed = 0.   !! %   | Sediment removal by BMP
          real :: bmp_bac = 0.   !! %   | Bacteria removal by BMP
        end type land_use_mgt_variables
        type (hydrologic_response_unit), dimension(:), allocatable :: hru
        real, dimension(:), allocatable :: sedyld
        integer :: ihru = 0, isol = 0, nhru = 0
        character(len=80) :: prog = ""
      end module hru_module
"""

PROC_READ = """\
      subroutine proc_read
      call hru_read
      end subroutine proc_read
"""

HRU_READ = """\
      subroutine hru_read
      end subroutine hru_read
"""

LSU_READ = """\
      subroutine lsu_read_elements
      end subroutine lsu_read_elements
"""

# Nothing calls it: dead code, and one call from lsu_read_elements -- closer
# than main's route, which is what used to win the first call-path slot.
DEAD = """\
      subroutine reg_read_elements
      call lsu_read_elements
      end subroutine reg_read_elements
"""

BMP = """\
      subroutine smp_bmpfixed (j)
      use hru_module
      integer, intent (in) :: j
      sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)
      end subroutine smp_bmpfixed
"""

STRUCTURE = """\
      subroutine structure_set_parms (j, istr)
      use hru_module
      integer, intent (in) :: j, istr
      hru(j)%lumv%bmp_sed = hru(istr)%lumv%bmp_sed !! Sediment
      hru(j)%lumv%bmp_bac = hru(istr)%lumv%bmp_bac !! Bacteria
      end subroutine structure_set_parms
"""


@pytest.fixture
def program_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name, text in (
        ("main.f90.in", MAIN), ("hru_module.f90", HRU_MODULE),
        ("proc_read.f90", PROC_READ), ("hru_read.f90", HRU_READ),
        ("lsu_read_elements.f90", LSU_READ), ("reg_read_elements.f90", DEAD),
        ("smp_bmpfixed.f90", BMP), ("structure_set_parms.f90", STRUCTURE),
    ):
        (src / name).write_text(text, encoding="utf-8")
    return src


@pytest.fixture
def index(program_source: Path):
    return build_source_index(program_source, Path(CORPUS) if CORPUS else None)


# ------------------------------------------------------------- the rewrite

def test_a_program_is_spelled_as_a_subroutine_and_nothing_else_moves() -> None:
    rewritten, names = program_as_subroutine(MAIN)
    assert names == ["main"]
    before, after = MAIN.splitlines(), rewritten.splitlines()
    assert len(after) == len(before)
    assert after[0] == "      subroutine main"
    assert after[-1] == "      end subroutine"
    changed = [number for number, (a, b) in enumerate(zip(before, after)) if a != b]
    assert changed == [0, len(before) - 1]


def test_end_program_and_a_trailing_comment_are_kept() -> None:
    text = "program swat\ncall run\nend program swat ! done\n"
    rewritten, names = program_as_subroutine(text)
    assert names == ["swat"]
    assert rewritten == "subroutine swat\ncall run\nend subroutine swat ! done\n"


def test_a_bare_end_after_contains_closes_an_internal_procedure_not_the_program() -> None:
    text = ("program p\ncall q\ncontains\nsubroutine q\nend\n"
            "end program p\n")
    rewritten, _ = program_as_subroutine(text)
    assert rewritten.splitlines()[4] == "end"
    assert rewritten.splitlines()[5] == "end subroutine p"


def test_text_without_a_program_is_unchanged() -> None:
    assert program_as_subroutine(BMP) == (BMP, [])
    text = "subroutine s\nprogram_name = 'x'\nend subroutine s\n"
    assert program_as_subroutine(text) == (text, [])


# ------------------------------------------------------------ the suffixes

@pytest.mark.parametrize("name, expected", [
    ("main.f90.in", True), ("MAIN.F90.IN", True), ("legacy.f.in", True),
    ("aqu_read.f90", True), ("AQU_READ.F90", True),
    ("CMakeLists.txt.in", False), ("version.in", False), ("notes.md", False),
])
def test_a_template_of_a_fortran_source_is_a_source(name: str, expected: bool) -> None:
    assert is_fortran_source(Path(name)) is expected


def test_the_fingerprint_follows_the_main_program(program_source: Path) -> None:
    """An edit to main.f90.in must make the index stale."""
    assert program_source / "main.f90.in" in source_files(program_source)
    before = source_fingerprint(program_source)
    main = program_source / "main.f90.in"
    main.write_text(MAIN.replace("call proc_read\n", ""), encoding="utf-8")
    assert source_fingerprint(program_source) != before


def test_a_tree_holding_only_a_template_looks_like_source(tmp_path: Path) -> None:
    (tmp_path / "main.f90.in").write_text(MAIN, encoding="utf-8")
    assert looks_like_swatplus(tmp_path)


# ------------------------------------------------------------- the build

@requires_corpus
def test_the_main_program_is_a_procedure_in_its_real_file(index) -> None:
    main = index.procedure("main")
    assert main is not None
    assert main.kind == "program"
    assert main.path == "main.f90.in"
    assert main.location == f"main.f90.in:1-{len(MAIN.splitlines())}"
    assert t_find_procedure(index, "main")["kind"] == "program"
    assert [item.name for item in main.locals] == ["iob"]
    assert index.procedure("lsu_read_elements").kind == "subroutine"


@requires_corpus
def test_callers_name_the_main_program_and_the_line(index) -> None:
    assert "main" in index.callers_of("lsu_read_elements")
    answer = t_callers(index, "lsu_read_elements")
    assert "main.f90.in:13 (main)" in answer["call_sites"]
    assert "reg_read_elements.f90:2 (reg_read_elements)" in answer["call_sites"]
    assert t_callers(index, "proc_read")["call_sites"] == ["main.f90.in:12 (main)"]


@requires_corpus
def test_call_paths_root_at_the_main_program(index) -> None:
    assert index.paths_to("lsu_read_elements")[0] == ["main", "lsu_read_elements"]
    assert index.paths_to("hru_read") == [["main", "proc_read", "hru_read"]]
    assert t_call_path(index, "hru_read") == [{"path": "main -> proc_read -> hru_read"}]


@requires_corpus
def test_the_programs_own_statements_are_indexed(index) -> None:
    """The scanner kept a program's calls and nothing else."""
    assert any(use.procedure == "main" and use.op == "open"
               for use in index.io_for_file("simulation.out"))
    assert "main:15" in index.writers_of("isol")
    assert [loop.index for loop in index.loops_in("main")] == ["ihru"]
    rows = t_uses(index, "hru%dbs%soil")
    assert [(row["at"], row["kind"], row["detail"]) for row in rows] == [
        ("main.f90.in:15", "copy", "isol")]


@requires_corpus
def test_writers_return_the_formula_for_a_plain_array(index) -> None:
    """`sedyld(j) = ...` was kept only for derived-type paths before."""
    answer = t_writers(index, "sedyld")
    assert answer["assignments"] == [{
        "at": "smp_bmpfixed:4",
        "expression": "sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)",
    }]


@requires_corpus
def test_uses_find_the_formula_and_mark_the_copies(index) -> None:
    rows = t_uses(index, "bmp_sed")
    assert [(row["at"], row["kind"]) for row in rows] == [
        ("smp_bmpfixed.f90:4", "assignment"),
        ("structure_set_parms.f90:4", "copy"),
    ]
    assert rows[0]["statement"] == "sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)"
    assert rows[1]["detail"] == "hru%lumv%bmp_sed"
    # Only ever copied: the comment naming it is no read, nor is the target.
    assert [(row["at"], row["kind"]) for row in t_uses(index, "bmp_bac")] == [
        ("structure_set_parms.f90:5", "copy")]
