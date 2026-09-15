"""Tests for saving and loading an index as JSON.

The round trip is the whole point: a snapshot is only worth publishing if what
comes back out answers questions identically to what went in. So these build an
index by hand -- covering the fields that are easy to lose in serialisation
(``None`` units, empty tuples, multi-hop call paths) -- rather than parsing,
which also lets them run with no SWAT+ checkout present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tamandua.index import (
    FACTS_NAME,
    INDEX_FORMAT_VERSION,
    RHS_NAME,
    SNAPSHOT_FORMAT,
    DerivedType,
    Field,
    IndexError_,
    IOUse,
    Loop,
    ModuleVariable,
    Procedure,
    Provenance,
    ScannerWarning,
    SelectCase,
    SourceIndex,
    Use,
    VariableDeclaration,
    WriterStatement,
    build_source_index,
    load_snapshot,
    save_rhs,
    save_snapshot,
)
from tamandua.index.snapshot import rhs_matches_snapshot, rhs_path_for

REAL_SOURCE = Path(os.environ.get("SWATPLUS_SOURCE", "/workspace/swatplus-62.0.0"))
CORPUS = os.environ.get("SWATPLUS_REFERENCE_CORPUS")


def _corpus_available() -> bool:
    try:
        import swatplus_reference  # noqa: F401
    except ImportError:
        return bool(CORPUS and (Path(CORPUS) / "src").is_dir())
    return True


requires_real = pytest.mark.skipif(
    not (REAL_SOURCE.is_dir() and _corpus_available()),
    reason="needs a SWAT+ checkout and swatplus-reference-corpus",
)


@pytest.fixture
def index() -> SourceIndex:
    """A small index exercising every section a snapshot has to carry."""
    idx = SourceIndex(provenance=Provenance(
        source_path="/src/swatplus",
        source_commit="de210d6",
        source_describe="62.0.0",
        source_fingerprint="abc123",
        generated_at="2026-08-26T00:00:00Z",
        format_version="1",
        parser_commit="41fc98e",
    ))
    idx.procedures["aqu_read"] = Procedure(
        name="aqu_read", module="aquifer_module",
        location="aquifer_module.f90:22", path="aquifer_module.f90",
        called_by=["command", "command"], callees=["allocate_parms"],
        uses=[Use(module="input_file_module", only=("in_aqu",), line=23)],
        arguments=[VariableDeclaration(
            name="frac", declaration="real, intent(in) :: frac", line=24,
            vartype="real", initial=None, units="-", description="fraction",
        )],
        locals=[VariableDeclaration(
            name="eof", declaration="integer :: eof = 0", line=25,
            vartype="integer", initial="0", units=None,
            description="end-of-file flag",
        )],
        select_cases=[SelectCase(
            subject="mode", cases=("1", "2", "default"), line=28,
        )],
    )
    # A procedure with nothing pointing at it in either direction: the empty
    # lists must survive as empty, not come back as None.
    idx.procedures["orphan"] = Procedure(
        name="orphan", module=None, location="misc.f90:3", path="misc.f90",
    )
    idx.io_by_file["aquifer.aqu"] = [
        IOUse(file="aquifer.aqu", op="read", unit="107",
              procedure="aqu_read", line=24, fields=("titldum", "eof")),
    ]
    idx.io_by_file["aquifer_day.txt"] = [
        # No unit, and no fields -- both are real cases in the corpus.
        IOUse(file="aquifer_day.txt", op="write", unit=None,
              procedure="aquifer_output", line=31),
    ]
    idx.io_by_unit["107"] = list(idx.io_by_file["aquifer.aqu"])
    idx.writers["aqu_d%rchrg"] = ["aquifer_output:31", "aqu_initial:12"]
    idx.writer_statements["aqu_d%rchrg"] = [
        WriterStatement(
            procedure="aquifer_output", line=31,
            raw="aqu_d(iaq)%rchrg = recharge + lateral_flow",
        ),
        WriterStatement(
            procedure="aqu_initial", line=12,
            raw="aqu_d(iaq)%rchrg = 0.",
        ),
    ]
    idx.loops["aqu_read"] = [
        Loop(procedure="aqu_read", line=26, header="do ish_aqp = 1, msh_aqp",
             end_line=29, index="ish_aqp"),
    ]
    idx.types["aquifer_dynamic"] = DerivedType(
        name="aquifer_dynamic", module="aquifer_module",
        location="aquifer_module.f90:8",
        fields=[
            Field(type_name="aquifer_dynamic", name="rchrg", vartype="real",
                  units="mm", description="recharge entering aquifer",
                  location="aquifer_module.f90:11"),
            # Undocumented field: units and description are both None.
            Field(type_name="aquifer_dynamic", name="flo", vartype="real",
                  units=None, description=None,
                  location="aquifer_module.f90:12"),
        ],
    )
    for item in (
        # The module-level instance of the derived type above -- the root of
        # the name a developer actually types, `aqu_d%rchrg`.
        ModuleVariable(
            name="aqu_d", module="aquifer_module",
            vartype="type (aquifer_dynamic)",
            declaration="type (aquifer_dynamic), dimension(:), allocatable :: aqu_d",
            line=18, units=None, description="aquifer state by object",
        ),
        # Undocumented, and carrying an initial value.
        ModuleVariable(
            name="msh_aqp", module="aquifer_module", vartype="integer",
            declaration="integer :: msh_aqp = 0", line=19,
            units=None, description=None, initial="0",
        ),
        # A compile-time constant: no runtime storage, so no object symbol.
        ModuleVariable(
            name="max_aqu", module="aquifer_module", vartype="integer",
            declaration="integer, parameter :: max_aqu = 1000", line=20,
            units=None, description="upper bound", initial="1000",
            is_parameter=True,
        ),
        # The collision, from the real tree: `hsaltb_d` is declared in both of
        # these modules, and a lookup keyed on the bare name loses one.
        ModuleVariable(
            name="hsaltb_d", module="salt_module", vartype="real",
            declaration="real, dimension(:) :: hsaltb_d", line=44,
            units="kg", description="salt balance by hru",
        ),
        ModuleVariable(
            name="hsaltb_d", module="output_ls_salt_module", vartype="real",
            declaration="real, dimension(:) :: hsaltb_d", line=61,
            units="kg", description="salt balance output",
        ),
    ):
        idx.module_variables[(item.module.lower(), item.name.lower())] = item
    idx.call_paths["aqu_read"] = [["main", "command", "aqu_read"]]
    idx.scanner_warnings.append(ScannerWarning(
        code="unclosed_block",
        message="if opened here is not closed",
        file="aquifer_module.f90",
        line=30,
        procedure="aqu_read",
    ))
    return idx


# ------------------------------------------------------------ round trip

def test_round_trip_preserves_every_query_answer(index, tmp_path):
    back = load_snapshot(save_snapshot(index, tmp_path / "snap.json"))

    assert back.provenance == index.provenance
    assert back.procedure("aqu_read").name == "aqu_read"
    assert back.procedure("aqu_read").module == "aquifer_module"
    assert back.procedure("aqu_read").path == "aquifer_module.f90"
    assert back.callers_of("aqu_read") == ["command"]      # deduplicated
    assert back.callees_of("aqu_read") == ["allocate_parms"]
    assert back.procedure("aqu_read").uses == index.procedure("aqu_read").uses
    assert back.procedure("aqu_read").arguments == index.procedure("aqu_read").arguments
    assert back.procedure("aqu_read").locals == index.procedure("aqu_read").locals
    assert back.procedure("aqu_read").select_cases == index.procedure("aqu_read").select_cases
    assert back.callers_of("orphan") == []
    assert back.callees_of("orphan") == []
    assert back.writers_of("aqu_d%rchrg") == ["aqu_initial:12", "aquifer_output:31"]
    assert back.paths_to("aqu_read") == [["main", "command", "aqu_read"]]
    assert [l.header for l in back.loops_in("aqu_read")] == ["do ish_aqp = 1, msh_aqp"]
    assert back.loops_in("aqu_read")[0].end_line == 29
    assert back.loops_in("aqu_read")[0].index == "ish_aqp"
    assert back.scope_at("aquifer_module.f90", 27)[0].index == "ish_aqp"
    assert back.scanner_warnings == index.scanner_warnings
    assert back.warnings_for_procedure("aqu_read") == index.scanner_warnings
    assert back.module_variables == index.module_variables
    assert back.module_variable("aquifer_module", "aqu_d").vartype == \
        "type (aquifer_dynamic)"
    # Both halves of the collision survive, and neither is chosen.
    assert [m.module for m in back.module_variables_named("hsaltb_d")] == \
        ["output_ls_salt_module", "salt_module"]
    assert back.module_variable("aquifer_module", "max_aqu").is_parameter is True
    assert back.module_variable("aquifer_module", "msh_aqp").initial == "0"
    assert back.module_variable("aquifer_module", "msh_aqp").description is None


def test_version_one_snapshot_loads_with_warning_defaults(index, tmp_path):
    path = save_snapshot(index, tmp_path / "snap.json")
    payload = json.loads(path.read_text())
    payload["snapshot_format"] = "1"
    payload.pop("scanner_warnings")
    payload.pop("unresolved_loop_files")
    payload["provenance"].pop("compile_status")
    for procedure in payload["procedures"]:
        for key in ("uses", "arguments", "locals", "select_cases"):
            procedure.pop(key)
    for loops in payload["loops"].values():
        for loop in loops:
            loop.pop("end_line")
            loop.pop("index")
    path.write_text(json.dumps(payload), encoding="utf-8")

    back = load_snapshot(path)
    assert back.provenance.compile_status == "not_checked"
    assert back.scanner_warnings == []
    assert back.procedure("aqu_read").uses == []
    assert back.loops_in("aqu_read")[0].end_line is None


def test_rhs_sidecar_round_trips_complete_assignment_statements(index, tmp_path):
    facts = save_snapshot(index, tmp_path / "swatplus-facts.json")
    save_rhs(index, tmp_path / "swatplus-rhs.json")

    back = load_snapshot(facts)
    assert back.writer_details("aqu_d%rchrg") == [
        {"at": "aqu_initial:12", "expression": "aqu_d(iaq)%rchrg = 0."},
        {"at": "aquifer_output:31",
         "expression": "aqu_d(iaq)%rchrg = recharge + lateral_flow"},
    ]


def test_missing_rhs_keeps_writer_response_shape(index, tmp_path):
    facts = save_snapshot(index, tmp_path / "swatplus-facts.json")
    back = load_snapshot(facts)
    assert back.writer_details("aqu_d%rchrg")[0]["expression"] == "unavailable"


def test_mismatched_rhs_is_rejected(index, tmp_path):
    facts = save_snapshot(index, tmp_path / "swatplus-facts.json")
    rhs = save_rhs(index, tmp_path / "swatplus-rhs.json")
    payload = json.loads(rhs.read_text(encoding="utf-8"))
    payload["source_fingerprint"] = "different-tree"
    rhs.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IndexError_, match="does not match the base facts"):
        load_snapshot(facts)


def test_round_trip_preserves_io_including_null_unit_and_empty_fields(index, tmp_path):
    back = load_snapshot(save_snapshot(index, tmp_path / "snap.json"))

    read = back.io_for_file("aquifer.aqu")
    assert len(read) == 1
    assert read[0].unit == "107"
    assert read[0].fields == ("titldum", "eof")
    assert isinstance(read[0].fields, tuple)

    write = back.io_for_file("aquifer_day.txt")
    assert write[0].unit is None
    assert write[0].fields == ()


def test_io_by_unit_is_rebuilt_not_stored(index, tmp_path):
    """It is derivable from io_by_file, so storing it would let the two drift."""
    path = save_snapshot(index, tmp_path / "snap.json")
    assert "io_by_unit" not in json.loads(path.read_text())

    back = load_snapshot(path)
    assert [u.procedure for u in back.io_for_unit("107")] == ["aqu_read"]
    assert back.io_for_unit("107", op="read") == back.io_for_unit("107")
    assert back.io_for_unit("107", op="write") == []


def test_round_trip_preserves_undocumented_fields(index, tmp_path):
    back = load_snapshot(save_snapshot(index, tmp_path / "snap.json"))

    documented, undocumented = back.derived_type("aquifer_dynamic").fields
    assert (documented.units, documented.description) == ("mm", "recharge entering aquifer")
    assert undocumented.units is None and undocumented.description is None
    assert undocumented.path == "aquifer_dynamic%flo"


def test_search_fields_works_after_a_round_trip(index, tmp_path):
    """The search path reads types; a lossy round trip would silently narrow it."""
    back = load_snapshot(save_snapshot(index, tmp_path / "snap.json"))
    assert [f.path for f in back.search_fields("recharge")] == ["aquifer_dynamic%rchrg"]
    assert [f.path for f in back.search_fields("rchrg")] == ["aquifer_dynamic%rchrg"]


# ------------------------------------------------------------- mechanics

def test_snapshot_is_byte_identical_across_rebuilds(index, tmp_path):
    """A release diff should show real changes, not key reordering."""
    first = save_snapshot(index, tmp_path / "a.json").read_bytes()
    second = save_snapshot(index, tmp_path / "b.json").read_bytes()
    assert first == second


def test_snapshot_creates_missing_parent_directories(index, tmp_path):
    written = save_snapshot(index, tmp_path / "dist" / "nested" / "snap.json")
    assert written.is_file()


def test_loading_a_future_format_names_the_fix(index, tmp_path):
    path = save_snapshot(index, tmp_path / "snap.json")
    payload = json.loads(path.read_text())
    payload["snapshot_format"] = "99"
    path.write_text(json.dumps(payload))

    with pytest.raises(IndexError_) as exc:
        load_snapshot(path)
    assert "99" in str(exc.value)
    assert "swatplus-build" in str(exc.value)


def test_a_format_two_snapshot_loads_without_the_new_section(index, tmp_path):
    """Formats 1 and 2 predate ``module_variables``, and still have to load.

    The section is read with a default rather than by key, so an older file
    comes back with it empty instead of raising. Everything else it does carry
    must still answer.
    """
    path = save_snapshot(index, tmp_path / "snap.json")
    payload = json.loads(path.read_text())
    payload["snapshot_format"] = "2"
    del payload["module_variables"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    back = load_snapshot(path)
    assert back.module_variables == {}
    assert back.module_variables_named("aqu_d") == []
    assert back.module_variable("aquifer_module", "aqu_d") is None
    # The rest of the snapshot is unaffected.
    assert back.procedure("aqu_read").name == "aqu_read"
    assert back.loops_in("aqu_read")[0].index == "ish_aqp"


def test_loading_a_missing_file_reports_the_path(tmp_path):
    with pytest.raises(IndexError_) as exc:
        load_snapshot(tmp_path / "absent.json")
    assert "absent.json" in str(exc.value)


def test_loading_malformed_json_says_so(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text("{not json")
    with pytest.raises(IndexError_) as exc:
        load_snapshot(path)
    assert "not valid JSON" in str(exc.value)


def test_loading_does_not_need_the_parser(index, tmp_path, monkeypatch):
    """Serving must not import swatplus_reference.

    Poisoning the import proves the load path never reaches for it, rather than
    happening to work because the parser was installed in the test environment.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("swatplus_reference"):
            raise AssertionError(f"load_snapshot must not import {name}")
        return real_import(name, *args, **kwargs)

    path = save_snapshot(index, tmp_path / "snap.json")
    monkeypatch.setattr(builtins, "__import__", refuse)
    assert load_snapshot(path).procedure("aqu_read") is not None


