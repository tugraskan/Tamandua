# Tamandua

Makes the coding assistant you already use — Claude Code, Copilot, Codex, a
local model — cheaper and more accurate on SWAT+.

It is not an assistant. It builds a **facts-only index** of the SWAT+ Fortran
source, from static analysis alone, and serves it as **MCP tools** and as a
file in your checkout. Procedures with locations -- the main program in
`main.f90.in` among them -- the call graph with every call site, file I/O with
unit numbers, variable writes by field path and where each value is read, loop
headers with their index variables, derived types with the units and meaning
the source documents inline. Nothing in it is written by a model.

- **Dependency pins:** [`docs/pins.toml`](docs/pins.toml)

## Use it

**If you just want the tools** — no SWAT+ checkout or parser:

```bash
pip install "git+https://github.com/tugraskan/Tamandua.git@v0.1.1"
```

The tagged package includes the pinned SWAT+ facts snapshot, verified fresh by
the release workflow -- pin a tag for that guarantee. Drop the `@tag` to track
`main` instead, which is the better choice once you have your own SWAT+
checkout (the server reparses that live, so the bundled snapshot is unused
anyway):

```bash
pip install --upgrade "git+https://github.com/tugraskan/Tamandua.git"
```

The future PyPI distribution name is `swatplus-tamandua` (`tamandua` is already
owned by an unrelated project). Point your assistant at the server with no
machine-specific paths:

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

**Input-file layouts.** What SWAT+ reads from each input file, column by
column, in read order -- derived from the facts, with every column citing its
declaration and every record the statement that reads it:

```bash
swatplus-layouts --file hru-data.hru                 # one file, bundled facts
swatplus-layouts --facts swatplus-facts.json --out swatplus-layouts.json
swatplus-layouts --links                             # every link between files
```

It needs neither the parser nor a checkout. To follow your own tree, build its
facts with `swatplus-build` first and pass them with `--facts`. It reports
what the model reads, not everything a file may hold, and carries no header
names; see [`docs/layouts_experiment.md`](docs/layouts_experiment.md).

**Links between input files.** A column whose value names a row of another
file carries `references`: `hru-data.hru`'s `land_use_mgt` → `landuse.lum`
`name`, evidence `hru_read.f90:71`. SWAT+ declares no foreign keys; it
searches at run time, and each link is one of those searches in the source --
directly, through a routine like `search`, or through a copy -- never a guess
from names that look alike. Against the editor schema's foreign keys, and what
neither covers: [`docs/links_experiment.md`](docs/links_experiment.md).

**Where a value is used.** `writers` says what assigns a variable; `uses`
says where its value is read, and how -- in a formula, a condition, a call, an
output list -- with the statement:

```
path|at|in|kind|detail|statement
hru%lumv%bmp_sed|smp_bmpfixed.f90:46|smp_bmpfixed|assignment|sedyld|sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)
bmpuser_db%bmp_sed|structure_set_parms.f90:103|structure_set_parms|copy|hru%lumv%bmp_sed|hru(j)%lumv%bmp_sed = bmpuser_db(istr)%bmp_sed
```

(`uses bmp_sed` against SWAT+ 62.0.1, from `swatplus-mcp --compact`.)

A bare name matches the field under any parent; a full path
(`hru%lumv%bmp_sed`) only that path and what it holds. A `copy` only moves
the value, so a field whose every read is a copy is never computed with under
that name -- `bmp_bac` -- and no rows at all means it is never read. The
reads and statements are in `swatplus-rhs.json`, beside the facts. Kinds,
limits and the recall audit: [`docs/reads_experiment.md`](docs/reads_experiment.md).

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
│   ├── render.py      #   SWATPLUS_INDEX.md
│   ├── install.py     #   assistant instruction-file pointers
│   ├── scope.py       #   loop nesting, for conditional breakpoints
│   ├── programs.py    #   the main program in main.f90.in, as a procedure
│   ├── reads.py       #   where each value is read, and how
│   └── layouts.py     #   input-file column layouts and the links between files
├── mcp/server.py      # 16 read-only tools over the same objects
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
