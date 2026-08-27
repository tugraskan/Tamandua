"""Tests for the SWAT+ run-output reader.

Unit tests use synthetic files mirroring the real layouts, so the suite needs no
run output. Tests against the Ames reference project are marked and skip when it
is absent.

The refusal tests matter most: 9 of the 25 output files in Ames have a header
whose field count does not match the data rows. Indexing a column by position
there returns a wrong number silently, which is worse than returning nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tamandua.output.reader import (
    OutputError,
    Summary,
    query,
    read_layout,
)

AMES = Path(
    os.environ.get("SWATPLUS_AMES", "/workspace/swatplus-62.0.0/refdata/Ames_sub1")
)
requires_ames = pytest.mark.skipif(
    not AMES.is_dir(), reason="no Ames reference project (set SWATPLUS_AMES)"
)


# Title / header / units / data -- the conventional SWAT+ output layout.
WELL_FORMED = """\
 demo                      SWAT+ 2024-08-27
  jday   mon   day    yr  name     precip      perc
                                       mm        mm
     1     1     1  1975  hru0001   10.500     1.200
     2     1     2  1975  hru0001   20.250    -0.300
     3     1     3  1975  hru0001    5.000     2.400
     1     1     1  1975  hru0002    9.100     0.100
"""

# Header declares more names than the rows ever carry.
MISALIGNED = """\
 demo                      SWAT+ 2024-08-27
  jday   mon   day    yr  name     sed_c    surq_c   surq_doc   surq_dic
     1     1     1  1975  hru0001  3672.0
     2     1     2  1975  hru0001  3671.0
"""

# No units row between header and data.
NO_UNITS = """\
 demo                      SWAT+ 2024-08-27
  jday   mon   day    yr  name     precip
     1     1     1  1975  hru0001   10.500
     2     1     2  1975  hru0001   20.250
"""


@pytest.fixture
def well_formed(tmp_path: Path) -> Path:
    path = tmp_path / "hru_wb_aa.txt"
    path.write_text(WELL_FORMED, encoding="utf-8")
    return path


@pytest.fixture
def misaligned(tmp_path: Path) -> Path:
    path = tmp_path / "hru_carbon_aa.txt"
    path.write_text(MISALIGNED, encoding="utf-8")
    return path


# ------------------------------------------------------------------ layout

def test_layout_finds_header_units_and_data(well_formed: Path) -> None:
    layout = read_layout(well_formed)
    assert layout.header_line == 1
    assert layout.units_line == 2
    assert layout.first_data_line == 3
    assert layout.trusted


def test_layout_handles_a_missing_units_row(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    path.write_text(NO_UNITS, encoding="utf-8")
    layout = read_layout(path)
    assert layout.units_line is None
    assert layout.first_data_line == 2
    assert layout.trusted


def test_layout_distrusts_a_mismatched_header(misaligned: Path) -> None:
    layout = read_layout(misaligned)
    assert not layout.trusted
    assert "cannot be matched by position" in layout.note


def test_missing_file_is_an_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(OutputError, match="no such output file"):
        read_layout(tmp_path / "absent.txt")


# ----------------------------------------------------------------- queries

def test_summary_reports_the_shape_of_a_series(well_formed: Path) -> None:
    result = query(well_formed, "precip", label_by="name")
    assert isinstance(result, Summary)
    assert result.n == 4
    assert result.minimum == 5.0
    assert result.maximum == 20.25
    assert result.maximum_at == "hru0001"


def test_filter_narrows_to_matching_rows(well_formed: Path) -> None:
    result = query(well_formed, "precip", where={"name": "hru0002"})
    assert result.n == 1
    assert result.first == 9.1


def test_negatives_are_counted(well_formed: Path) -> None:
    """The question that starts most debugging: did this go negative?"""
    result = query(well_formed, "perc")
    assert result.negatives == 1
    assert result.minimum == -0.3


def test_raw_returns_the_series(well_formed: Path) -> None:
    rows = query(well_formed, "precip", label_by="name", raw=True)
    assert [v for _, v in rows] == [10.5, 20.25, 5.0, 9.1]


def test_empty_match_is_not_an_error(well_formed: Path) -> None:
    result = query(well_formed, "precip", where={"name": "hru9999"})
    assert result.n == 0
    assert "no rows matched" in result.render()


def test_summary_renders_compactly(well_formed: Path) -> None:
    text = query(well_formed, "precip", label_by="name").render()
    assert len(text) < 120, "a summary that grows stops being cheaper than awk"
    assert "n=4" in text


# ---------------------------------------------------------------- refusals

def test_query_refuses_a_file_it_cannot_index(misaligned: Path) -> None:
    """Silently returning the wrong column is the failure this prevents."""
    with pytest.raises(OutputError, match="cannot be matched by position"):
        query(misaligned, "sed_c")


def test_unknown_column_lists_what_is_available(well_formed: Path) -> None:
    with pytest.raises(OutputError, match="no column 'baseflow'"):
        query(well_formed, "baseflow")


# ------------------------------------------------------------- real corpus

@requires_ames
def test_real_ames_water_balance() -> None:
    result = query(AMES / "hru_wb_aa.txt", "surq_gen", label_by="name")
    assert result.n == 12
    assert result.minimum == pytest.approx(24.082, abs=0.001)
    assert result.minimum_at == "hru0002"
    assert result.maximum == pytest.approx(33.134, abs=0.001)
    assert result.maximum_at == "hru0005"


@requires_ames
def test_real_ames_has_files_that_must_be_refused() -> None:
    """Not a hypothetical: a third of the shipped output cannot be indexed.

    Both outcomes count as a refusal -- a header that will not line up, and a
    file that is not SWAT+ output at all. Either way the reader declines rather
    than returning a number from the wrong column.
    """
    trusted = refused = 0
    for path in sorted(AMES.glob("*.txt")):
        try:
            layout = read_layout(path)
        except OutputError:
            refused += 1
            continue
        trusted += layout.trusted
        refused += not layout.trusted

    assert trusted >= 10, "expected most Ames output to be queryable"
    assert refused >= 5, "expected several Ames files the reader must decline"
