"""Tests for deriving input-file column layouts from the facts.

Most build an index by hand, the way test_snapshot does, so they run with
neither a SWAT+ checkout nor the parser: layouts are computed from facts alone,
and that is the configuration a plain install has. The bundled-snapshot tests
pin what the shipped facts say about real files; one parser-gated test covers
the build-time half, following a filename passed in as an argument.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tamandua.index import (
    DerivedType,
    Field,
    IOUse,
    Loop,
    ModuleVariable,
    Procedure,
    Provenance,
    SourceIndex,
    Use,
    VariableDeclaration,
    build_source_index,
)
from tamandua.index.build import argument_filenames, call_arguments
from tamandua.index.layouts import (
    LAYOUT_FORMAT,
    declared_extents,
    file_layout,
    file_layouts,
    layouts_json,
)
from tamandua.index.layouts_cli import _load, main

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


# ------------------------------------------------------------------ fixture

def _local(name: str, declaration: str, line: int, vartype: str,
           units: str | None = None, description: str | None = None) -> VariableDeclaration:
    return VariableDeclaration(name=name, declaration=declaration, line=line,
                               vartype=vartype, initial=None, units=units,
                               description=description)


def _field(type_name: str, name: str, vartype: str, declaration: str,
           line: int, units: str | None = None,
           description: str | None = None) -> Field:
    return Field(type_name=type_name, name=name, vartype=vartype, units=units,
                 description=description, location=f"hru_module.f90:{line}",
                 declaration=declaration)


def _io(file: str, op: str, line: int, *fields: str, procedure: str = "hru_read",
        unit: str = "113") -> IOUse:
    return IOUse(file=file, op=op, unit=unit, procedure=procedure, line=line,
                 fields=tuple(fields))


@pytest.fixture
def index() -> SourceIndex:
    """hru_read's real shape: count pass, rewind, peek, backspace, read."""
    idx = SourceIndex(provenance=Provenance(
        source_path="/src/swatplus/src", source_commit="de210d6",
        source_describe="62.0.0", source_fingerprint="abc",
        generated_at="2026-09-29T00:00:00Z", format_version="4",
        parser_commit="110c2a2",
    ))
    idx.types["hru_databases_char"] = DerivedType(
        name="hru_databases_char", module="hru_module", location="hru_module.f90:158-166",
        fields=[
            _field("hru_databases_char", "name", "character(len=40)",
                   'character(len=40) :: name = ""', 159),
            _field("hru_databases_char", "topo", "character(len=40)",
                   'character(len=40) :: topo = ""', 160,
                   description="points to topography.hyd"),
        ])
    idx.types["hydrologic_response_unit_db"] = DerivedType(
        name="hydrologic_response_unit_db", module="hru_module",
        location="hru_module.f90:170-174",
        fields=[
            _field("hydrologic_response_unit_db", "name", "character(len=40)",
                   'character(len=40) :: name = "default"', 171),
            _field("hydrologic_response_unit_db", "dbsc", "type (hru_databases_char)",
                   "type (hru_databases_char) :: dbsc", 173),
        ])
    idx.types["cn_table"] = DerivedType(
        name="cn_table", module="hru_module", location="hru_module.f90:180-184",
        fields=[
            _field("cn_table", "name", "character(len=16)",
                   "character(len=16) :: name", 181),
            _field("cn_table", "cn", "real", "real, dimension(4) :: cn = 0.", 182,
                   units="none", description="curve number by hydrologic group"),
            _field("cn_table", "ly", "real", "real :: ly(mlyr)", 183),
            _field("cn_table", "conc", "real",
                   "real, dimension(:), allocatable :: conc", 184, units="mg/l"),
        ])
    idx.module_variables[("hru_module", "hru_db")] = ModuleVariable(
        name="hru_db", module="hru_module", vartype="type (hydrologic_response_unit_db)",
        declaration="type (hydrologic_response_unit_db), dimension(:), allocatable :: hru_db",
        line=175, units=None, description=None)
    idx.module_variables[("hru_module", "cntbl")] = ModuleVariable(
        name="cntbl", module="hru_module", vartype="type (cn_table)",
        declaration="type (cn_table), dimension(:), allocatable :: cntbl",
        line=190, units=None, description=None)
    idx.module_variables[("hru_module", "mlyr")] = ModuleVariable(
        name="mlyr", module="hru_module", vartype="integer",
        declaration="integer, parameter :: mlyr = 3", line=10,
        units=None, description=None, initial="3", is_parameter=True)
    # Declared in two modules: only a `use` can say which one a reader means.
    for module in ("hru_module", "salt_module"):
        idx.module_variables[(module, "twin")] = ModuleVariable(
            name="twin", module=module, vartype="real",
            declaration="real :: twin", line=20, units=None,
            description=f"the {module} one")

    idx.procedures["hru_read"] = Procedure(
        name="hru_read", module=None, location="hru_read.f90:1-120",
        path="hru_read.f90",
        uses=[Use(module="hru_module", only=(), line=3)],
        locals=[
            _local("titldum", "character (len=80) :: titldum", 12, "character (len=80)"),
            _local("header", "character (len=80) :: header", 13, "character (len=80)"),
            _local("k", "integer :: k", 14, "integer"),
            _local("i", "integer :: i", 15, "integer"),
            _local("nout", "integer :: nout", 16, "integer"),
            _local("frac", "real :: frac(nout)", 17, "real"),
        ],
    )
    idx.procedures["cn_read"] = Procedure(
        name="cn_read", module=None, location="cn_read.f90:1-60",
        path="cn_read.f90", uses=[Use(module="salt_module", only=("twin",), line=2)],
        locals=[_local("titldum", "character (len=80) :: titldum", 5, "character (len=80)"),
                _local("ic", "integer :: ic", 6, "integer"),
                _local("j", "integer :: j", 7, "integer")],
    )

    hru = "hru-data.hru"
    for use in [
        _io(hru, "open", 44),
        _io(hru, "read", 45, "titldum"),
        _io(hru, "read", 47, "header"),
        _io(hru, "read", 50, "i"),               # counting pass
        _io(hru, "rewind", 57),
        _io(hru, "read", 58, "titldum"),
        _io(hru, "read", 60, "header"),
        _io(hru, "read", 64, "i"),               # peek...
        _io(hru, "backspace", 66),
        _io(hru, "read", 67, "k", "hru_db(i)%dbsc"),  # ...then the record
        _io(hru, "read", 70, "(hru_db(i)%dbsc%topo, frac(j), j = 1, nout)"),
        _io(hru, "close", 90),
    ]:
        idx.io_by_file[hru].append(use)

    cn = "cntable.lum"
    for use in [
        _io(cn, "open", 10, procedure="cn_read", unit="107"),
        _io(cn, "read", 11, "titldum", procedure="cn_read", unit="107"),
        _io(cn, "read", 20, "cntbl(ic)%name", "cntbl(ic)%cn", "twin",
            procedure="cn_read", unit="107"),
        _io(cn, "read", 22, "cntbl(ic)%name", "cntbl(ic)%cn", "twin",
            "cntbl(ic)%ly", procedure="cn_read", unit="107"),
        _io(cn, "read", 26, "cntbl(ic)%conc", procedure="cn_read", unit="107"),
        _io(cn, "read", 30, "cntbl(ic)%name", procedure="cn_read", unit="107"),
    ]:
        idx.io_by_file[cn].append(use)
    idx.io_by_file["pcp(i)%filename"].append(
        _io("pcp(i)%filename", "read", 40, "titldum", "nobody", procedure="cn_read"))

    idx.loops["hru_read"] = [
        Loop(procedure="hru_read", line=43, header="do", end_line=100, index=None),
        Loop(procedure="hru_read", line=62, header="do ihru = 1, imax",
             end_line=80, index="ihru"),
        Loop(procedure="hru_read", line=69, header="do j = 1, 3", end_line=71, index="j"),
    ]
    idx.loops["cn_read"] = [
        Loop(procedure="cn_read", line=15, header="do ic = 1, n", end_line=40, index="ic"),
        Loop(procedure="cn_read", line=25, header="do j = 1, 2", end_line=27, index="j"),
    ]
    return idx


