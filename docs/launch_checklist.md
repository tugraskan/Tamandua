# Launching Tamandua

Everything between "the code is ready" and "someone else can install it."
Written for whoever runs it, human or agent. Steps 1–3 need a Windows/Linux
machine with a real SWAT+ checkout; the rest is GitHub.

Nothing here has been executed. The whole point of step 2 is that every
measurement so far ran against synthetic Fortran.

---

## What you need first

| | |
|---|---|
| SWAT+ source | tag `62.0.0`, commit `de210d6` (docs/pins.toml) |
| The parser | `tugraskan/swatplus-reference-corpus` at `2daa14ae7b50c597aefbc110734ec5bfc5472cb0` — **private** |
| Python | 3.11+ |

The parser is a build-time dependency only. Nobody installing Tamandua needs it.

---

## 1. Install and check the suite

```bash
git clone <this repo> && cd Tamandua
python -m pip install -e ".[dev]"

export SWATPLUS_REFERENCE_CORPUS=/path/to/swatplus-reference-corpus
python -m pytest -q
```

**Expect:** `140 passed, 9 skipped`. Without `SWATPLUS_REFERENCE_CORPUS` set you get
`119 passed, 30 skipped` — also fine, it just means the parser-backed tests
skipped rather than ran.

## 2. Build against real SWAT+ — the actual gate

```bash
export SWATPLUS_SOURCE=/path/to/swatplus         # the 62.0.0 checkout
swatplus-build --facts dist/swatplus-facts.json
```

**Record three things, because none of them are known yet:**

- **Does it finish at all?** The parser was swapped on 2026-08-26 and has never
  run over the real 648-file tree from here.
- **How long?** The old parser took 6.2 s. This one is a different
  implementation; `docs/status.md` marks that number as unverified.
- **How big is `dist/swatplus-facts.json`?** This decides step 6.

Sanity-check the result:

```bash
python scripts/eval_source_navigation.py
```

**Expect 12/12.** Anything less means the parser swap lost facts, and that is a
stop-and-investigate, not a launch. Report which case IDs failed.

Then confirm it serves with nothing else present:

```bash
env -u SWATPLUS_SOURCE -u SWATPLUS_REFERENCE_CORPUS \
  swatplus-mcp --facts dist/swatplus-facts.json --compact
```

Paste in one line and press enter:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"file_io","arguments":{"file":"aquifer.aqu"}}}
```

**Expect** a row naming `aqu_read`. That is the claim the whole distribution
story rests on: the file answers without the source or the parser.

## 3. Try it in a real assistant

Point a client at the file you just built and ask it a SWAT+ question.

Claude Desktop — `claude_desktop_config.json`:

```json
{"mcpServers": {"swatplus-source": {
  "command": "swatplus-mcp",
  "args": ["--facts", "C:\\path\\to\\swatplus-facts.json", "--compact"]}}}
```

VS Code uses `.vscode/mcp.json`, where the same entry nests inside a top-level
`"servers"` object instead of `"mcpServers"` — a different schema, and an easy
mistake.

Ask: *"Which routine reads aquifer.aqu?"* → `aqu_read`.

**Untested surface:** the server has only ever run under VS Code and Claude
Code. Claude Desktop and Codex speak the same protocol, so this should be
config-only — but nobody has confirmed it. If it fails, that is a finding, not
a mistake.

## 4. Create the repository

Push to `tugraskan/Tamandua` with a fresh history. Then:

- Add repository secret **`CORPUS_TOKEN`** — a token with read access to
  `tugraskan/swatplus-reference-corpus`. `.github/workflows/release.yml` checks
  the parser out with it, and without it the release job fails.

## 5. Cut a release

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The workflow builds the facts file and the markdown index against the pinned
commits, **deletes both checkouts, verifies the file still answers**, and only
then publishes. If it fails at the verify step, the artifact is not
self-contained and must not ship.

**Expect** two release assets: `swatplus-facts.json` and `SWATPLUS_INDEX.md`.

## 6. Decide how people install it

Now that step 2 gave you a real file size:

- **Under ~20 MB** → bundle the facts file inside the package as package data,
  make the server fall back to the bundled copy when `--facts` is absent, and
  publish to PyPI. Install becomes `pip install tamandua` and a config with no
  paths in it at all.
- **Larger** → keep the download step. `pip install tamandua`, download the
  asset, pass `--facts`.

Either way the README's install section needs updating to match what you chose.

## 7. Archive the predecessor

**Archive `tugraskan/SWATPLUS-TACI`. Do not delete it.**

Six links in these docs point into it, and commit `222092d` there is the only
surviving copy of the chatbot stack that was removed on 2026-08-26. Archiving
keeps it readable; deleting takes it with you.

---

## Known-unfinished, for the record

Not blockers, but do not let anyone discover them by surprise:

- Three figures in `docs/status.md` — parse time, the ~94% byte saving, and
  12/12 frozen cases — were measured with the *previous* parser and are marked
  as needing a re-run. Step 2 settles the third.
- `scripts/test_ant_model.py` is built and unit-tested against a mock, and has
  never run against a live endpoint. Local/weak models are the untested case
  that matters most, since they are the ones likeliest to need these tools.
- `search_fields` and `breakpoint` work and are tested, but neither has been
  used to answer a real question.
