"""Tests for input-file links: which column names a row of another file.

SWAT+ declares no foreign keys; it searches at run time. The build keeps each
equality test between two variables as a fact (``Comparison``), with the
copies that may carry a column's value to one (``Copy``), and
``tamandua.index.layouts`` derives the links from those and the layouts.

Most tests build an index by hand, as test_layouts does, so they need neither
the parser nor a checkout. The bundled-snapshot tests pin what the shipped
facts say about real files, and the parser-gated ones cover the build half.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tamandua.index import (
    Comparison,
    Copy,
    DerivedType,
    Field,
    IOUse,
    Loop,
    ModuleVariable,
    Procedure,
    Provenance,
    SourceIndex,
    VariableDeclaration,
    build_source_index,
)
from tamandua.index.build import (
    caller_opened_files,
    comparison_operand,
    condition_comparisons,
    if_condition,
    substitute_actual,
)
from tamandua.index.layouts import (
    LAYOUT_FORMAT,
    file_layout,
    file_layouts,
    input_links,
    layout_json,
    links_json,
)
from tamandua.index.layouts_cli import _load, main

CORPUS = os.environ.get("SWATPLUS_REFERENCE_CORPUS")
REAL_SOURCE = Path(os.environ.get("SWATPLUS_SOURCE", "/workspace/swatplus-62.0.0"))


def _corpus_available() -> bool:
    try:
        import swatplus_reference  # noqa: F401
    except ImportError:
        return bool(CORPUS and (Path(CORPUS) / "src").is_dir())
    return True


requires_corpus = pytest.mark.skipif(
    not _corpus_available(), reason="no swatplus-reference-corpus (set SWATPLUS_REFERENCE_CORPUS)"
)
requires_real = pytest.mark.skipif(
    not ((REAL_SOURCE / "src").is_dir() or REAL_SOURCE.is_dir()) or not _corpus_available(),
    reason="needs a SWAT+ checkout and swatplus-reference-corpus",
)


# ------------------------------------------------------ condition parsing

@pytest.mark.parametrize("condition, expected", [
    ("hru_db(i)%dbsc%land_use_mgt == lum(ilum)%name",
     [("hru_db(i)%dbsc%land_use_mgt", "==", "lum(ilum)%name")]),
    # Reversed operands are kept as written; which side is searched is
    # decided later, from the loops.
    ("lum(ilum)%name .eq. hru_db(i)%dbsc%land_use_mgt",
     [("lum(ilum)%name", "==", "hru_db(i)%dbsc%land_use_mgt")]),
    ("trim(adjustl(a%b)) == trim(c(i)%name)", [("a%b", "==", "c(i)%name")]),
    ("shf_db(i)%jday == jday .and. shf_db(i)%lsu == ilsu",
     [("shf_db(i)%jday", "==", "jday"), ("shf_db(i)%lsu", "==", "ilsu")]),
    (".not. (a%x == b(k)%name)", [("a%x", "==", "b(k)%name")]),
    ("x%y /= z(j)%name", [("x%y", "/=", "z(j)%name")]),
    ("x%y .ne. z(j)%name", [("x%y", "/=", "z(j)%name")]),
    # A literal, a number or an expression is not a variable.
    ('in_hru%hru_data == "null"', []),
    ("hru_db(i)%dbs%land_use_mgt == 0", []),
    ("a(i) + 1 == b(j)", []),
    # `<=` is not an equality.
    ("a(i) <= b(j)", []),
    # An equality inside a subscript is not the condition's.
    ("a(merge(1, 2, x == y)) > 0", []),
])
def test_condition_comparisons(condition, expected) -> None:
    assert condition_comparisons(condition) == expected


def test_if_condition_reads_if_and_else_if() -> None:
    assert if_condition("if (a == b(i)%name) then") == "a == b(i)%name"
    assert if_condition("else if (x(j) == y) then") == "x(j) == y"
    assert if_condition("if (a == 'q)') exit") == "a == 'q)'"
    assert if_condition("do i = 1, n") is None


def test_comparison_operand_strips_padding_and_whitespace() -> None:
    assert comparison_operand(" trim( lum ( ilum ) % name ) ") == "lum(ilum)%name"
    assert comparison_operand("'text'") is None
    assert comparison_operand("1.5") is None


# ------------------------------------------------ build: through a call

def test_substitute_actual_restates_a_search_at_its_call() -> None:
    call = "call search (wst_n, db_mx%wst, ob(i)%wst_c, ob(i)%wst)"
    dummies = ["sch", "max", "cfind", "iseq"]
    # The array handed whole is searched element by element: `(:)`.
    assert substitute_actual("sch(nn)", "search", dummies, call) == "wst_n(:)"
    assert substitute_actual("cfind", "search", dummies, call) == "ob(i)%wst_c"
    # Not a dummy: unchanged.
    assert substitute_actual("time%yrc", "search", dummies, call) == "time%yrc"


def test_substitute_actual_refuses_what_it_cannot_restate() -> None:
    dummies = ["sch", "max", "cfind", "iseq"]
    # An element handed to a dummy the callee indexes itself.
    assert substitute_actual("sch(nn)", "search", dummies,
                             "call search (names(3), n, x, k)") is None
    # An expression is not a variable.
    assert substitute_actual("cfind", "search", dummies,
                             'call search (names, n, "lit", k)') is None
    # By keyword, and a component under the dummy.
    assert substitute_actual("cfind%name", "search", dummies,
                             "call search (names, n, iseq=k, cfind=obj(j))") == "obj(j)%name"


def test_caller_opened_files_needs_every_call_site_to_agree() -> None:
    events = {
        "mgt_read": [(33, "open", "107", "management.sch"), (98, "close", "107", "")],
        "late": [(10, "open", "107", "x.sch")],
        "closed": [(5, "open", "107", "y.sch"), (8, "close", "107", "")],
    }
    assert caller_opened_files("read_ops", "107", {"read_ops": [("mgt_read", 91)]},
                               events) == ["management.sch"]
    # Opened after the call, or closed before it: unknown, so nothing.
    assert caller_opened_files("read_ops", "107", {"read_ops": [("late", 5)]}, events) == []
    assert caller_opened_files("read_ops", "107", {"read_ops": [("closed", 9)]}, events) == []
    # One unresolved call site is enough to keep the unit.
    assert caller_opened_files(
        "read_ops", "107",
        {"read_ops": [("mgt_read", 91), ("closed", 9)]}, events) == []


# ----------------------------------------------------------- hand-built

def _field(type_name: str, name: str, vartype: str = "character(len=40)",
           declaration: str | None = None) -> Field:
    return Field(type_name=type_name, name=name, vartype=vartype, units=None,
                 description=None, location=f"db_module.f90:{len(name)}",
                 declaration=declaration or f"{vartype} :: {name}")


def _type(name: str, *fields: Field) -> DerivedType:
    return DerivedType(name=name, module="db_module", location="db_module.f90:1",
                       fields=list(fields))


def _module_var(name: str, vartype: str, array: bool = True) -> ModuleVariable:
    shape = ", dimension(:), allocatable" if array else ""
    return ModuleVariable(name=name, module="db_module", vartype=vartype,
                          declaration=f"{vartype}{shape} :: {name}", line=1,
                          units=None, description=None)


def _local(name: str, vartype: str = "integer") -> VariableDeclaration:
    return VariableDeclaration(name=name, declaration=f"{vartype} :: {name}",
                               line=2, vartype=vartype, initial=None, units=None,
                               description=None)


def _reader(idx: SourceIndex, proc: str, file: str, loop: str, *fields: str,
            locals_: tuple[str, ...] = (), end: int = 14) -> None:
    """A reader: title line, then one record per iteration of ``loop``."""
    idx.procedures[proc] = Procedure(
        name=proc, module=None, location=f"{proc}.f90:1-40", path=f"{proc}.f90",
        locals=[_local("titldum", "character(len=80)"), _local(loop),
                *(_local(name) for name in locals_)])
    for use in (IOUse(file, "open", "107", proc, 10),
                IOUse(file, "read", "107", proc, 11, ("titldum",)),
                IOUse(file, "read", "107", proc, 13, fields)):
        idx.io_by_file[file].append(use)
    idx.loops[proc] = [Loop(proc, 12, f"do {loop} = 1, n", end, loop)]


def _compare(idx: SourceIndex, proc: str, line: int, left: str, right: str,
             loops: tuple[str, ...] | None, op: str = "==", via: str | None = None) -> None:
    idx.comparisons[proc].append(Comparison(proc, line, left, op, right, loops, via))


def _copy(idx: SourceIndex, proc: str, line: int, target: str, source: str,
          op: str = "=") -> None:
    item = Copy(proc, line, target, op, source)
    idx.copies[item.target_path].append(item)
    idx.writers[item.target_path].append(f"{proc}:{line}")


@pytest.fixture
def index() -> SourceIndex:
    idx = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit="de210d6", source_describe="62.0.0",
        source_fingerprint="abc", generated_at="t", format_version="5",
        parser_commit="110c2a2"))
    idx.types["hru_char"] = _type("hru_char", _field("hru_char", "name"),
                                  _field("hru_char", "topo"),
                                  _field("hru_char", "land_use_mgt"))
    idx.types["hru_db_type"] = _type(
        "hru_db_type", _field("hru_db_type", "name"),
        _field("hru_db_type", "dbsc", "type (hru_char)"))
    idx.types["topo_type"] = _type("topo_type", _field("topo_type", "name"),
                                   _field("topo_type", "slope", "real"))
    idx.types["lum_type"] = _type("lum_type", _field("lum_type", "name"),
                                  _field("lum_type", "mgt"))
    idx.types["op_type"] = _type("op_type", _field("op_type", "op"),
                                 _field("op_type", "op_char"))
    idx.types["sched_type"] = _type(
        "sched_type", _field("sched_type", "name"),
        _field("sched_type", "ops", "type (op_type)",
               "type (op_type), dimension(:), allocatable :: ops"))
    idx.types["tbl_type"] = _type("tbl_type", _field("tbl_type", "name"),
                                  _field("tbl_type", "opt"))
    idx.types["sta_type"] = _type("sta_type", _field("sta_type", "name"),
                                  _field("sta_type", "wgn"))
    for name, vartype, array in [
        ("hru_db", "type (hru_db_type)", True), ("hru", "type (hru_db_type)", True),
        ("hru_init", "type (hru_db_type)", True), ("topo_db", "type (topo_type)", True),
        ("lum", "type (lum_type)", True), ("sched", "type (sched_type)", True),
        ("mgt", "type (op_type)", False), ("tbl_a", "type (tbl_type)", True),
        ("tbl_b", "type (tbl_type)", True), ("d_tbl", "type (tbl_type)", False),
        ("wst", "type (sta_type)", True), ("wgn_n", "character(len=40)", True),
        ("tname", "character(len=40)", False),
    ]:
        idx.module_variables[("db_module", name)] = _module_var(name, vartype, array)

    # The record loop runs on past the comparisons below, as hru_read's does.
    _reader(idx, "hru_read", "hru-data.hru", "ihru", "k", "hru_db(ihru)",
            locals_=("k", "ith", "ilum", "i"), end=40)
    _reader(idx, "topo_read", "topography.hyd", "ith", "topo_db(ith)")
    _reader(idx, "lum_read", "landuse.lum", "ilu", "lum(ilu)")
    _reader(idx, "sch_read", "management.sch", "isch", "sched(isch)%name")
    idx.io_by_file["management.sch"].append(IOUse(
        "management.sch", "read", "107", "sch_read", 20, ("sched(isch)%ops(iop)",)))
    idx.loops["sch_read"].append(Loop("sch_read", 19, "do iop = 1, n", 21, "iop"))
    _reader(idx, "a_read", "a.dtl", "i", "tbl_a(i)")
    _reader(idx, "b_read", "b.dtl", "i", "tbl_b(i)")
    _reader(idx, "wgn_read", "weather-wgn.cli", "iwgn", "wgn_n(iwgn)")
    _reader(idx, "sta_read", "weather-sta.cli", "i", "wst(i)")
    for name in ("mgt_sched", "other", "search"):
        idx.procedures[name] = Procedure(
            name=name, module=None, location=f"{name}.f90:1-40", path=f"{name}.f90",
            locals=[_local("k"), _local("jday")] if name == "other" else [])

    # The pattern: a loop over topo_db, one side subscripted by it.
    _compare(idx, "hru_read", 30, "hru_db(ihru)%dbsc%topo", "topo_db(ith)%name",
             ("ihru", "ith"))
    # The same, operands reversed.
    _compare(idx, "hru_read", 31, "lum(ilum)%name", "hru_db(ihru)%dbsc%land_use_mgt",
             ("ihru", "ilum"))
    _compare(idx, "hru_read", 32, "hru_db(ihru)%dbsc%topo", "topo_db(ith)%name",
             ("ihru", "ith"), op="/=")
    _compare(idx, "hru_read", 33, "hru_db(ihru)%dbsc%topo", "topo_db(1)%name", ("ihru",))
    # Searching the operations of one schedule, not the rows of a file.
    _compare(idx, "sch_read", 22, "sched(isch)%ops(iop)%op_char", "lum(1)%name",
             ("isch", "iop"))
    # A local compared where it was never read.
    _compare(idx, "other", 5, "topo_db(ith)%name", "k", ("ith",))
    # Copied once, from one column.
    _copy(idx, "mgt_sched", 10, "mgt", "sched(isched)%ops(iop)")
    _compare(idx, "mgt_sched", 12, "lum(ilu)%name", "mgt%op_char", ("ilu",))
    # Pointed at two different tables.
    _copy(idx, "mgt_sched", 14, "d_tbl", "tbl_a(i)", op="=>")
    _copy(idx, "mgt_sched", 15, "d_tbl", "tbl_b(i)", op="=>")
    _compare(idx, "mgt_sched", 16, "d_tbl%opt", "lum(ilu)%name", ("ilu",))
    # Restored from a saved copy of itself, and filled from the file once.
    _copy(idx, "mgt_sched", 20, "hru", "hru_init")
    _copy(idx, "mgt_sched", 21, "hru_init", "hru")
    _copy(idx, "mgt_sched", 22, "hru(j)%dbsc", "hru_db(i)%dbsc")
    _compare(idx, "mgt_sched", 24, "hru(j)%dbsc%topo", "topo_db(ith)%name", ("ith",))
    # A column tested against a copy of itself.
    _copy(idx, "mgt_sched", 26, "tname", "topo_db(1)%name")
    _compare(idx, "mgt_sched", 27, "topo_db(ith)%name", "tname", ("ith",))
    # A search made in a called routine, restated at the call.
    _compare(idx, "sta_read", 30, "wgn_n(:)", "wst(i)%wgn", ("i",), via="search:22")
    return idx


def _links(idx: SourceIndex) -> dict[tuple[str, str], list[str]]:
    links, _ = input_links(idx)
    found: dict[tuple[str, str], list[str]] = {}
    for link in links:
        found.setdefault((f"{link.source_file}:{link.source_path}",
                          f"{link.target_file}:{link.target_path}"), []).append(link.at)
    return found


def _reasons(idx: SourceIndex) -> dict[str, str]:
    return {item.at: item.reason for item in input_links(idx)[1]}


def test_a_search_over_the_target_rows_is_a_link(index) -> None:
    links, _ = input_links(index)
    link = next(item for item in links if item.at == "hru_read.f90:30")
    assert (link.source_file, link.source_path) == ("hru-data.hru", "hru_db%dbsc%topo")
    assert (link.target_file, link.target_path) == ("topography.hyd", "topo_db%name")
    assert link.compares == "hru_db(ihru)%dbsc%topo == topo_db(ith)%name"
    assert link.through == []


def test_operands_either_way_round_link_the_same_way(index) -> None:
    assert ("hru-data.hru:hru_db%dbsc%land_use_mgt", "landuse.lum:lum%name") in _links(index)


def test_what_is_not_a_search_says_why(index) -> None:
    reasons = _reasons(index)
    assert reasons["hru_read.f90:32"] == "an inequality, not a search"
    assert reasons["hru_read.f90:33"] == (
        "the loop over ihru reads hru_db's rows; a test inside it is not a search")
    assert reasons["sch_read.f90:22"] == \
        "the loop over iop runs over part of sched, not its rows"


def test_a_local_matches_only_what_its_own_routine_reads(index) -> None:
    # `k` is a column of hru-data.hru, but as hru_read's local, not other's.
    assert _reasons(index)["other.f90:5"] == "k is not a column any input file is read into"


def test_a_copy_with_one_source_is_followed(index) -> None:
    links, _ = input_links(index)
    link = next(item for item in links if item.at == "mgt_sched.f90:12")
    assert (link.source_file, link.source_path) == ("management.sch", "sched%ops%op_char")
    assert (link.target_file, link.target_path) == ("landuse.lum", "lum%name")
    assert link.through == ["mgt_sched.f90:10"]


def test_a_copy_from_several_columns_is_not(index) -> None:
    assert _reasons(index)["mgt_sched.f90:16"] == \
        "d_tbl%opt is assigned from 2 columns: tbl_a%opt, tbl_b%opt"


def test_a_copy_back_from_itself_adds_no_source(index) -> None:
    links, _ = input_links(index)
    link = next(item for item in links if item.at == "mgt_sched.f90:24")
    assert (link.source_file, link.source_path) == ("hru-data.hru", "hru_db%dbsc%topo")
    assert set(link.through) == {"mgt_sched.f90:20", "mgt_sched.f90:21", "mgt_sched.f90:22"}


def test_a_column_tested_against_itself_is_not_a_link(index) -> None:
    assert _reasons(index)["mgt_sched.f90:27"] == "both sides hold the same column"


def test_a_search_in_a_called_routine_is_a_link(index) -> None:
    links, _ = input_links(index)
    link = next(item for item in links if item.at == "sta_read.f90:30")
    assert (link.source_file, link.source_path) == ("weather-sta.cli", "wst%wgn")
    assert (link.target_file, link.target_path) == ("weather-wgn.cli", "wgn_n")
    assert link.through == ["search.f90:22"]


def test_an_uncalled_legacy_reader_does_not_create_a_second_target(index) -> None:
    _reader(index, "legacy_wgn_read", "legacy-wgn.cli", "i", "wgn_n(i)")
    index.procedures["wgn_read"].called_by = ["proc_db"]
    links = [item for item in input_links(index)[0]
             if item.at == "sta_read.f90:30"]
    assert [(item.target_file, item.target_path) for item in links] == [
        ("weather-wgn.cli", "wgn_n")]


def test_nothing_is_linked_by_a_name_alone(index) -> None:
    # `lum%mgt` and `sched%name` look made for each other; no comparison
    # joins them, so no link does either.
    assert not [key for key in _links(index) if key[0] == "landuse.lum:lum%mgt"]


def test_references_land_on_the_source_column(index) -> None:
    main = file_layout(index, "hru-data.hru").main
    topo = next(column for column in main.columns if column.name == "topo")
    targets = {(ref.file, ref.column): ref for ref in topo.references}
    direct = targets[("topography.hyd", "name")]
    assert direct.role == "main" and direct.position == 1
    assert direct.evidence == ["hru_read.f90:30", "mgt_sched.f90:24"]
    assert direct.through == ["mgt_sched.f90:20", "mgt_sched.f90:21", "mgt_sched.f90:22"]
    name = next(column for column in main.columns if column.name == "name")
    assert name.references == []


def test_json_carries_references_on_every_column(index) -> None:
    assert LAYOUT_FORMAT == "2"
    layout = layout_json(file_layout(index, "hru-data.hru"))
    columns = layout["records"][0]["columns"]
    assert all("references" in column for column in columns)
    topo = next(column for column in columns if column["name"] == "topo")
    assert topo["references"][0] == {
        "file": "topography.hyd", "column": "name", "path": "topo_db%name",
        "role": "main", "position": 1,
        "evidence": ["hru_read.f90:30", "mgt_sched.f90:24"],
        "through": ["mgt_sched.f90:20", "mgt_sched.f90:21", "mgt_sched.f90:22"],
    }


def test_links_json_lists_links_and_what_did_not_link(index) -> None:
    payload = links_json(index)
    assert payload["layout_format"] == LAYOUT_FORMAT
    link = next(item for item in payload["links"] if item["file"] == "weather-sta.cli")
    assert link["target_file"] == "weather-wgn.cli"
    assert link["evidence"] == [{"at": "sta_read.f90:30", "procedure": "sta_read",
                                 "compares": "wgn_n(:) == wst(i)%wgn",
                                 "through": ["search.f90:22"]}]
    reasons = {item["at"]: item["reason"] for item in payload["not_linked"]}
    assert reasons["hru_read.f90:32"] == "an inequality, not a search"


def test_an_index_without_comparisons_has_no_references(index) -> None:
    index.comparisons.clear()
    layouts = file_layouts(index)
    assert all(not column.references for layout in layouts.values()
               for record in layout.records for column in record.columns)


# ---------------------------------------- what the bundled facts say (62.0.0)

@pytest.fixture(scope="module")
def bundled() -> SourceIndex:
    return _load(None)


@pytest.fixture(scope="module")
def bundled_layouts(bundled):
    return file_layouts(bundled)


def _references(layouts, file: str, column: str) -> dict[str, list[str]]:
    main = layouts[file].main
    found = next(item for item in main.columns if item.name == column)
    return {f"{ref.file}:{ref.column}": ref.evidence for ref in found.references}


def test_bundled_hru_data_land_use_mgt_names_a_landuse_row(bundled_layouts) -> None:
    """The example the whole feature starts from."""
    assert _references(bundled_layouts, "hru-data.hru", "land_use_mgt") == {
        "landuse.lum:name": ["hru_read.f90:71"]}


def test_bundled_hru_data_links_every_pointer_column(bundled_layouts) -> None:
    targets = {
        column: sorted(_references(bundled_layouts, "hru-data.hru", column))
        for column in ("topo", "hyd", "soil", "soil_plant_init", "surf_stor",
                       "snow", "field")
    }
    assert targets == {
        "topo": ["topography.hyd:name"], "hyd": ["hydrology.hyd:name"],
        "soil": ["soils.sol:snam"], "soil_plant_init": ["soil_plant.ini:name"],
        "surf_stor": ["wetland.wet:name"], "snow": ["snow.sno:name"],
        "field": ["field.fld:name"],
    }


def test_bundled_decision_tables_are_targets(bundled) -> None:
    links, _ = input_links(bundled)
    targets = {link.target_file for link in links}
    assert {"lum.dtl", "flo_con.dtl", "res_rel.dtl", "scen_lu.dtl"} <= targets


def test_bundled_weather_stations_link_through_search(bundled) -> None:
    links, _ = input_links(bundled)
    link = next(item for item in links if item.source_file == "hru.con"
                and item.target_file == "weather-sta.cli")
    assert link.source_path == "ob%wst_c" and link.target_path == "wst%name"
    assert link.at == "hyd_read_connect.f90:338"
    assert link.through == ["search.f90:22", "cli_staread.f90:70"]
    stations = {item.target_file for item in links
                if item.source_file == "weather-sta.cli"}
    assert {"pcp.cli", "tmp.cli", "slr.cli", "hmd.cli", "wnd.cli",
            "weather-wgn.cli"} <= stations


def test_bundled_weather_links_target_layout_columns(bundled_layouts) -> None:
    pcp = next(ref for ref in next(
        column for column in bundled_layouts["weather-sta.cli"].main.columns
        if column.name == "pgage").references if ref.file == "pcp.cli")
    assert (pcp.column, pcp.path, pcp.role, pcp.position) == (
        "filename", "pcp%filename", "main", 1)


def test_bundled_references_have_existing_target_columns(bundled_layouts) -> None:
    targets = {
        (layout.file, record.role, position, column.path)
        for layout in bundled_layouts.values()
        for record in layout.records
        for position, column in enumerate(record.columns, start=1)
    }
    dangling = [
        (layout.file, column.path, ref.file, ref.role, ref.position, ref.path)
        for layout in bundled_layouts.values()
        for record in layout.records
        for column in record.columns
        for ref in column.references
        if (ref.file, ref.role, ref.position, ref.path) not in targets
    ]
    assert dangling == []


def test_bundled_uncalled_manure_reader_is_not_a_target(bundled) -> None:
    links, _ = input_links(bundled)
    management_targets = {
        item.target_file for item in links if item.source_file == "management.sch"
    }
    assert "manure_db.frt" in management_targets
    assert "manure.frt" not in management_targets


def test_bundled_management_operations_are_under_management_sch(bundled_layouts) -> None:
    layout = bundled_layouts["management.sch"]
    ops = next(record for record in layout.records if record.at == "read_mgtops.f90:27")
    assert ops.role == "child"
    op_char = next(column for column in ops.columns if column.name == "op_char")
    assert "tillage.til:tillnm" in {f"{ref.file}:{ref.column}" for ref in op_char.references}


def test_bundled_references_all_cite_file_and_line(bundled_layouts) -> None:
    references = [ref for layout in bundled_layouts.values()
                  for record in layout.records for column in record.columns
                  for ref in column.references]
    assert len(references) > 120
    for ref in references:
        assert ref.evidence
        for site in [*ref.evidence, *ref.through]:
            name, _, line = site.rpartition(":")
            assert name.endswith(".f90") and line.isdigit(), site


def test_cli_prints_links(tmp_path) -> None:
    out = tmp_path / "links.json"
    assert main(["--links", "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["links"] and payload["not_linked"]
    assert {"file", "path", "target_file", "target_path", "evidence"} <= set(payload["links"][0])


# ------------------------------------------------------ build (parser)

DB_MODULE = """\
      module db_module
        type topo_type
          character(len=40) :: name = ""
          real :: slope = 0.
        end type topo_type
        type hru_type
          character(len=40) :: name = ""
          character(len=40) :: topo = ""
        end type hru_type
        type sta_type
          character(len=40) :: name = ""
          character(len=40) :: wgn = ""
        end type sta_type
        type (topo_type), dimension(:), allocatable :: topo_db
        type (hru_type), dimension(:), allocatable :: hru_db
        type (hru_type) :: saved
        type (sta_type), dimension(:), allocatable :: wst
        character(len=40), dimension(:), allocatable :: wgn_n
      end module db_module