# ------------------------------------------------------------ record shape

def test_whole_structure_read_expands_components_in_declaration_order(index) -> None:
    layout = file_layout(index, "hru-data.hru")
    main = layout.main
    assert main.at == "hru_read.f90:67"
    assert [c.name for c in main.columns] == ["k", "name", "topo"]
    assert [c.path for c in main.columns] == ["k", "hru_db%dbsc%name", "hru_db%dbsc%topo"]
    assert main.complete


def test_every_column_cites_its_declaration(index) -> None:
    main = file_layout(index, "hru-data.hru").main
    assert main.columns[0].declared_at == "hru_read.f90:14"      # a local
    assert main.columns[2].declared_at == "hru_module.f90:160"   # a component
    assert main.columns[2].description == "points to topography.hyd"


def test_second_pass_after_rewind_is_the_layout_and_a_peek_is_not_a_record(index) -> None:
    layout = file_layout(index, "hru-data.hru")
    # The count pass before the rewind and the peek before the backspace are
    # both gone; the title and header of the second pass are the preamble.
    assert [line.at for line in layout.preamble] == ["hru_read.f90:58", "hru_read.f90:60"]
    assert [line.kind for line in layout.preamble] == ["text", "text"]
    assert [r.at for r in layout.records] == ["hru_read.f90:67", "hru_read.f90:70"]


