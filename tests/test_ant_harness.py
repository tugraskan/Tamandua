"""Tests for scripts/test_ant_model.py -- the agentic loop against ANT.

Unit-testable without network access: httpx.MockTransport stands in for the
ANT endpoint, so these verify the schema conversion and the tool-call loop
without needing a live server. The live-endpoint run itself needs a machine on
the LAN and is documented in docs/ant_integration.md, not exercised here.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from test_ant_model import run_conversation, to_openai_tools  # noqa: E402

from tamandua.mcp.server import tool_specs


def test_schema_conversion_covers_every_tool() -> None:
    specs = tool_specs()
    tools = to_openai_tools(specs)
    assert len(tools) == len(specs)
    for tool in tools:
        assert tool["type"] == "function"
        assert tool["function"]["name"]
        assert tool["function"]["parameters"]["type"] == "object"


def _mock_client(*turns: dict) -> httpx.Client:
    """A model that emits the given messages in sequence, one per request."""
    calls = iter(turns)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": next(calls)}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


@dataclass
class _Use:
    procedure: str
    unit: str
    op: str
    line: int


class _FakeIndex:
    """Enough of SourceIndex for dispatch() to answer one known tool."""

    def io_for_file(self, file):
        if file != "aquifer.aqu":
            return []
        return [_Use(procedure="aqu_read", unit="107", op="open", line=30)]


def test_tool_call_is_dispatched_and_answer_returned(monkeypatch) -> None:
    """A model that calls a tool then answers: the loop dispatches the call
    against the real index and feeds the result back."""
    tool_call = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "file_io",
                                    "arguments": json.dumps({"file": "aquifer.aqu"})}}],
    }
    final = {"role": "assistant", "content": "aqu_read reads it on unit 107"}
    client = _mock_client(tool_call, final)

    answer, calls = run_conversation(
        client, "http://fake/v1/chat/completions", "test-model",
        "Which routine reads aquifer.aqu?", _FakeIndex(), to_openai_tools(tool_specs()),
        "system prompt", 2000,
    )
    assert calls == 1
    assert "aqu_read" in answer


def test_a_model_that_never_calls_a_tool_costs_zero_calls() -> None:
    """The bare-tools finding this harness exists to check for: availability
    is not use. A model may answer without touching the tools at all."""
    client = _mock_client({"role": "assistant", "content": "I think it's aqu_read"})
    answer, calls = run_conversation(
        client, "http://fake/v1/chat/completions", "test-model",
        "Which routine reads aquifer.aqu?", _FakeIndex(), to_openai_tools(tool_specs()),
        None, 2000,
    )
    assert calls == 0
    assert answer == "I think it's aqu_read"


def test_a_broken_tool_call_is_reported_back_not_raised() -> None:
    """A model can call a tool with bad arguments; the loop must recover
    rather than crash the whole run over one malformed call."""
    bad_call = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "nonexistent_tool", "arguments": "{}"}}],
    }
    final = {"role": "assistant", "content": "I could not find that tool"}
    client = _mock_client(bad_call, final)

    answer, calls = run_conversation(
        client, "http://fake/v1/chat/completions", "test-model",
        "question", _FakeIndex(), to_openai_tools(tool_specs()), None, 2000,
    )
    assert calls == 1
    assert "could not find" in answer


def test_giving_up_after_too_many_rounds_does_not_hang() -> None:
    """A model that never stops calling tools must not loop forever."""
    endless = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "file_io",
                                    "arguments": json.dumps({"file": "x"})}}],
    }
    client = _mock_client(*([endless] * 20))
    answer, calls = run_conversation(
        client, "http://fake/v1/chat/completions", "test-model",
        "question", _FakeIndex(), to_openai_tools(tool_specs()), None, 2000,
    )
    assert calls == 8  # the loop's round cap
    assert "gave up" in answer
