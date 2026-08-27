#!/usr/bin/env python3
"""Run the four-question test against a real ANT model, agentically.

Every arm measured so far used Claude Code -- the model least likely to need
these tools, since composing a correct grep pattern is itself a skill a small
local model may not have. This is the untested case: does a local model
reached through ANT reach for `swatplus-source`'s tools at all, with or
without an instruction to prefer them.

This talks to ANT's OpenAI-compatible endpoints directly (workstation guide
section 7), not through the MCP protocol -- there is no MCP client library
dependency this way, and it drives the same tool functions
`tamandua/mcp/server.py` exposes over stdio, so a tool call the model
emits is answered from the same index, not a re-implementation.

Requires network access to ANT (this only runs on a machine that has it):

    python scripts/test_ant_model.py \\
        --url http://172.20.1.78:8000/v1/chat/completions --model laguna-s \\
        --key "$env:ANT_API_KEY" \\
        --source C:\\path\\to\\swatplus-main --corpus C:\\path\\to\\swatplus-reference-corpus

    # Repeat with --pointer docs/pointers/mcp_claude_md.md for the fair-fight arm.

Per the workstation guide's four rules: max_tokens defaults generously (2000),
temperature is left at the model's own default (never forced to 0), and this
refuses to run against maverick / nemotron-3-super / nemotron-3-ultra (port
8040), where tool calling is documented as broken.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tamandua.index import IndexError_, build_source_index  # noqa: E402
from tamandua.mcp.server import dispatch, tool_specs  # noqa: E402

#: Documented in the workstation guide as broken for tool calling.
_NO_TOOL_CALLING_PORTS = ("8040",)

QUESTIONS = [
    ("Which routine reads aquifer.aqu?", ["aqu_read"]),
    ("What calls hru_control?", ["command"]),
    ("Which routine writes aquifer_day.txt?", ["aquifer_output"]),
    ("What loops are in hru_control and what are their index variables?",
     ["j1", "ipest", "iauto", "ipl", "iout", "isalt", "ics"]),
]

DEFAULT_INSTRUCTION = (
    "You have tools from a 'swatplus-source' server that index this SWAT+ "
    "Fortran checkout via static analysis. Prefer them over reading source "
    "files yourself -- they cover the whole tree, so they do not miss "
    "occurrences the way a manual read can. In particular, for questions "
    "about loops, always call the `loops` tool: reading a routine by eye "
    "reliably misses loops nested inside conditionals."
)


def to_openai_tools(specs: list[dict]) -> list[dict]:
    """MCP tool specs -> OpenAI function-calling format.

    The two schemas differ only in wrapping: MCP's `inputSchema` is OpenAI's
    `function.parameters`, everything else lines up.
    """
    return [
        {"type": "function", "function": {
            "name": spec["name"], "description": spec["description"],
            "parameters": spec["inputSchema"],
        }}
        for spec in specs
    ]


def run_conversation(
    client: httpx.Client, url: str, model: str, question: str,
    index, tools: list[dict], system: str | None, max_tokens: int,
) -> tuple[str, int]:
    """One question to completion. Returns (final answer, tool-call count)."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": question})

    tool_calls_made = 0
    for _ in range(8):  # generous cap; a real answer needs far fewer
        payload = {
            "model": model, "messages": messages, "tools": tools,
            "max_tokens": max_tokens,
        }
        response = client.post(url, json=payload, timeout=120.0)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        messages.append(message)

        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content", ""), tool_calls_made

        for call in calls:
            tool_calls_made += 1
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
                result = dispatch(index, name, args)
            except Exception as exc:  # the model gets to see and recover from this
                result = {"error": str(exc)}
            messages.append({
                "role": "tool", "tool_call_id": call["id"],
                "content": json.dumps(result, separators=(",", ":")),
            })

    return "(gave up after 8 rounds without a final answer)", tool_calls_made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="ANT chat-completions endpoint")
    parser.add_argument("--model", required=True, help="Model name, e.g. laguna-s")
    parser.add_argument("--key", default=None, help="Bearer token, if the endpoint needs one")
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--pointer", type=Path, default=None,
                        help="File whose text becomes the system prompt (the fair-fight "
                             "arm). Omit for the bare-tools arm.")
    parser.add_argument("--max-tokens", type=int, default=2000,
                        help="Workstation guide: give these models room, they think "
                             "privately first and a small limit returns what looks "
                             "like an empty reply")
    args = parser.parse_args()

    if any(port in args.url for port in _NO_TOOL_CALLING_PORTS):
        parser.exit(2, "error: tool calling is documented as broken on this port "
                       "(maverick / nemotron-3-super / nemotron-3-ultra); "
                       "this test cannot run against it.\n")

    try:
        index = build_source_index(args.source, args.corpus)
    except IndexError_ as exc:
        parser.exit(2, f"error: {exc}\n")

    tools = to_openai_tools(tool_specs())
    system = args.pointer.read_text(encoding="utf-8") if args.pointer else DEFAULT_INSTRUCTION
    headers = {"Authorization": f"Bearer {args.key}"} if args.key else {}

    print(f"model    {args.model}")
    print(f"pointer  {args.pointer or '(default instruction, see --pointer)'}")
    print(f"{'question':<58} {'calls':>6}  answer")
    print("-" * 100)

    with httpx.Client(headers=headers) as client:
        total_calls = 0
        for question, expect in QUESTIONS:
            answer, calls = run_conversation(
                client, args.url, args.model, question, index, tools, system, args.max_tokens,
            )
            total_calls += calls
            hit = all(term.lower() in answer.lower() for term in expect[:1])
            print(f"{question:<58} {calls:>6}  {'ok' if hit else 'CHECK'}  "
                  f"{answer[:80].replace(chr(10), ' ')}")

    print("-" * 100)
    print(f"total tool calls across 4 questions: {total_calls}")
    print("\nScore each answer by hand against the frozen expectations in "
          "evaluation/source_navigation.jsonl -- the substring check above is "
          "a hint, not a grade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