def test_a_read_in_a_nested_loop_is_a_child_record(index) -> None:
    child = file_layout(index, "hru-data.hru").records[1]
    assert child.role == "child"
    assert child.loops == ["do", "ihru", "j"]


def test_a_leading_count_is_preamble_not_the_main_record(index) -> None:
    proc = index.procedures["hru_read"]
    proc.locals.extend([
        _local("count", "integer :: count", 92, "integer"),
        _local("value", "real :: value", 95, "real"),
    ])
    counted = "counted.dat"
    for use in [
        _io(counted, "read", 91, "titldum"),
        _io(counted, "read", 92, "count"),
        _io(counted, "read", 93, "header"),
        _io(counted, "read", 95, "k", "value"),
    ]:
        index.io_by_file[counted].append(use)
    index.loops["hru_read"].append(
        Loop(procedure="hru_read", line=94, header="do i = 1, count",
             end_line=96, index="i"))

    layout = file_layout(index, counted)
    assert [(line.kind, line.reads) for line in layout.preamble] == [
        ("text", ["titldum"]),
        ("values", ["count"]),
        ("text", ["header"]),
    ]
    assert layout.main.at == "hru_read.f90:95"
    assert [column.name for column in layout.main.columns] == ["k", "value"]


def test_implied_do_columns_repeat_by_their_count(index) -> None:
    child = file_layout(index, "hru-data.hru").records[1]
    assert [(c.name, c.repeat) for c in child.columns] == [("topo", "nout"), ("frac", "nout")]


def test_a_fixed_array_expands_to_one_column_per_element(index) -> None:
    main = file_layout(index, "cntable.lum").main
    names = [c.name for c in main.columns]
    assert names[:5] == ["name", "cn(1)", "cn(2)", "cn(3)", "cn(4)"]
    assert all(c.units == "none" for c in main.columns[1:5])


def test_a_parameter_sized_array_expands_by_the_parameter(index) -> None:
    main = file_layout(index, "cntable.lum").main
    assert [c.name for c in main.columns][-3:] == ["ly(1)", "ly(2)", "ly(3)"]


def test_an_allocatable_array_is_a_repeating_column_not_a_failure(index) -> None:
    child = next(r for r in file_layout(index, "cntable.lum").records if r.role == "child")
    assert [(c.name, c.repeat) for c in child.columns] == [("conc", "*")]
    assert child.complete


def test_a_non_constant_extent_repeats_by_the_extent_as_written(index) -> None:
    child = file_layout(index, "hru-data.hru").records[1]
    # `frac(j)` is subscripted inside the implied-do, so the do count wins.
    assert child.columns[1].repeat == "nout"


def test_the_same_record_read_two_ways_is_an_alternative(index) -> None:
    records = file_layout(index, "cntable.lum").records
    roles = {r.at: r.role for r in records}
    assert roles["cn_read.f90:22"] == "main"            # the longer reading
    assert roles["cn_read.f90:20"] == "alternative"     # begins the same way
    assert roles["cn_read.f90:30"] == "alternative"     # a prefix of both


