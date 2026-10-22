"""``writers`` finds what sets a variable through its components and through READ statements.

A live swat-investigator check of swatplus PR 307 asked ``writers`` for
``hpathb_y`` and ``pco%pest%y`` and got ``none``: every assignment to
``hpathb_y`` is to a component (``hpathb_y(j)%path(ipath) = ...``), and
``pco%pest%y`` is set only by a list-directed READ of print.prt.
"""

from __future__ import annotations

from tamandua.index import IOUse, Provenance, SourceIndex, WriterStatement
from tamandua.index.build import read_targets
from tamandua.mcp.server import t_writers


def make_index() -> SourceIndex:
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit=None, source_describe=None,
        source_fingerprint="abc", generated_at="2026-10-22T00:00:00Z",
        format_version="6", parser_commit=None,
    ))
    index.writers["hpathb_y%path"] = ["hru_pathogen_output:52", "hru_pathogen_output:68"]
    index.writer_statements["hpathb_y%path"] = [WriterStatement(
        procedure="hru_pathogen_output", line=52,
        raw="hpathb_y(j)%path(ipath) = hpathb_y(j)%path(ipath) + hpathb_m(j)%path(ipath)")]
    reads = [
        IOUse(file="print.prt", op="read", unit="107", procedure="basin_print_codes_read", line=145,
              fields=("name", "pco%pest%d", "pco%pest%m", "pco%pest%y", "pco%pest%a")),
        IOUse(file="constituents.cs", op="read", unit="106", procedure="constit_db_read", line=40,
              fields=("(cs_db%pests(i), i = 1, cs_db%num_pests)",)),
        IOUse(file="soils.sol", op="read", unit="107", procedure="soil_db_read", line=60, fields=("soildb(isol)%s",)),
        IOUse(file="aquifer_day.txt", op="write", unit="2520", procedure="aquifer_output", line=30,
              fields=("pco%pest%y",)),
    ]
    for use in reads:
        index.io_by_file[use.file].append(use)
    return index


def test_component_assignments_set_the_variable() -> None:
    found = t_writers(make_index(), "hpathb_y")["assignments"]
    assert [(item["at"], item.get("target")) for item in found] == [
        ("hru_pathogen_output:52", "hpathb_y%path"), ("hru_pathogen_output:68", "hpathb_y%path")]
    assert found[0]["expression"].startswith("hpathb_y(j)%path(ipath) =")


def test_a_list_directed_read_sets_the_items_it_lists() -> None:
    found = t_writers(make_index(), "pco%pest%y")["assignments"]
    assert found == [{"at": "basin_print_codes_read:145", "via": "read", "target": "pco%pest%y",
                      "file": "print.prt", "expression": "unavailable"}]
    # A whole record read sets each component, and a component read sets the record.
    assert [item["target"] for item in t_writers(make_index(), "pco%pest")["assignments"]] == [
        "pco%pest%d", "pco%pest%m", "pco%pest%y", "pco%pest%a"]
    assert [item["at"] for item in t_writers(make_index(), "soildb%s%alb")["assignments"]] == ["soil_db_read:60"]


def test_an_implied_do_sets_its_items_not_its_bounds() -> None:
    assert read_targets("(cs_db%pests(i), i = 1, cs_db%num_pests)") == ["cs_db%pests"]
    assert read_targets("((a(i, j), j = 1, m), b(i), i = 1, n)") == ["a", "b"]
    assert read_targets("hru(j)%sdr") == ["hru%sdr"]
    found = t_writers(make_index(), "cs_db%pests")["assignments"]
    assert [(item["at"], item["file"]) for item in found] == [("constit_db_read:40", "constituents.cs")]
    assert t_writers(make_index(), "cs_db%num_pests")["assignments"] == "none"


def test_a_write_statement_sets_nothing() -> None:
    assert t_writers(make_index(), "pco%pest%d")["assignments"][0]["file"] == "print.prt"
    assert all(item.get("file") != "aquifer_day.txt" for item in t_writers(make_index(), "pco%pest%y")["assignments"])


def test_a_common_name_lists_twenty_reads_and_counts_the_rest_by_file() -> None:
    # Review of tugraskan/Tamandua#11: every file's reader starts with `name`, hundreds of READ sites.
    index = make_index()
    for number in range(30):
        use = IOUse(file=f"table{number % 3}.dat", op="read", unit="107", procedure=f"reader{number}", line=10,
                    fields=("name", "value"))
        index.io_by_file[use.file].append(use)
    answer = t_writers(index, "name")
    reads = [item for item in answer["assignments"] if item.get("via") == "read"]
    assert len(reads) == 20
    # 31 with print.prt's own `name`; the first 20 in statement order are listed.
    assert answer["reads_omitted"] == {"count": 11, "by_file": {"table1.dat": 1, "table2.dat": 10}}
    assert "reads_omitted" not in t_writers(index, "pco%pest%y")


def test_the_path_index_is_built_once_and_follows_new_reads(monkeypatch) -> None:
    # Review of tugraskan/Tamandua#11: each call scanned every writer key and every READ.
    from tamandua.index import build

    calls = []
    original = build.read_targets
    monkeypatch.setattr(build, "read_targets", lambda item: calls.append(item) or original(item))
    index = make_index()
    index.writer_details("pco%pest%y")
    scanned = len(calls)
    index.writer_details("pco%pest%y")
    index.writer_details("hpathb_y")
    assert len(calls) == scanned, "a second lookup reads the cached path index"
    index.io_by_file["new.dat"].append(IOUse(file="new.dat", op="read", unit="9", procedure="late", line=3,
                                             fields=("pco%pest%y",)))
    assert [item["at"] for item in index.writer_details("pco%pest%y")] == ["basin_print_codes_read:145", "late:3"]


def test_a_read_of_an_enclosing_record_names_that_record() -> None:
    # Review of tugraskan/Tamandua#11: a whole-record READ was reported like a READ of the field itself.
    deep = t_writers(make_index(), "soildb%s%alb")["assignments"]
    assert deep == [{"at": "soil_db_read:60", "via": "read", "target": "soildb%s", "file": "soils.sol",
                     "expression": "unavailable", "record": "soildb%s"}]
    direct = t_writers(make_index(), "pco%pest%y")["assignments"]
    assert "record" not in direct[0]
