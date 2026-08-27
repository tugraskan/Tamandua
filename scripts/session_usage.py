#!/usr/bin/env python3
"""Report real token usage and tool calls from Claude Code session transcripts.

`/cost` goes quiet on subscription plans, and asking the model how many tokens
it used gets you a confabulated number. Claude Code writes every session to
disk as JSONL with the provider's own usage figures on each assistant turn;
this reads those.

    python scripts/session_usage.py                    # every recent session
    python scripts/session_usage.py --match swatplus   # only matching projects
    python scripts/session_usage.py --limit 8 --csv results.csv

One row per session. Since the evaluation runs one question per fresh chat,
a row is a question.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from pathlib import Path


def transcript_root() -> Path:
    """Claude Code's per-project transcript directory."""
    for candidate in (
        Path(os.environ.get("CLAUDE_CONFIG_DIR", "")) / "projects",
        Path.home() / ".claude" / "projects",
    ):
        if candidate.is_dir():
            return candidate
    raise SystemExit(
        "no transcript directory found. Looked for ~/.claude/projects — "
        "set CLAUDE_CONFIG_DIR if yours lives elsewhere."
    )


def summarize(path: Path) -> dict | None:
    """Pull question, token totals, and tool-call count out of one session."""
    question = ""
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    tool_calls = 0
    turns = 0

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        message = event.get("message") or {}
        content = message.get("content")

        # First human turn is the question that was asked.
        if not question and event.get("type") == "user":
            if isinstance(content, str):
                question = content.strip()
            elif isinstance(content, list):
                question = " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()

        if event.get("type") == "assistant":
            turns += 1
            usage = message.get("usage") or {}
            tokens["input"] += usage.get("input_tokens", 0) or 0
            tokens["output"] += usage.get("output_tokens", 0) or 0
            tokens["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
            tokens["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
            if isinstance(content, list):
                tool_calls += sum(
                    1 for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                )

    if not turns:
        return None

    return {
        "when": datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M"),
        "project": path.parent.name,
        "question": (question[:58] + "…") if len(question) > 59 else question,
        "tool_calls": tool_calls,
        "turns": turns,
        # Cache reads are billed differently but still occupy context; report
        # the total the session actually moved, and the fresh input separately.
        "input": tokens["input"],
        "cache_read": tokens["cache_read"],
        "output": tokens["output"],
        "total": sum(tokens.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match", default="", help="only projects whose name contains this")
    parser.add_argument("--limit", type=int, default=12, help="how many recent sessions")
    parser.add_argument("--csv", type=Path, help="also write a CSV")
    args = parser.parse_args()

    sessions = sorted(
        (p for p in transcript_root().rglob("*.jsonl")
         if args.match.lower() in p.parent.name.lower()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: args.limit]

    rows = [row for path in sessions if (row := summarize(path))]
    if not rows:
        raise SystemExit("no sessions matched. Try without --match, or raise --limit.")

    print(f"{'when':<12} {'question':<60} {'calls':>5} {'in':>8} {'cached':>9} {'out':>7} {'total':>9}")
    print("-" * 115)
    for r in reversed(rows):  # oldest first, so the run reads in order
        print(f"{r['when']:<12} {r['question']:<60} {r['tool_calls']:>5} "
              f"{r['input']:>8,} {r['cache_read']:>9,} {r['output']:>7,} {r['total']:>9,}")

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(reversed(rows))
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
