"""Tests for loop-scope recovery -- what is live at a line.

The parser records where a loop starts but not where it ends, so nesting is
recovered from the source. A wrong loop variable in a breakpoint condition
costs a whole compile-and-run cycle to discover, so an unrecoverable file must
report that rather than guess.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tamandua.index.scope import condition_for, loop_ranges, scope_at

REAL_SOURCE = Path(os.environ.get("SWATPLUS_SOURCE", "/workspace/swatplus-62.0.0"))
FORTRAN = REAL_SOURCE / "src" if (REAL_SOURCE / "src").is_dir() else REAL_SOURCE
requires_source = pytest.mark.skipif(
    not FORTRAN.is_dir(), reason="no SWAT+ source checkout (set SWATPLUS_SOURCE)"
)

NESTED = """\
      subroutine demo
      do iauto = 1, n
        do iac = 1, m
          x = 1
        end do
      end do
      do while (eof == 0)
        read (107,*) line
      enddo
      do
        exit
      end do
      return
      end subroutine demo
"""

UNBALANCED = """\
      subroutine broken
      do i = 1, n
        x = 1
      return
      end subroutine broken
"""


@pytest.fixture
def nested(tmp_path: Path) -> Path:
    path = tmp_path / "demo.f90"
    path.write_text(NESTED, encoding="utf-8")
    return path


def test_counted_while_and_bare_loops_are_all_found(nested: Path) -> None:
    """A bare `do` opens a block too, and missing it made 159 files look
    unbalanced when the scan was first written."""
    ranges = loop_ranges(nested)
    assert ranges is not None
    assert [loop.index for loop in ranges] == ["iauto", "iac", None, None]


def test_nesting_is_outermost_first(nested: Path) -> None:
    scopes = scope_at(nested, 4)
    assert [s.index for s in scopes] == ["iauto", "iac"]


def test_line_outside_any_loop(nested: Path) -> None:
    """Not an error -- it is the answer when a routine is called per object."""
    assert scope_at(nested, 13) == []


def test_unbalanced_file_reports_nothing(tmp_path: Path) -> None:
    path = tmp_path / "broken.f90"
    path.write_text(UNBALANCED, encoding="utf-8")
    assert loop_ranges(path) is None
    assert scope_at(path, 3) is None


def test_missing_file_reports_nothing(tmp_path: Path) -> None:
    assert loop_ranges(tmp_path / "absent.f90") is None


def test_comment_only_do_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "c.f90"
    path.write_text("      subroutine s\n      ! do i = 1, n\n      return\n      end\n",
                    encoding="utf-8")
    assert loop_ranges(path) == []


def test_condition_names_every_index(nested: Path) -> None:
    condition = condition_for(scope_at(nested, 4))
    assert condition == "iauto == <value> .and. iac == <value>"


def test_condition_skips_while_loops(nested: Path) -> None:
    """A while loop has no index to pin."""
    assert condition_for(scope_at(nested, 8)) == ""


def test_condition_accepts_extra_terms(nested: Path) -> None:
    condition = condition_for(scope_at(nested, 4), {"time%day": "200"})
    assert condition.endswith("time%day == 200")


@requires_source
def test_real_corpus_nesting_is_recoverable() -> None:
    """647 of 648 files balanced when this was written. If that regresses
    sharply the scan has stopped matching the source."""
    total = resolved = 0
    for path in sorted(FORTRAN.glob("*.f90")):
        total += 1
        resolved += loop_ranges(path) is not None
    assert total > 600
    assert resolved / total > 0.99


@requires_source
def test_real_corpus_scope_of_a_nested_line() -> None:
    scopes = scope_at(FORTRAN / "hru_control.f90", 800)
    assert [s.index for s in scopes] == ["isalt", "jj"]
