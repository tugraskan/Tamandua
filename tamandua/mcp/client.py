"""Minimal MCP stdio client.

A small, synchronous JSON-RPC-over-stdio client — just enough to speak MCP to
one server process for the duration of a CLI invocation. This is deliberately
generic (not dataselector-specific): MCP is one transport behind the dataset
client interface (R-6), so this module has no SWAT+-specific knowledge.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from typing import Any


class McpError(RuntimeError):
    """The server returned a JSON-RPC error, or the process misbehaved."""


@dataclass(frozen=True)
class ToolResult:
    """The text content of an MCP ``tools/call`` response."""

    text: str
    is_error: bool = False


class McpStdioClient:
    """Speaks MCP over a subprocess's stdin/stdout.

    Use as a context manager so the subprocess is always terminated:

        with McpStdioClient(["node", "server.js", "--index", path]) as client:
            client.list_tools()
            client.call_tool("lookup_docs", {"file": "aquifer.aqu"})
    """

    def __init__(self, command: list[str], *, timeout: float = 30.0) -> None:
        self._command = command
        self._timeout = timeout
        self._process: subprocess.Popen | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def __enter__(self) -> McpStdioClient:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def start(self) -> None:
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "swatplus-assistant", "version": "0.1"},
            },
        )
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        if self._process is None:
            return
        try:
            self._process.stdin.close()
            self._process.wait(timeout=5)
        except Exception:
            self._process.kill()
        finally:
            self._process = None

    # --- MCP calls ----------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content") or []
        text = "\n".join(
            item.get("text", "") for item in content if item.get("type") == "text"
        )
        return ToolResult(text=text, is_error=bool(result.get("isError")))

    # --- JSON-RPC plumbing ----------------------------------------------

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._process is None:
            raise McpError("client is not started")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                        "params": params})
            message = self._read_matching(request_id)
        if "error" in message:
            error = message["error"]
            raise McpError(f"{method} failed: {error.get('message', error)}")
        return message.get("result", {})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _read_matching(self, request_id: int) -> dict[str, Any]:
        """Read lines until the response with this request's id arrives.

        Notifications and out-of-order server logs on stdout are skipped
        rather than treated as errors.
        """
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = self._process.stdout.readline()
            if line == "":
                stderr = self._process.stderr.read() if self._process.stderr else ""
                raise McpError(
                    f"server exited before responding to request {request_id}"
                    + (f": {stderr}" if stderr else "")
                )
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # a non-JSON log line on stdout; ignore
            if message.get("id") == request_id:
                return message
