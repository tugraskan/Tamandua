"""Tests for the read-only MCP server.

Every byte a tool returns lands in a model's context, so its output must be as
tight as the rendered index's. These guard the two ways a compact row can
quietly cost more than it should -- a sequence rendered as a Python repr, and
a column silently dropped when later rows introduce keys the first row lacked.
"""

from __future__ import annotations

import json
from pathlib import Path

from tamandua.mcp import server as _server_module

from tamandua.mcp.server import (
    INVALID_PARAMS,
    Current,
    accepted_arguments,
    auto_source,
    handle,
    load_bundled_snapshot,
    render_compact,
    t_file_io,
    t_find_procedure,
    t_loops,
    t_module_variable,
    t_writers,
    tool_specs,
)
from tamandua.index import (
    IndexError_,
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
)


def test_sequences_are_joined_not_repred() -> None:
    """`str(("a","b"))` emits a Python tuple, quotes and all -- noise the
    caller pays for on every row."""
    rows = [{"procedure": "aquifer_output", "fields": ("time%day", "iaq")}]
    text = render_compact(rows)
    assert "time%day,iaq" in text
    assert "(" not in text and "'" not in text


def test_unit_carries_its_name() -> None:
    """Unit and line are otherwise two bare numbers in adjacent columns. That
    exact ambiguity cost eight tool calls when the rendered index had it."""
    rows = [{"file": "aquifer_day.txt", "unit": "2520", "line": 22}]
    assert "unit=2520" in render_compact(rows)


def test_absent_unit_is_not_labelled() -> None:
    assert "unit=" not in render_compact([{"file": "x", "unit": None, "line": 1}])


def test_compact_row_has_a_header() -> None:
    """A tool result arrives without the schema, so the keys ride along."""
    text = render_compact([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    assert text.splitlines()[0] == "a|b"


def test_every_tool_declares_a_schema() -> None:
    for spec in tool_specs():
        assert spec["name"] and spec["description"]
        assert spec["inputSchema"]["type"] == "object"


def test_notifications_get_no_response() -> None:
    """A JSON-RPC notification has no id and must not be answered."""
    assert handle(None, {"jsonrpc": "2.0", "method": "notifications/initialized"}, False) is None


def test_initialize_reports_the_protocol_version() -> None:
    reply = handle(None, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, False)
    assert reply["result"]["protocolVersion"]
    assert reply["result"]["serverInfo"]["name"] == "swatplus-source"
    assert "file_io first" in reply["result"]["instructions"]


def test_bundled_snapshot_answers_the_archetype_question() -> None:
    rows = t_file_io(load_bundled_snapshot(), "aquifer.aqu")
    assert any(row["procedure"] == "aqu_read" for row in rows)


def test_unknown_method_still_answers() -> None:
    """A client that asks for something unsupported must not hang."""
    reply = handle(None, {"jsonrpc": "2.0", "id": 9, "method": "resources/list"}, False)
    assert reply["id"] == 9
    assert "result" in reply


def test_tools_list_is_json_serialisable() -> None:
    json.dumps(tool_specs())


# ---------------------------------------------- self-describing results

def test_no_cell_is_ever_blank() -> None:
    """A blank cell reads as a broken response rather than an absent value.

    `find_procedure` emitted `...|module=` for a module-less procedure, which a
    real session reported as the tool having failed.
    """
    text = render_compact([{"name": "aqu_read", "module": None, "other": ""}])
    for cell in text.splitlines()[1].split("|"):
        assert cell, "empty cell in a compact row"
    assert "none" in text


def test_empty_sequence_says_none() -> None:
    assert "none" in render_compact([{"callers": []}])


def test_scalar_results_echo_the_question() -> None:
    """A bare word is unreadable without context.

    `callers` returned `command` -- correct, and a real session read it as an
    empty response and fell back to grep.
    """
    from tamandua.mcp.server import t_callers

    class _Index:
        def callers_of(self, name):
            return ["command"]

    text = render_compact(t_callers(_Index(), "hru_control"))
    assert "procedure=hru_control" in text
    assert "callers=command" in text


def test_new_tools_are_declared() -> None:
    """Four capabilities the index file does not expose and dataselector does
    not cover: words to identifier, a type's contents, how execution arrives,
    and a run's numbers."""
    names = {spec["name"] for spec in tool_specs()}
    assert {"search_fields", "describe_type", "call_path", "read_output"} <= names


def test_read_output_reports_a_bad_file_rather_than_raising() -> None:
    """A tool that throws leaves the client with nothing to act on."""
    from tamandua.mcp.server import t_read_output

    class _Index:
        class provenance:
            source_path = "/nonexistent/src"

    result = t_read_output(_Index(), file="absent.txt", column="x")
    assert "error" in result


def test_rows_with_different_keys_keep_every_column() -> None:
    """Taking the header from the first row drops columns later rows add.

    `breakpoint` returns a condition only for stops inside a loop, and those
    conditions vanished entirely until the header became a union.
    """
    text = render_compact([{"a": 1}, {"a": 2, "b": 3}])
    assert text.splitlines()[0] == "a|b"
    assert text.splitlines()[1].endswith("|none")
    assert text.splitlines()[2] == "2|3"


def test_debugging_tools_are_declared() -> None:
    names = {spec["name"] for spec in tool_specs()}
    assert {"breakpoint", "scope_at"} <= names


def test_existing_tools_surface_the_widened_procedure_and_loop_facts() -> None:
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit=None, source_describe=None,
        source_fingerprint="abc", generated_at="2026-08-28T00:00:00Z",
        format_version="2", parser_commit=None,
    ))
    index.procedures["dispatch"] = Procedure(
        name="dispatch", module="routing", location="routing.f90:10-30",
        path="routing.f90",
        uses=[Use(module="state", only=("water",), line=11)],
        arguments=[VariableDeclaration(
            name="frac", declaration="real :: frac", line=12,
            vartype="real", initial=None, units="-", description="fraction",
        )],
        locals=[],
        select_cases=[SelectCase(subject="mode", cases=("1", "default"), line=15)],
    )
    index.loops["dispatch"] = [Loop(
        procedure="dispatch", line=20, end_line=24, index="i", header="do i = 1, n",
    )]

    procedure = t_find_procedure(index, "dispatch")
    assert procedure["uses"][0]["only"] == ("water",)
    assert procedure["arguments"][0]["line"] == 12
    assert procedure["select_cases"][0]["cases"] == ("1", "default")
    assert t_loops(index, "dispatch")[0]["end_line"] == 24