# ----------------------------------------------------------- real corpus

@requires_real
def test_real_index_round_trips(tmp_path):
    built = build_source_index(REAL_SOURCE, Path(CORPUS) if CORPUS else None)
    back = load_snapshot(save_snapshot(built, tmp_path / "snap.json"))

    assert len(back.procedures) == len(built.procedures)
    assert len(back.types) == len(built.types)
    assert back.writers.keys() == built.writers.keys()
    assert sum(len(v) for v in back.io_by_file.values()) == \
        sum(len(v) for v in built.io_by_file.values())
    for name in list(built.procedures)[:50]:
        assert back.callers_of(name) == built.callers_of(name)
        assert back.callees_of(name) == built.callees_of(name)


# ------------------------------------------------- the files we actually ship

def _bundled(name: str) -> Path:
    return Path(__file__).resolve().parent.parent / "tamandua" / "data" / name


def test_the_bundled_pair_is_shipped_together():
    """Both halves of the release snapshot are in the package.

    The facts file answers "where is this set"; the sidecar answers "set to
    what". Shipping only the first is not a failure -- ``load_snapshot`` treats
    the sidecar as optional-by-presence -- so every expression silently read
    ``unavailable`` on a plain install while the release asset answered fine.
    """
    assert _bundled(FACTS_NAME).is_file()
    assert _bundled(RHS_NAME).is_file(), (
        f"{RHS_NAME} is missing from tamandua/data/, so a plain install cannot "
        "answer what any assignment computes")