def test_a_use_statement_picks_between_same_named_module_variables(index) -> None:
    main = file_layout(index, "cntable.lum").main
    twin = next(c for c in main.columns if c.name == "twin")
    assert twin.declared_at == "salt_module:20"
    assert twin.description == "the salt_module one"


def test_an_undeclared_name_leaves_the_record_incomplete_and_says_why() -> None:
    idx = SourceIndex(provenance=Provenance(
        source_path="/x", source_commit=None, source_describe=None,
        source_fingerprint="f", generated_at="t", format_version="4", parser_commit=None))
    idx.procedures["r"] = Procedure(name="r", module=None, location="r.f90:1-9", path="r.f90")
    idx.io_by_file["x.dat"].append(IOUse(file="x.dat", op="read", unit="9",
                                         procedure="r", line=3, fields=("ghost", "k")))
    record = file_layout(idx, "x.dat").main
    assert not record.complete
    assert "ghost" in record.unresolved[0]


def test_an_expression_filename_is_flagged_as_not_a_default(index) -> None:
    assert file_layout(index, "hru-data.hru").filename_is_default
    layout = file_layout(index, "pcp(i)%filename")
    assert layout is not None and not layout.filename_is_default


def test_a_file_nothing_reads_has_no_layout(index) -> None:
    assert file_layout(index, "nothing.txt") is None


def _named_blocks(read_name_line: int) -> SourceIndex:
    """pest_hru.ini's shape: title, header, then per block a name line."""
    idx = SourceIndex(provenance=Provenance(
        source_path="/x", source_commit=None, source_describe=None,
        source_fingerprint="f", generated_at="t", format_version="5", parser_commit=None))
    idx.types["ini"] = DerivedType(name="ini", module="m", location="m.f90:1", fields=[
        _field("ini", "name", "character(len=40)", "character(len=40) :: name", 2),
        _field("ini", "soil", "real", "real :: soil", 3)])
    idx.module_variables[("m", "pest_ini")] = ModuleVariable(
        name="pest_ini", module="m", vartype="type (ini)",
        declaration="type (ini), dimension(:), allocatable :: pest_ini",
        line=5, units=None, description=None)
    idx.procedures["pest_read"] = Procedure(
        name="pest_read", module=None, location="pest_read.f90:1-30",
        path="pest_read.f90",
        locals=[_local("titldum", "character (len=80) :: titldum", 3, "character (len=80)"),
                _local("header", "character (len=80) :: header", 4, "character (len=80)"),
                _local("ip", "integer :: ip", 5, "integer"),
                _local("j", "integer :: j", 6, "integer")])
    for use in [
        _io("pest_hru.ini", "open", 10, procedure="pest_read"),
        _io("pest_hru.ini", "read", 11, "titldum", procedure="pest_read"),
        _io("pest_hru.ini", "read", 12, "header", procedure="pest_read"),
        _io("pest_hru.ini", "read", read_name_line, "pest_ini(ip)%name",
            procedure="pest_read"),
        _io("pest_hru.ini", "read", 16, "titldum", "pest_ini(ip)%soil",
            procedure="pest_read"),
    ]:
        idx.io_by_file["pest_hru.ini"].append(use)
    idx.loops["pest_read"] = [
        Loop(procedure="pest_read", line=13, header="do ip = 1, n", end_line=18, index="ip"),
        Loop(procedure="pest_read", line=15, header="do j = 1, m", end_line=17, index="j"),
    ]
    return idx


def test_a_name_read_once_per_row_is_a_record_not_preamble() -> None:
    """``read pest_soil_ini(ipesti)%name`` inside ``do ipesti`` names each
    block. Taken for a title, it pushed ``data_starts_after`` one line too
    deep and left the name, which other files point at, out of the layout."""
    layout = file_layout(_named_blocks(14), "pest_hru.ini")
    assert len(layout.preamble) == 2
    assert [(r.role, r.at) for r in layout.records] == [
        ("main", "pest_read.f90:14"), ("child", "pest_read.f90:16")]
    assert layout.main.columns[0].path == "pest_ini%name"


def test_a_text_read_before_the_row_loop_is_still_preamble() -> None:
    # The same read outside the loop names nothing per row: a title.
    layout = file_layout(_named_blocks(12), "pest_hru.ini")
    assert [line.reads for line in layout.preamble][-1] == ["pest_ini(ip)%name"]


