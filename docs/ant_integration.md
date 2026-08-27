# Testing with ANT's local models, and integrating with ANT

Everything measured so far used Claude Code — the model least likely to need
these tools, since composing a correct grep pattern is itself a skill a small
local model may not reliably have. This is that test, plus the separate
question of whether `swatplus-source` should live inside ANT itself.

## Testing local models

This session has no access to ANT's network (`172.20.1.78` is your LAN), so
this has to run on a machine that does. `scripts/test_ant_model.py` drives the
same agentic loop the byte comparisons used, against ANT's raw
OpenAI-compatible endpoints (workstation guide §7) rather than through MCP —
no client library needed, and every tool call the model emits is answered by
the real `tamandua/mcp/server.py` dispatch function, not a
re-implementation.

```powershell
cd C:\Users\taci.ugraskan\source\repos\Tamandua
pip install -e .[dev]
$env:ANT_API_KEY = "<your key>"

python scripts\test_ant_model.py `
  --url http://172.20.1.78:8000/v1/chat/completions --model laguna-s --key $env:ANT_API_KEY `
  --source "C:\Users\taci.ugraskan\source\repos\SWATPLUS_REPOS\swatplus-main" `
  --corpus "C:\Users\taci.ugraskan\source\repos\swatplus-reference-corpus"
```

Run it twice per model: once bare (the default instruction the script sends is
deliberately thin), once with `--pointer docs\pointers\mcp_claude_md.md` — the
same file that turned Claude's run from 10 calls/3 correct to 8 calls/4
correct. That comparison is the actual question: does a small model respond to
the same instruction the way Claude did, or does an instruction alone not
compensate for a weaker model.

**Which models to try**, per the workstation guide:

| Model | Note |
|---|---|
| `laguna-s` | Fastest, the default coding model — but the guide's own accuracy note says it approved a planted bug in 4 of 5 attempts. A real signal to watch for here, not just speed |
| `gpt-oss:120b`, `gemma4:26b` | Reached via `review_code` / `delegate_quick_task`; reliably caught the planted bug |
| `llama4-maverick`, `nemotron-3-super`, `nemotron-3-ultra` (port 8040) | **Do not test.** The guide documents tool calling as broken for these; the script refuses to run against that port |

Score by hand against the frozen expectations in
`evaluation/source_navigation.jsonl` — the script's own substring check is a
hint, not a grade, same caveat as every other arm.

## Integrating with ANT

Two different designs, and this session cannot build either — both are changes
to ANT's own server config, which the workstation guide says to take to
whoever administers it, not something reachable from a repository checkout.

### Option A — keep it separate (what exists today)

`swatplus-source` stays a per-repo MCP server, configured in each user's own
`.mcp.json` alongside ANT's `local-agent-router` entry. Nothing changes on
ANT's side.

- Works today; nothing to ask anyone for.
- Every user configures it themselves, and it only reaches whichever client
  they set it up in — Claude Code, say, not a model reached through
  `delegate_code_task`, unless that delegation path itself forwards tool
  access (untested; likely does not, since a delegate call reads as
  send-a-prompt-get-a-completion, not an agentic loop with the caller's
  tools attached).

### Option B — fold it into ANT's tool surface

Add `swatplus-source`'s tools as another backend the `local-agent-router`
aggregator exposes, alongside `search_swat_corpus`. Every model reached
through ANT gains them at once, with no per-repo setup.

- This is the pattern `search_swat_corpus` and the dataselector both already
  follow: one shared tool layer behind multiple transports, so the tool
  surface cannot drift between them.
- The cost is organizational, not per-user: roughly 4,500 bytes of standing
  schema cost for these fourteen tools, in
  every message, for every ANT session — including the large fraction of ANT's
  traffic that has nothing to do with SWAT+. That is the same standing-cost
  question this project has measured all along, now paid once by the server
  instead of once per configured client.
- Needs write access to ANT's configuration and whoever maintains it.

### What would decide between them

The local-model test above, run before asking anyone to change ANT:

- **If a local model reached through ANT reliably uses the tools once told
  to** (mirroring Claude's result), Option B's organization-wide standing cost
  buys real reliability and is worth proposing.
- **If a local model ignores the tools regardless of instruction** — plausible
  given the guide's own note that `laguna-s` fails to catch a planted bug 4 of
  5 times — folding the tools into ANT buys a schema cost that nothing uses,
  and Option A (opt-in, per-repo) is the more honest default until a stronger
  model is the one being routed to.

Either way, this is a proposal to make to ANT's administrator with the test
results in hand, not a change this repository can make on its own.