"""

TOPO_READ = """\
      subroutine topo_read
      use db_module
      character (len=80) :: titldum
      integer :: ith, eof
      open (107,file="topography.hyd")
      read (107,*,iostat=eof) titldum
      do ith = 1, 3
        read (107,*,iostat=eof) topo_db(ith)
      end do
      close (107)
      end subroutine topo_read
"""

HRU_READ = """\
      subroutine hru_read
      use db_module
      character (len=80) :: titldum
      integer :: i, ith, eof
      open (113,file="hru-data.hru")
      read (113,*,iostat=eof) titldum
      do i = 1, 3
        read (113,*,iostat=eof) hru_db(i)
        do ith = 1, 3
          if (trim(topo_db(ith)%name) .eq. trim(hru_db(i)%topo) .and. i > 0) then
            exit
          end if
        end do
      end do
      close (113)
      saved = hru_db(1)
      do ith = 1, 3
        if (saved%topo == topo_db(ith)%name) exit
      end do
      end subroutine hru_read
"""

STA_READ = """\
      subroutine sta_read
      use db_module
      character (len=80) :: titldum
      integer :: i, iwgn, eof
      open (114,file="weather-wgn.cli")
      read (114,*,iostat=eof) titldum
      do iwgn = 1, 3
        read (114,*,iostat=eof) wgn_n(iwgn)
      end do
      close (114)
      open (115,file="weather-sta.cli")
      read (115,*,iostat=eof) titldum
      do i = 1, 3
        read (115,*,iostat=eof) wst(i)
        call search (wgn_n, 3, wst(i)%wgn, iwgn)
      end do
      close (115)
      end subroutine sta_read
