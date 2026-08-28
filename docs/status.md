# Where Tamandua stands

The one-page map. Read this before anything else in `docs/`.

Last updated 2026-08-27 (real-source launch and post-fork bug fixes verified).

---

## What this is

A **facts-only index of SWAT+ Fortran source**, delivered two ways: a 14-tool
MCP server, and a generated file in the checkout. Both answer the same
questions — which routine reads this file, what calls this, what assigns this
variable, what loops are here, where do I set a breakpoint — from static
analysis, with nothing written by a model.

The point is to make **whatever assistant a developer already uses** cheaper
and more accurate on SWAT+, not to be an assistant.

## Measured

| | |
|---|---|
| Full-tree build | **3.199 s**, 4,547,355 bytes, 734 procedures and 510 derived types (SWAT+ 62.0.0) |
| Staleness check | **14 ms** — hashes the working tree, so uncommitted edits count |
| Live reload | one running process picked up a replaced facts file on its next request |
| Frozen source navigation | **12/12**, including `aquifer.aqu` → `aqu_read` |
| Output reader vs. independent `awk` | exact match on real Ames data |
| Tests | **151 pass, 9 skipped** with the parser; **128/32** without; real-source gate **158/2** |

`index_experiment.md` and `output_reader_experiment.md` carry the method and
the caveats for these.

**Verified on real source 2026-08-27.** The pinned parser handles the complete
648-file SWAT+ 62.0.0 tree, and the frozen navigation evaluation is 12/12.
Still unverified against the new scanner: the eight-question byte comparison
(grep 194,675 B · index 11,639 B) and the field-doc coverage figure (4,003 of
6,904), both measured with swatplus-doc-builder.


## What exists

- `tamandua/index/` — parse, render, install pointers, snapshot, scope
- `tamandua/mcp/server.py` — 14 read-only tools over the same objects
- `tamandua/mcp/client.py` — generic MCP stdio client
- `tamandua/output/reader.py` — query a run's output, refuses files it cannot index safely
- `swatplus-build` — writes `swatplus-facts.json` plus the optional-by-presence
  `swatplus-rhs.json` expression sidecar (both are release assets; use
  `--no-rhs` for base facts only); `--markdown` adds the greppable rendering and
  the instruction pointers for tools that cannot run a server



## Resolved

**Distribution.** The parser is separate (`swatplus-reference-corpus`, and
`swatplus-doc-builder` before it), so this used to be unusable by anyone
outside the team. The facts file and validated expression sidecar split
building from serving: the parser is now a build-time dependency only, and the
release workflow publishes JSON the server answers from with neither the
parser nor a SWAT+ checkout present. See [D-8](decisions.md).

## Where the findings are

| Document | Question it answers |
|---|---|
| `index_experiment.md` | Index vs grep, measured, with the script |
| `output_reader_experiment.md` | Reading a run's output, and the files that cannot be indexed safely |
| `ant_integration.md` | Testing local models, and whether to fold this into ANT |
| `launch_checklist.md` | Everything between "code is ready" and "someone else can install it" |

Three earlier experiment write-ups (`three_arms.md`, `mcp_vs_index.md`,
`adoption_eval.md`) were removed in the 2026-08-26 declutter: their headline
conclusion — that a checked-in file beats an MCP server, so don't build the
server out — was overturned by the real-session evidence summarised above.
Their surviving findings are in this document. The originals are in the
archived repository's history ([github.com/tugraskan/SWATPLUS-TACI](https://github.com/tugraskan/SWATPLUS-TACI)).


## Worth carrying forward

Eight defects in this work were found by ordinary use and none by any harness: an
ambiguous column that cost eight tool calls, a variable index keyed on the wrong
thing, a dead `python -m` entry point, a correct tool result that read as a
failure, a dropped column in heterogeneous rows, and 159 files miscounted from a
missing loop form, input files keyed by expressions instead of their source
defaults, and a long-running server that kept serving its startup snapshot.

Every one was invisible to measurement and obvious within one real question.
The two post-fork fixes were adapted from archived commits `c5fa088` (live
freshness) and `8760e3d` (input filenames); Tamandua also retains its stronger
parser-level input resolution at the pinned parser commit.