def test_writers_returns_the_complete_sidecar_expression() -> None:
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit=None, source_describe=None,
        source_fingerprint="abc", generated_at="2026-08-28T00:00:00Z",
        format_version="2", parser_commit=None,
    ))
    index.writers["state%water"] = ["route:18"]
    index.writer_statements["state%water"] = [WriterStatement(
        procedure="route", line=18,
        raw="state(i)%water = inflow + recharge",
    )]
    assert t_writers(index, "state%water")["assignments"] == [{
        "at": "route:18", "expression": "state(i)%water = inflow + recharge",
    }]


def test_scope_at_rejects_a_non_numeric_line() -> None:
    from tamandua.mcp.server import t_scope_at

    class _Index:
        def procedure(self, name):
            class _P:
                class location:
                    path = "x.f90"
            return _P()

    assert "error" in t_scope_at(_Index(), procedure="hru_control", line="abc")


def test_relevant_scanner_warning_rides_with_tool_answer() -> None:
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit=None, source_describe=None,
        source_fingerprint="abc", generated_at="2026-08-28T00:00:00Z",
        format_version="2", parser_commit=None,
    ))
    index.procedures["broken"] = Procedure(
        name="broken", module=None, location="broken.f90:1-4", path="broken.f90",
    )
    index.scanner_warnings.append(ScannerWarning(
        code="unclosed_block", message="if opened here is not closed",
        file="broken.f90", line=2, procedure="broken",
    ))

    reply = handle(index, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "find_procedure", "arguments": {"name": "broken"}},
    }, False)

    content = reply["result"]["content"]
    assert len(content) == 2
    assert "unclosed_block" in content[1]["text"]
    assert "broken.f90:2" in content[1]["text"]


def test_healthy_answer_does_not_pay_for_empty_warning_block() -> None:
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit=None, source_describe=None,
        source_fingerprint="abc", generated_at="2026-08-28T00:00:00Z",
        format_version="2", parser_commit=None,
    ))
    index.procedures["healthy"] = Procedure(
        name="healthy", module=None, location="healthy.f90:1-2", path="healthy.f90",
    )
    reply = handle(index, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "find_procedure", "arguments": {"name": "healthy"}},
    }, False)
    assert len(reply["result"]["content"]) == 1


