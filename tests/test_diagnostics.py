"""Advisory source checks catch obvious damage without pretending to compile."""

from __future__ import annotations

from pathlib import Path

from tamandua.index.diagnostics import scan_source_warnings


def _scan(tmp_path: Path, text: str) -> list:
    source = tmp_path / "src"
    source.mkdir()
    path = source / "fixture.f90"
    path.write_text(text, encoding="utf-8")
    return scan_source_warnings(source, [path])


def _codes(warnings: list) -> set[str]:
    return {warning.code for warning in warnings}


def test_balanced_procedure_has_no_warnings(tmp_path: Path) -> None:
    warnings = _scan(tmp_path, """\
subroutine route(flow)
  implicit none
  real :: flow
  if (flow > 0.) then
    select case (int(flow))
    case (1)
      do i = 1, 2
        flow = flow + i
      end do
    case default
      flow = 0.
    end select
  end if
end subroutine route
""")
    assert warnings == []


def test_unclosed_and_unmatched_blocks_are_reported(tmp_path: Path) -> None:
    warnings = _scan(tmp_path, """\
subroutine broken
  else
  case (1)
  if (ready) then
end subroutine broken
""")
    codes = _codes(warnings)
    assert {"else_without_if", "case_without_select", "block_closed_out_of_order"} <= codes


def test_explicit_end_name_must_match(tmp_path: Path) -> None:
    warnings = _scan(tmp_path, """\
subroutine alpha
end subroutine beta
""")
    assert _codes(warnings) == {"mismatched_end_name"}


def test_duplicate_cases_are_reported(tmp_path: Path) -> None:
    warnings = _scan(tmp_path, """\
subroutine choose
  select case (kind)
  case (1, 2)
  case (2)
  case default
  case default
  end select
end subroutine choose
""")
    assert {"duplicate_case_label", "duplicate_case_default"} <= _codes(warnings)


def test_damaged_statements_are_reported(tmp_path: Path) -> None:
    warnings = _scan(tmp_path, """\
subroutine broken
  x = (a + b
  y = value +
  z = 'unfinished
  total = first &
end subroutine broken
""")
    codes = _codes(warnings)
    assert {
        "unbalanced_parentheses",
        "incomplete_assignment",
        "unbalanced_quote",
    } <= codes


def test_unfinished_continuation_at_eof_is_reported(tmp_path: Path) -> None:
    warnings = _scan(tmp_path, "subroutine broken\n  total = first &\n")
    assert "unfinished_continuation" in _codes(warnings)


def test_implicit_none_arguments_and_duplicate_names_are_reported(tmp_path: Path) -> None:
    warnings = _scan(tmp_path, """\
subroutine broken(a, a, missing)
  implicit none
  real :: a
  integer :: local
  integer :: local
end subroutine broken
""")
    codes = _codes(warnings)
    assert {
        "duplicate_argument",
        "duplicate_declaration",
        "undeclared_argument",
    } <= codes


def test_duplicate_symbols_are_scoped(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    first = source / "a.f90"
    second = source / "b.f90"
    first.write_text("module a\ncontains\nsubroutine same\nend subroutine same\nend module a\n")
    second.write_text("module b\ncontains\nsubroutine same\nend subroutine same\nend module b\n")
    assert "duplicate_symbol" not in _codes(scan_source_warnings(source, [first, second]))


def test_invalid_utf8_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    path = source / "bad.f90"
    path.write_bytes(b"subroutine bad\n  x = '\xff'\nend subroutine bad\n")
    warnings = scan_source_warnings(source, [path])
    assert "invalid_utf8" in _codes(warnings)
    assert all(warning.file == "bad.f90" for warning in warnings)