"""

SEARCH = """\
      subroutine search(sch, max, cfind, iseq)
      integer :: max, iseq, nn
      character(len=40), dimension(max) :: sch
      character(len=40) :: cfind
      nn = max / 2
      if (sch(nn) == cfind) then
        iseq = nn
      end if
      end subroutine search
"""


@requires_corpus
def test_build_keeps_comparisons_calls_and_copies(tmp_path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    for name, text in [("db_module.f90", DB_MODULE), ("topo_read.f90", TOPO_READ),
                       ("hru_read.f90", HRU_READ), ("sta_read.f90", STA_READ),
                       ("search.f90", SEARCH)]:
        (src / name).write_text(text, encoding="utf-8")
    idx = build_source_index(src, Path(CORPUS) if CORPUS else None)

    # `.eq.`, trim() and `.and.` reduce to one comparison of two variables;
    # `i > 0` is not an equality and is dropped.
    first = idx.comparisons_in("hru_read")[0]
    assert (first.line, first.left, first.op, first.right) == \
        (10, "topo_db(ith)%name", "==", "hru_db(i)%topo")
    assert first.loops == ("i", "ith")
    # The routine's own test on its dummies, and the same test restated at
    # the call with what the call passes.
    assert [(c.left, c.right) for c in idx.comparisons_in("search")] == [("sch(nn)", "cfind")]
    restated = next(c for c in idx.comparisons_in("sta_read") if c.via)
    assert (restated.line, restated.left, restated.right, restated.via) == \
        (15, "wgn_n(:)", "wst(i)%wgn", "search:6")
    # The copy that carries a column to the second comparison.
    assert [(c.procedure, c.line, c.source) for c in idx.copies_to("saved")] == \
        [("hru_read", 16, "hru_db(1)")]

    links = {(link.source_file, link.target_file, link.at): link.through
             for link in input_links(idx)[0]}
    assert links == {
        ("hru-data.hru", "topography.hyd", "hru_read.f90:10"): [],
        ("hru-data.hru", "topography.hyd", "hru_read.f90:18"): ["hru_read.f90:16"],
        ("weather-sta.cli", "weather-wgn.cli", "sta_read.f90:15"): ["search.f90:6"],
    }


@requires_real
def test_stored_loops_agree_with_scope_at_on_the_real_tree() -> None:
    """``Comparison.loops`` is stored, so it must not drift from the loops.

    Both come from the same facts at build time; this is the guard that they
    still say the same thing for every comparison in the tree.
    """
    idx = build_source_index(REAL_SOURCE, Path(CORPUS) if CORPUS else None)
    checked = 0
    for items in idx.comparisons.values():
        for item in items:
            proc = idx.procedure(item.procedure)
            scopes = idx.scope_at(proc.path, item.line)
            expected = None if scopes is None else tuple(
                scope.index or scope.header for scope in scopes)
            assert item.loops == expected, (item.procedure, item.line)
            checked += 1
    assert checked > 400