def test_a_reader_called_by_the_opener_reads_child_records(index) -> None:
    """read_mgtops reads each schedule's operations on the unit its caller,
    mgt_read_mgtops, has open. Its records are children of the caller's,
    not a wider "main" that demotes the schedule line."""
    index.procedures["cn_read"].callees.append("cn_ops")
    index.procedures["cn_ops"] = Procedure(
        name="cn_ops", module=None, location="cn_ops.f90:1-20", path="cn_ops.f90",
        locals=[_local("k", "integer :: k", 3, "integer"),
                _local("titldum", "character (len=80) :: titldum", 4, "character (len=80)")])
    index.io_by_file["cntable.lum"].append(
        _io("cntable.lum", "read", 12, "titldum", "k", "k", "k", "k", "k", "k", "k",
            "k", "k", "k", "k", procedure="cn_ops", unit="107"))
    layout = file_layout(index, "cntable.lum")
    assert layout.readers == ["cn_read", "cn_ops"]
    assert layout.main.at == "cn_read.f90:22"
    ops = next(r for r in layout.records if r.procedure == "cn_ops")
    assert ops.role == "child"


# ------------------------------------------------------------ declarations

@pytest.mark.parametrize("declaration, name, extents", [
    ("real, dimension(4) :: cn = 0.", "cn", ["4"]),
    ("real :: cn(4)", "cn", ["4"]),
    ("real :: a, cn(12, 3), b", "cn", ["12", "3"]),
    ("real, dimension(:), allocatable :: conc", "conc", [":"]),
    ("real :: cn = 0.", "cn", None),
    ("character(len=40) :: name = \"a(b)\"", "name", None),
    (None, "x", None),
])
def test_declared_extents(declaration, name, extents) -> None:
    assert declared_extents(declaration, name) == extents


# ------------------------------------------------------------ JSON and CLI

def test_json_carries_positions_provenance_and_format(index) -> None:
    payload = layouts_json(index)
    assert payload["layout_format"] == LAYOUT_FORMAT
    assert payload["provenance"]["parser_commit"] == "110c2a2"
    layout = payload["files"]["hru-data.hru"]
    assert layout["data_starts_after"] == 2
    main = layout["records"][0]
    assert main["role"] == "main" and main["complete"] is True
    assert [c["position"] for c in main["columns"]] == [1, 2, 3]
    assert "repeat" not in main["columns"][0]
    assert layout["records"][1]["columns"][0]["repeat"] == "nout"


def test_file_layouts_keys_on_lowercased_names(index) -> None:
    assert set(file_layouts(index)) == {"hru-data.hru", "cntable.lum", "pcp(i)%filename"}


