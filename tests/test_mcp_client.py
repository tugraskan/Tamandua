"""Tests for the generic MCP stdio client.

Carried over from the dataset client's suite when the client itself was kept:
it is how this repo talks *to* another MCP server (dataselector, or our own in
a harness), so it outlives the chatbot it was first written for.

Everything here runs against ``fixtures/fake_mcp_server.py`` -- a real
subprocess speaking real JSON-RPC, so the framing is exercised rather than
mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tamandua.mcp.client import McpError, McpStdioClient

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


@pytest.fixture
def fake_client():
    with McpStdioClient([sys.executable, str(FAKE_SERVER)]) as client:
        yield client


def test_initialize_and_list_tools(fake_client):
    tools = fake_client.list_tools()
    assert [t["name"] for t in tools] == ["echo"]


def test_call_tool_returns_text(fake_client):
    result = fake_client.call_tool("echo", {"value": "hello"})
    assert result.text == "echo: hello"
    assert not result.is_error


def test_call_tool_error_result_is_flagged_not_raised(fake_client):
    """A tool-level error (isError: true) is data, not a transport failure --
    the caller decides what to do with it."""
    result = fake_client.call_tool("nonexistent_tool", {})
    assert result.is_error


def test_jsonrpc_error_raises_mcp_error(fake_client):
    with pytest.raises(McpError, match="boom tool always fails"):
        fake_client.call_tool("boom", {})


def test_client_not_started_raises():
    client = McpStdioClient([sys.executable, str(FAKE_SERVER)])
    with pytest.raises(McpError, match="not started"):
        client.list_tools()


def test_context_manager_closes_process():
    client = McpStdioClient([sys.executable, str(FAKE_SERVER)])
    with client:
        assert client._process is not None
    assert client._process is None
