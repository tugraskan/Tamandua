# Tamandua

Makes the coding assistant you already use — Claude Code, Copilot, Codex, a
local model — cheaper and more accurate on SWAT+.

It is not an assistant. It builds a **facts-only index** of the SWAT+ Fortran
source, from static analysis alone, and serves it as **MCP tools** and as a
file in your checkout. Procedures with locations, the call graph, file I/O with
unit numbers, variable writes by field path, loop headers with their index
variables, derived types with the units and meaning the source documents
inline. Nothing in it is written by a model.

- **Dependency pins:** [`docs/pins.toml`](docs/pins.toml)

## Use it

**If you just want the tools** — no SWAT+ checkout or parser:

```bash
pip install "git+https://github.com/tugraskan/Tamandua.git@v0.1.1"
```

The tagged package includes the pinned SWAT+ facts snapshot. The future PyPI
distribution name is `swatplus-tamandua` (`tamandua` is already owned by an
unrelated project). Point your assistant at the server with no machine-specific
paths:

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

**If you have a SWAT+ checkout and the parser**, point the server directly at
the source:

```bash
swatplus-mcp --source /path/to/swatplus --compact
```

The running process fingerprints the working Fortran tree before each request.
It reparses only after an edit, so the next answer follows the source without a
manual rebuild or server restart.

To produce a portable facts file for someone else:

```bash
cd <your swatplus checkout> && swatplus-build
```

That writes `swatplus-facts.json` and nothing else. A server started with
`--facts` notices when that file is replaced and reloads the newest complete
snapshot without restarting. The package-bundled release snapshot remains
static by design.

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
pytest
```

The parser is a build-time dependency only: the bundled facts file lets the
installed server run with neither it nor the SWAT+ source. Pass `--facts` to use another snapshot or `--source`
to build live from a checkout.

```bash
# builds swatplus-facts.json plus sibling swatplus-rhs.json by default
swatplus-build --facts dist/swatplus-facts.json --out dist/SWATPLUS_INDEX.md

# serve them with nothing else installed
swatplus-mcp --facts dist/swatplus-facts.json
```

Tagging `v*.*.*` runs [`.github/workflows/release.yml`](.github/workflows/release.yml),
which builds all three artifacts against the pinned commits, deletes the
checkouts, verifies the snapshot and sidecar still answer, and publishes them.

## Principles

Facts only — if a model wrote it, it does not go in the index. Every claim
carries its file and line. Measure before concluding; findings live in
`docs/*_experiment.md` with the script that produced them. Eight defects in this
work were found by ordinary use and none by any harness, so prefer running the
thing over reasoning about it.

[release]: https://github.com/tugraskan/Tamandua/releases
