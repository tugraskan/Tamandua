# Tamandua

Makes the coding assistant you already use — Claude Code, Copilot, Codex, a
local model — cheaper and more accurate on SWAT+.

It is not an assistant. It builds a **facts-only index** of the SWAT+ Fortran
source, from static analysis alone, and serves it as **MCP tools** and as a
file in your checkout. Procedures with locations, the call graph, file I/O with
unit numbers, variable writes by field path, loop headers with their index
variables, derived types with the units and meaning the source documents
inline. Nothing in it is written by a model.

Why bother: asked to list the loops in `hru_control`, an assistant working from
raw source read all 890 lines and produced a tidy table of 18 — of 21. Nothing
in the answer suggested it was incomplete. Exhaustive questions are exhaustive
by construction here, and every row carries its file and line.

- **Where things stand:** [`docs/status.md`](docs/status.md) ← start here
- **Decisions:** [`docs/decisions.md`](docs/decisions.md)
- **Dependency pins:** [`docs/pins.toml`](docs/pins.toml)

## Use it

**If you just want the tools** — no SWAT+ checkout or parser:

```bash
pip install swatplus-tamandua
```

The package includes the pinned SWAT+ facts snapshot. Point your assistant at
the server with no machine-specific paths:

```jsonc
// .mcp.json (Claude Code) — VS Code uses .vscode/mcp.json, where these
// entries nest inside a top-level "servers" object instead.
{
  "mcpServers": {
    "swatplus-source": {
      "command": "swatplus-mcp",
      "args": ["--compact"]
    }
  }
}
```

**If you have a SWAT+ checkout and the parser**, build your own:

```bash
cd <your swatplus checkout> && swatplus-build
```

That writes `swatplus-facts.json` and nothing else. Run it again whenever — it
checks that the file still matches both the source and parser and only rebuilds
when either changed, so an assistant can run it before every question without
deciding whether it is worth it.

**If your tool cannot run an MCP server**, `swatplus-build --markdown` also
renders `SWATPLUS_INDEX.md` and points every assistant instruction file
(`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`) at it, to be
grepped instead. That is the fallback, not the default.

The check hashes the **working tree, not the last commit** — questions get
asked about code being edited, which is exactly when a commit-based check
would call a stale index current.

## Pairs with dataselector

[`swatplus-dataselector`](https://github.com/tugraskan/swatplus-dataselector)
serves the other half: what a column in an *input* file means, and what is in
a real `TxtInOut` dataset. The two don't overlap — this one knows what the
Fortran does, that one knows what the files hold. Run both and tell your
assistant which is for which.

## Layout

```
tamandua/
├── index/             # build the facts, render, install pointers, snapshot
│   ├── build.py       #   static analysis -> SourceIndex
│   ├── snapshot.py    #   read/write swatplus-facts.json, so serving needs no parser
│   ├── render.py      #   SOURCE_INDEX.md
│   ├── install.py     #   assistant instruction-file pointers
│   └── scope.py       #   loop nesting, for conditional breakpoints
├── mcp/server.py      # 14 read-only tools over the same objects
├── mcp/client.py      # generic MCP stdio client (talk to another server)
├── output/reader.py   # query a run's output files
└── config.py          # loads docs/pins.toml
```

## Develop

```bash
export SWATPLUS_SOURCE=/path/to/swatplus
export SWATPLUS_REFERENCE_CORPUS=/path/to/swatplus-reference-corpus  # the parser
python -m pip install -e ".[dev]"
pytest                       # 140 pass, 9 skipped (119/30 without the corpus)
```

The parser is a build-time dependency only: the bundled facts file lets the
installed server run with neither it nor the SWAT+ source (see
[D-8](docs/decisions.md)). Pass `--facts` to use another snapshot or `--source`
to build live from a checkout.

```bash
# build the publishable artifacts
swatplus-build --facts dist/swatplus-facts.json --out dist/SWATPLUS_INDEX.md

# serve them with nothing else installed
swatplus-mcp --facts dist/swatplus-facts.json
```

Tagging `v*.*.*` runs [`.github/workflows/release.yml`](.github/workflows/release.yml),
which builds both against the pinned commits, deletes the checkouts, verifies
the snapshot still answers, and publishes it.

## Principles

Facts only — if a model wrote it, it does not go in the index. Every claim
carries its file and line. Measure before concluding; findings live in
`docs/*_experiment.md` with the script that produced them. Six defects in this
work were found by ordinary use and none by any harness, so prefer running the
thing over reasoning about it.

[release]: https://github.com/tugraskan/Tamandua/releases
