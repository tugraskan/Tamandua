"""A tiny fake MCP stdio server for unit-testing McpStdioClient without a
real dataselector process. Understands initialize, tools/list, and
tools/call for two fake tools: 'echo' and 'boom' (always errors).
"""
import json
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        msg_id = message.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "0.0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
                {"name": "echo", "description": "echoes its argument"},
            ]}})
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": f"echo: {args.get('value')}"}],
                }})
            elif name == "boom":
                send({"jsonrpc": "2.0", "id": msg_id, "error": {
                    "code": -32000, "message": "boom tool always fails",
                }})
            else:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": ""}], "isError": True,
                }})
        else:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32601, "message": f"unknown method {method}",
            }})


if __name__ == "__main__":
    main()