def test_cli_writes_one_layout(tmp_path) -> None:
    out = tmp_path / "hru.json"
    assert main(["--file", "hru-data.hru", "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["file"] == "hru-data.hru"
    assert payload["records"][0]["columns"][0]["name"] == "k"


def test_cli_fails_for_a_file_nothing_reads() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--file", "no-such-file.txt"])
    assert exc.value.code == 1


# ---------------------------------------- what the bundled facts say (62.0.0)

@pytest.fixture(scope="module")
def bundled() -> SourceIndex:
    return _load(None)


def test_bundled_hru_data_layout_matches_hru_read(bundled) -> None:
    main = file_layout(bundled, "hru-data.hru").main
    assert main.at == "hru_read.f90:67"
    assert [c.name for c in main.columns] == [
        "k", "name", "topo", "hyd", "soil", "land_use_mgt",
        "soil_plant_init", "surf_stor", "snow", "field"]


def test_bundled_soils_have_a_layer_record_per_soil(bundled) -> None:
    layout = file_layout(bundled, "soils.sol")
    assert [(r.role, len(r.columns)) for r in layout.records] == [("main", 7), ("child", 14)]


def test_bundled_plants_are_read_two_ways(bundled) -> None:
    roles = sorted(r.role for r in file_layout(bundled, "plants.plt").records)
    assert roles == ["alternative", "main"]


def test_bundled_count_line_is_preamble_not_the_table_schema(bundled) -> None:
    layout = file_layout(bundled, "cal_parms.cal")
    assert [(line.kind, line.reads) for line in layout.preamble] == [
        ("text", ["titldum"]),
        ("values", ["mchg_par"]),
        ("text", ["header"]),
    ]
    assert layout.main.at == "cal_parm_read.f90:42"
    assert [column.name for column in layout.main.columns] == [
        "name", "ob_typ", "absmin", "absmax", "units"]


def test_bundled_connectivity_files_are_named_not_con_file(bundled) -> None:
    # Resolved through hyd_connect's calls to hyd_read_connect(in_con%...).
    assert file_layout(bundled, "con_file") is None
    for name in ("hru.con", "aquifer.con", "channel.con", "rout_unit.con"):
        layout = file_layout(bundled, name)
        assert layout is not None, name
        assert layout.readers == ["hyd_read_connect"]


def test_bundled_layouts_are_complete_where_a_layout_exists(bundled) -> None:
    layouts = file_layouts(bundled)
    assert len(layouts) > 240
    incomplete = [name for name, layout in layouts.items()
                  if layout.main is None or not layout.main.complete]
    assert incomplete == []


# ------------------------------------------------ build: argument filenames

def test_call_arguments_respects_parentheses_and_quotes() -> None:
    raw = 'call f (in_con%hru_con, "hru ,x", a(i, j), n=3)'
    assert call_arguments(raw, "f") == ["in_con%hru_con", '"hru ,x"', "a(i, j)", "n=3"]
    assert call_arguments("call g(1)", "f") is None


def test_argument_filenames_resolves_each_call_site() -> None:
    inputs = {"in_con%hru_con": "hru.con", "in_con%aqu_con": "aquifer.con"}
    sites = [
        'call hyd_read_connect(in_con%hru_con, "hru     ", 1)',
        'call hyd_read_connect(in_con%aqu_con, "aqu     ", 1)',
        'call hyd_read_connect(in_con%hru_con, "hru     ", 2)',   # repeat
        "call hyd_read_connect(obtyp='x', con_file='literal.con')",
        "call hyd_read_connect(other_dummy)",                      # an expression
    ]
    assert argument_filenames("hyd_read_connect", ["con_file", "obtyp"], "con_file",
                              sites, inputs) == [
        "hru.con", "aquifer.con", "literal.con", "other_dummy"]


def test_argument_filenames_is_empty_without_call_sites() -> None:
    assert argument_filenames("r", ["f"], "f", [], {}) == []
    assert argument_filenames("r", ["f"], "not_a_dummy", ["call r(x)"], {}) == []


READ_CONNECT = """\
      subroutine read_connect(con_file)
      character (len=20) :: con_file
      character (len=80) :: titldum
      integer :: k, eof
      open (107,file=con_file)
      read (107,*,iostat=eof) titldum
      do i = 1, 3
        read (107,*,iostat=eof) k
        backspace 107
        read (107,*,iostat=eof) k, titldum
      end do
      close (107)
      end subroutine read_connect
"""

CONNECT = """\
      subroutine connect
      use input_file_module
      call read_connect(in_con%hru_con)
      call read_connect("literal.con")
      end subroutine connect
"""

CON_MODULE = """\
      module input_file_module
        type input_con
          character(len=25) :: hru_con = "hru.con"
        end type input_con
        type (input_con) :: in_con
      end module input_file_module
"""


@requires_corpus
def test_build_follows_a_filename_argument_and_a_bare_backspace(tmp_path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "read_connect.f90").write_text(READ_CONNECT, encoding="utf-8")
    (src / "connect.f90").write_text(CONNECT, encoding="utf-8")
    (src / "input_file_module.f90").write_text(CON_MODULE, encoding="utf-8")
    idx = build_source_index(src, Path(CORPUS) if CORPUS else None)
    assert idx.io_for_file("con_file") == []
    for name in ("hru.con", "literal.con"):
        ops = [(u.op, u.line) for u in idx.io_for_file(name)]
        assert ("open", 5) in ops, name
        # `backspace 107`, without parentheses, is kept and on the right file.
        assert ("backspace", 9) in ops, name
    # So the peek at line 8 is not mistaken for a record.
    assert file_layout(idx, "hru.con").main.at == "read_connect.f90:10"