def test_the_bundled_sidecar_matches_the_bundled_facts():
    """A sidecar from a different build is worse than none.

    It is keyed on source fingerprint and parser commit, so a mismatched pair
    would attach one tree's expressions to another tree's line numbers.
    """
    facts = _bundled(FACTS_NAME)
    assert rhs_matches_snapshot(rhs_path_for(facts), facts), (
        "bundled swatplus-rhs.json does not match swatplus-facts.json; "
        "rebuild both with `swatplus-build` in one run")


def test_the_bundled_snapshot_answers_with_an_expression():
    """The end-to-end property the pair exists for."""
    index = load_snapshot(_bundled(FACTS_NAME))
    details = index.writer_details("db_mx%aqudb")
    assert details and details[0]["expression"] != "unavailable"


def test_the_bundled_snapshot_is_the_current_format():
    """A format-1 snapshot still loads, so staleness has to be asserted.

    The shipped file sat at format 1 through a format-2 release: it loaded
    without complaint while silently missing every procedure's arguments,
    locals and uses, and every loop's index and end line.
    """
    payload = json.loads(_bundled(FACTS_NAME).read_text(encoding="utf-8"))
    assert str(payload["snapshot_format"]) == SNAPSHOT_FORMAT, (
        "the bundled snapshot predates the current format; rebuild it with "
        "`swatplus-build` where a SWAT+ checkout and the parser are present")
    assert str(payload["index_format"]) == INDEX_FORMAT_VERSION


def test_the_bundled_snapshot_carries_module_variables():
    """The section added in format 3, asserted on the shipped file.

    Older formats stay readable and the section loads empty from them, which
    is exactly how a stale bundle goes unnoticed: every module-variable query
    would answer "not found" from a file that loads without complaint. That is
    the format-1 failure repeated, so it gets its own assertion rather than
    relying on the version number alone.
    """
    index = load_snapshot(_bundled(FACTS_NAME))
    assert index.module_variables, (
        "the bundled snapshot carries no module variables, so every "
        "module_variable query answers 'not found'; rebuild it with "
        "`swatplus-build`")
    # A state object SWAT+ declares at module level, not a type component.
    assert index.module_variables_named("aqu_d"), \
        "aqu_d is not indexed as a module variable"
