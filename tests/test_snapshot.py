"""Tests for saving and loading an index as JSON (docs/decisions.md D-8).

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
    SNAPSHOT_FORMAT,
    DerivedType,
    Field,
    IndexError_,
    IOUse,
    Loop,
    Procedure,
    Provenance,
    SourceIndex,
    build_source_index,
    load_snapshot,
    save_snapshot,
)

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
    idx.loops["aqu_read"] = [
        Loop(procedure="aqu_read", line=26, header="do ish_aqp = 1, msh_aqp"),
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
    idx.call_paths["aqu_read"] = [["main", "command", "aqu_read"]]
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
    assert back.callers_of("orphan") == []
    assert back.callees_of("orphan") == []
    assert back.writers_of("aqu_d%rchrg") == ["aqu_initial:12", "aquifer_output:31"]
    assert back.paths_to("aqu_read") == [["main", "command", "aqu_read"]]
    assert [l.header for l in back.loops_in("aqu_read")] == ["do ish_aqp = 1, msh_aqp"]


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
    """The point of D-8: serving must not import swatplus_reference.

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