# ------------------------------------------- module-level variables


def _salt_index() -> SourceIndex:
    """An index carrying the real `hsaltb_d` collision."""
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit=None, source_describe=None,
        source_fingerprint="abc", generated_at="2026-09-15T00:00:00Z",
        format_version="3", parser_commit=None,
    ))
    for item in (
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
        ModuleVariable(
            name="max_aqu", module="aquifer_module", vartype="integer",
            declaration="integer, parameter :: max_aqu = 1000", line=20,
            units=None, description=None, initial="1000", is_parameter=True,
        ),
    ):
        index.module_variables[(item.module.lower(), item.name.lower())] = item
    return index


def test_an_ambiguous_name_returns_every_candidate_not_a_winner() -> None:
    """The answer the nm-generated symbol map gets wrong.

    It keys on the bare name and resolves duplicates by sorting the mangled
    symbols, so the winner is whichever module name sorts first -- unrelated
    to the scope the question came from. A tool that picked one would repeat
    exactly that, silently.
    """
    answer = t_module_variable(_salt_index(), "hsaltb_d")
    assert answer["declared_in"] == ["output_ls_salt_module", "salt_module"]
    assert "ambiguous" in answer
    assert len(answer["candidates"]) == 2


def test_a_module_qualifies_an_ambiguous_name() -> None:
    answer = t_module_variable(_salt_index(), "hsaltb_d", "salt_module")
    assert answer["module"] == "salt_module"
    assert answer["declared_at"] == "salt_module:44"
    assert answer["units"] == "kg"


def test_a_parameter_says_it_has_no_object_symbol() -> None:
    """Four SWAT+ declarations are compile-time constants. A caller building a
    debugger symbol map has to exclude them, so the answer says so."""
    answer = t_module_variable(_salt_index(), "max_aqu")
    assert "no object symbol" in answer["parameter"]
    assert answer["initial"] == "1000"


def test_an_unknown_name_falls_back_to_a_search() -> None:
    answer = t_module_variable(_salt_index(), "salt balance")
    assert answer["exact"] == "none"
    assert {row["module"] for row in answer["similar"]} == {
        "salt_module", "output_ls_salt_module"}
    assert t_module_variable(_salt_index(), "nothing_here")["found"] == "no"


def test_every_tool_spec_is_well_formed() -> None:
    """A malformed schema makes a tool unusable without failing loudly."""
    for spec in tool_specs():
        assert spec["name"] and spec["description"]
        schema = spec["inputSchema"]
        assert schema["type"] == "object"
        for required in schema.get("required", []):
            assert required in schema["properties"], (
                f"{spec['name']} requires {required}, which it does not declare")
# ------------------------------------------------- a bad call must not be fatal

def _bad_call_index() -> SourceIndex:
    index = SourceIndex(provenance=Provenance(
        source_path="/src", source_commit=None, source_describe=None,
        source_fingerprint="abc", generated_at="2026-08-28T00:00:00Z",
        format_version="2", parser_commit=None,
    ))
    index.procedures["aqu_read"] = Procedure(
        name="aqu_read", module=None, location="aqu_read.f90:1-67",
        path="aqu_read.f90",
    )
    return index


def _call(index, name, arguments):
    return handle(index, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}},
                  compact=True)


def test_a_wrong_argument_name_returns_an_error_instead_of_raising() -> None:
    """dispatch() splats caller arguments straight into the tool.

    One wrong keyword -- `query` where search_fields takes `text`, which a
    client guesses on its first try -- raised TypeError out through serve() and
    killed the process. The client saw its tools vanish mid-session, with no
    error and nothing in the transcript explaining it.
    """
    index = _bad_call_index()
    response = _call(index, "search_fields", {"query": "lateral flow"})

    assert "error" in response, "a bad argument name must not propagate"
    assert response["error"]["code"] == INVALID_PARAMS


def test_the_error_names_the_arguments_the_tool_accepts() -> None:
    """Naming the fix is what turns a dead session into a corrected retry."""
    index = _bad_call_index()
    message = _call(index, "search_fields", {"query": "x"})["error"]["message"]

    assert "query" in message
    assert "search_fields accepts: text" in message


def test_an_unknown_tool_is_an_error_not_an_exit() -> None:
    index = _bad_call_index()
    response = _call(index, "no_such_tool", {})

    assert response["error"]["code"] == INVALID_PARAMS
    assert "no_such_tool" in response["error"]["message"]


