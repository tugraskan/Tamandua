"""Tests for the read-only MCP server.

Every byte a tool returns lands in a model's context, so its output must be as
tight as the rendered index's. These guard the two ways a compact row can
quietly cost more than it should -- a sequence rendered as a Python repr, and
a column silently dropped when later rows introduce keys the first row lacked.
"""

from __future__ import annotations

import json

from tamandua.mcp.server import (
    handle,
    load_bundled_snapshot,
    render_compact,
    t_file_io,
    t_find_procedure,
    t_loops,
    t_writers,
    tool_specs,
)
from tamandua.index import (
    Loop,
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
