# Where Tamandua stands

The one-page map. Read this before anything else in `docs/`.

Last updated 2026-09-15 (parser pin bumped; verified on real source; loop-scope defect fixed).

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
| Tests | real-source gate **187 pass, 0 skipped**; **178/9** without a SWAT+ checkout; **152/35** without the parser either |
| | Counts exclude `tests/test_ant_harness.py`; see the httpx note below. |
| Full-tree build | **5.8 s** on this runner, 734 procedures and 510 derived types (SWAT+ 62.0.0), unchanged by the parser swap |
| Loop recovery vs the parser | **2,833 of 2,833 agree**, none invented; 19 remaining are gwflow_pond.f90, still unresolved by design |
| Assignment targets vs the parser | **21,770 of 21,770 agree**, neither misses one the other finds |

`index_experiment.md` and `output_reader_experiment.md` carry the method and
the caveats for these.

## The parser pin moved (2026-09-15)

`docs/pins.toml` pinned corpus commit `2daa14ae`, which **no longer exists**:
the corpus rewrote its history for its public release, so `actions/checkout`
could not resolve that ref and `release.yml` could not have built. The pin is
now `7a6e21ec` (main), and the two guards in `tests/test_config.py` moved with
it.

Verified against a real SWAT+ 62.0.0 checkout (`de210d6`, 648 files), not just
the synthetic fixtures:

- Full-tree build succeeds, 734 procedures and 510 derived types -- the same
  figures as the previous parser.
- **187 pass, 0 skipped** with source and the Ames dataset present.
- Tamandua's two entry points, `BuildConfig` and `FortranScanner`, are
  unchanged, and every field it reads is still there.
- `FortranScanner.scan()` still leaves `called_by`, `call_paths` and
  `CallRef.resolved` empty, so `tamandua/index/analyze.py` is still required.
  The corpus grew its own semantic layer (`parser/semantic.py`), but `scan()`
  does not run it.
- The **schema scanner is still stdlib-only** -- only `parser/ast_index.py`
  imports `fparser`. Verified by scanning with `fparser` blocked from
  `sys.meta_path`. `release.yml` checks the corpus out rather than installing
  it, so this is load-bearing.

## Two defects the pin bump exposed

Neither was caused by the new parser; both were invisible until it gave a
second opinion to compare against. That is the ninth and tenth entry for
"found by ordinary use, not by a harness".

**Loop scope missed 172 real loops.** `index/scope.py` matched `do` only at the
start of a line, but SWAT+ packs whole loops onto one line behind a `;` --
`buf = 0.0; do k = 1, n; buf(k) = soil1(j)%str(k)%c; end do`, 133 times in
soil_nutcarb_write.f90 and 39 in soil_carbvar_write.f90. Every line inside one
reported **no scope at all**, so `breakpoint` offered no index variable to pin
-- the exact silent-wrong-answer that module exists to prevent. Fixed with a
quote-aware statement splitter and guarded by three tests in `test_scope.py`
(confirmed to fail against the previous code). Measured after the fix: 2,833
loops in common with the parser, **zero disagreement on any end line, none
invented**. The 19 the parser still finds are all in gwflow_pond.f90, the one
file whose `do`/`end do` do not balance, still reported unresolved rather than
guessed at.

**The bundled snapshot was two formats stale, and half of it was missing.**
`tamandua/data/swatplus-facts.json` shipped as `snapshot_format: 1` with
`parser_commit: 2daa14ae`. Format 1 is still *readable*, so it loaded silently
while missing everything format 2 added: per-procedure `arguments`, `locals`,
`uses` and `select_cases`, and `index`/`end_line` on every loop. Served from
that file, `aqu_read` reported 0 uses and 0 locals, and **every** `breakpoint`
query returned 0 loops. Rebuilt against `de210d6` with the new parser;
`aqu_read` now reports 4 uses and 9 locals, and a write inside a packed loop
yields `k == <value>`.

The sidecar was the other half. `swatplus-build` writes two files -- the
compact facts, and `swatplus-rhs.json` carrying the assignment expressions --
and `load_snapshot` treats the sidecar as optional-by-presence. Only the facts
file had ever been bundled, so on a plain `pip install` **every one of the
11,495 writer expressions read `unavailable`**, while the downloadable release
asset answered them fine. `release.yml` asserts expressions are available for
the dist asset and never checked the bundled copy, which is why it went
unnoticed. Both halves now ship: `writers` for `db_mx%aqudb` returns
`db_mx%aqudb = msh_aqp` from a bare install, and 10,119 of 21,598 writer
records carry an expression.

Package data goes 4.34 MB -> 7.77 MB: 6.39 MB of format-2 facts plus the
1.38 MB sidecar. Four tests in `test_snapshot.py` now assert the pair ships
together, matches on fingerprint and parser commit, answers with a real
expression, and is the current format; `release.yml` runs them against the
bundled copy before publishing. Three of the four fail if the sidecar is
removed.

## Not taken up

`AssignmentDoc.target` / `target_root` / `expression` make `build.py`'s
`_ASSIGN_RE` redundant. Measured on real source: the two agree on **all 21,770
assignments**, and neither finds a target the other misses. So switching is a
safe refactor with no behaviour change -- and for that reason not urgent. The
site is commented.

Likewise `IOOperation.condition` gives the `if` guard wrapping an I/O statement
(verified: a guarded `write` reports `if (pco%day_print == "y") then`). Nothing
here surfaces it yet. It is not what `scope.condition_for` builds -- that one
pins loop index variables for a debugger breakpoint -- so it would be an
addition, not a replacement.

**Unrelated, but latent.** `pyproject.toml` pins `httpx>=0.27` with no upper
bound. `tests/test_ant_harness.py` uses `httpx.MockTransport`, which httpx 1.0
removes -- those 5 tests fail on an httpx 1.0 prerelease. Nothing to do with
the parser.

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
parser nor a SWAT+ checkout present.

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