def test_the_server_still_answers_after_a_bad_call() -> None:
    """The point of the fix: one malformed call costs one call, not the session."""
    index = _bad_call_index()
    _call(index, "search_fields", {"query": "x"})

    good = _call(index, "find_procedure", {"name": "aqu_read"})
    assert "error" not in good
    assert "aqu_read" in good["result"]["content"][0]["text"]


def test_accepted_arguments_reads_the_published_schema() -> None:
    assert accepted_arguments("search_fields") == ["text"]
    assert accepted_arguments("find_procedure") == ["name"]
    assert accepted_arguments("nope") == []


# ------------------------------------------- which tree is this server reading

def _index_at(path: str, commit: str | None, describe: str | None = None) -> SourceIndex:
    return SourceIndex(provenance=Provenance(
        source_path=path, source_commit=commit, source_describe=describe,
        source_fingerprint="abc", generated_at="2026-09-16T00:00:00Z",
        format_version="3", parser_commit=None,
    ))


def test_live_source_note_names_the_tree_and_commit() -> None:
    """The one fact a caller needs before quoting a file and line.

    A server aimed at the wrong checkout answers confidently, correctly, and
    about code the caller is not looking at. Nothing in a tool result gives
    that away, so it has to be said up front.
    """
    note = Current(_index_at("/repo/src", "de210d64db4f1d75"), source=Path("/repo/src")).source_note

    assert "/repo/src" in note
    assert "de210d64db4f" in note


def test_bundled_source_note_does_not_read_as_a_working_tree() -> None:
    """Its provenance path names the build machine, which invites the mistake."""
    note = Current(_index_at("/someone/elses/machine/src", "de210d6", "62.0.0")).source_note

    assert "bundled" in note
    assert "62.0.0" in note
    assert "working tree" in note


def test_a_fallback_says_why_it_fell_back() -> None:
    """Silently serving the bundle is the failure this whole change is about."""
    note = Current(
        _index_at("/x", "abc", "62.0.0"),
        fallback_reason="The working directory /repo is a SWAT+ checkout, but "
                        "it could not be indexed (no parser).",
    ).source_note

    assert "could not be indexed" in note
    assert "/repo" in note


def test_initialize_carries_the_source_note() -> None:
    """Sent once per session, so the warning is free at the scale that matters."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

    result = handle(_index_at("/repo/src", "abc"), request, True,
                    "Source: /repo/src at commit abc")["result"]

    assert "Source: /repo/src at commit abc" in result["instructions"]
    # the standing guidance survives alongside it
    assert "facts-only tools" in result["instructions"]


def test_initialize_without_a_note_is_unchanged() -> None:
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

    result = handle(_index_at("/repo/src", "abc"), request, True)["result"]

    assert "Source:" not in result["instructions"]


# ------------------------------------------------- choosing without a config

def test_auto_source_reads_the_working_directory_when_it_is_a_checkout(
    tmp_path, monkeypatch
) -> None:
    """An editor starts the server inside the project it has open."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "demo.f90").write_text("      subroutine demo\n      end subroutine demo\n")
    monkeypatch.chdir(tmp_path)
    built = _index_at(str(src), "abc")
    monkeypatch.setattr(_server_module, "build_source_index", lambda *a, **k: built)

    current = auto_source()

    assert str(src) in current.source_note
    assert "bundled" not in current.source_note


def test_auto_source_uses_the_bundle_when_there_is_no_project(
    tmp_path, monkeypatch
) -> None:
    """A desktop chat app has no project and starts somewhere neutral."""
    monkeypatch.chdir(tmp_path)

    assert "bundled" in auto_source().source_note


def test_auto_source_falls_back_loudly_when_the_tree_cannot_be_indexed(
    tmp_path, monkeypatch
) -> None:
    """Better the bundled release than a dead server -- but say so."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "demo.f90").write_text("      subroutine demo\n      end subroutine demo\n")
    monkeypatch.chdir(tmp_path)

    def refuse(*_a, **_k):
        raise IndexError_("cannot find swatplus-reference-corpus")

    monkeypatch.setattr(_server_module, "build_source_index", refuse)

    note = auto_source().source_note
    assert "bundled" in note
    assert "could not be indexed" in note
    assert "cannot find swatplus-reference-corpus" in note
