# Where Tamandua stands

The one-page map. Read this before anything else in `docs/`.

Last updated 2026-09-29 (input-file links derived from the comparisons SWAT+
makes, format 5; two layout fixes; bundled snapshot rebuilt). Earlier the same
day: input-file layouts derived from the facts; filenames passed as arguments
followed to their callers; parser pin bumped to `110c2a2` for a field-doc
attribution fix. Previously updated 2026-09-22 (derived-type field
declarations restored, format 4, bundled snapshot rebuilt).

---

## What this is

A **facts-only index of SWAT+ Fortran source**, delivered two ways: a 15-tool
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
| Tests | real-source gate **301 pass, 0 skipped**; **263/38** with neither source nor parser. Both measured 2026-09-29 after the links change (format 5), on the pinned tree. |
| | Counts exclude `tests/test_ant_harness.py`; see the httpx note below. |
| Full-tree build | **5.8 s** on this runner, 734 procedures and 510 derived types (SWAT+ 62.0.0), unchanged by the parser swap |
| Loop recovery vs the parser | **2,833 of 2,833 agree**, none invented; 19 remaining are gwflow_pond.f90, still unresolved by design |
| Assignment targets vs the parser | **21,770 of 21,770 agree**, neither misses one the other finds |

`index_experiment.md` and `output_reader_experiment.md` carry the method and
the caveats for these. Neither came across in the fork; both are in the
archived repository (see "Where the findings are").

## Input-file links (format 5, 2026-09-29)

SWAT+ never declares that a column of one input file names a row of another;
it searches at run time, `if (hru_db(i)%dbsc%land_use_mgt == lum(ilum)%name)`
inside `do ilum`. The index now keeps each such test as a fact, and
`tamandua/index/layouts.py` derives the links from them: every layout column
carries `references` (target file, column, its position, and the
`file.f90:line` evidence), and `swatplus-layouts --links` lists every link and
every comparison that shows none, with why. So `hru-data.hru`'s
`land_use_mgt` → `landuse.lum` `name`, evidence `hru_read.f90:71`. Nothing is
matched by name, and the dataselector's static links are a comparison only.

| On 62.0.0 (`de210d6`, parser `110c2a2`) | |
|---|---|
| Comparisons stored | **389** in `if`/`else if` conditions, plus **122** restated at a call to the routine that makes them |
| Copies stored | **277**, the assignments that carry a column's value to a comparison |
| Links | **162**, from 159 comparison sites; 20 through `search()`, 18 through a copy |
| Grep baseline, `if (x == a(i)%name)` | **105 of 113** sites become links; the 8 others are `d_tbl` (a pointer at four tables), a value never read from a file, and a loop over one table's conditions |
| File pairs shared with the editor schema | **75** (Tamandua 160, editor 185); same column on 73 |
| Snapshot cost | **+150,941 bytes, +2.04%** |

The two column disagreements favour the source: the editor makes
`landuse.lum`'s `urb_ro` a key to `urban.urb` although SWAT+ only switches on
it, and says `channel-lte.cha`'s `nut` names `sed_nut.cha`'s rows where
`sd_channel_read.f90:282` searches them with `hydc`. Of the pairs only the
editor has, 98 of 110 involve a table SWAT+ never reads under that name; in
the 12 others SWAT+ 62.0.0 does not search (the `initial.aqu` org-min lookup
is commented out) or links by index or row position, which is not covered.
Of the pairs only Tamandua has, 72 of 86 involve a file the editor has no
table for (36 of them the four decision tables under their own names) and 14
are the `management.sch` operation columns and `constituents.cs`, which the
editor stores as free text.

Three variants needed more than the plain pattern, and each is a static fact:
a search made in a called routine on its dummy arguments (`search`, how every
`.con` file reaches `weather-sta.cli`) is restated at each call with the
actual arguments; a value copied before it is compared (`mgt =
sched(isched)%mgt_ops(...)`) is followed when every assignment to it is a copy
from one column; and a local matches only the nearest read before the test.

**The format change** follows formats 3 and 4: `INDEX_FORMAT_VERSION` and
`SNAPSHOT_FORMAT` are `5`, format 4 and older still load with no comparisons,
no copies and so no links, and `test_the_bundled_snapshot_carries_comparisons`
asserts the shipped file really has them. The rebuild changed only the two new
sections, the format counters, `generated_at`, and one I/O row (below); the
RHS sidecar is byte-identical, and a second rebuild was identical apart from
`generated_at`. `LAYOUT_FORMAT` is `2`.

**Two layout defects** turned up following links to columns that were not
there, both found by use, not by a harness. A name read once per row --
`read (107,*) pest_soil_ini(ipesti)%name` inside `do ipesti` -- was taken for
preamble, which set `data_starts_after` one line too deep in seven files and
left six (`pcp.cli`, `tmp.cli`, `slr.cli`, `hmd.cli`, `wnd.cli`, `pet.cli`)
with no layout at all. And `read_mgtops` reads `management.sch`'s operation
lines on a unit its caller opened, so the statement was filed under
`unit_107`; the build now takes the file a unit is open on at every call site
(all must agree), and the layout makes such a reader's records `child`.

Method, the comparison pair by pair, and the caveats -- one-line `if`
assignments are invisible to the parser (checked: none writes a path a
copy-based link relies on), links by index or position are not covered, a
pointer aimed at several tables is not resolved: `links_experiment.md`,
`scripts/measure_links.py`.

## Input-file layouts (2026-09-29)

`tamandua/index/layouts.py` and `swatplus-layouts` derive what SWAT+ reads
from each input file, column by column, in read order: the read statement's
variables (`IOUse.fields`) expanded through the derived types they land in.
It runs on any facts file, the bundled one included, so it needs neither the
parser nor a checkout. The first consumer is the dataselector, whose schema is
otherwise a static extraction from one swatplus-editor commit.

- **257 of 257** layouts on 62.0.0 have a complete main record; 260 of 260 on
  `97ca231` (252 and 255 before the two layout fixes above).
- On Ames, **46** of 109 files have a layout, against 37 in the dataselector's
  static schema (44 before the per-row name fix above added `pcp.cli` and
  `tmp.cli`). Of those, 27 are read value-for-value and 9 read a leading run
  with the tail (usually `description`) unread; the 2 that read more than the
  row holds are `file.cio` and `soil_plant.ini`, whose Ames form is the
  layout's `alternative`.
- Between 62.0.0 and `97ca231`, 10 files' records changed and 8 files
  appeared -- what a frozen schema misses.

Two build fixes came with it, both changing what the bundled snapshot says:
the twelve `.con` files were filed under `hyd_read_connect`'s dummy argument
`con_file`, and are now resolved through its call sites; and the two
`backspace 107` statements written without parentheses were dropped, and are
now kept on the right file. Method, numbers and caveats (header names differ
from Fortran names; branch conditions are not in the facts):
`layouts_experiment.md`. Links between files are the section above.

The dataselector pin moved to its published v0.2.0 (`0c73c64`) the same day:
it compiles, and its standalone MCP server lists the same six tools and
answers against Ames through `tamandua.mcp.client`. Its "Set Up This
Workspace" installs Tamandua but not the parser, which building layouts from a
live checkout needs.

## The parser pin moved again (2026-09-29)

`7a6e21ec` → `110c2a2`, for the corpus's `01adce1`. A comment aligned under a
declaration's inline comment, with no leading `|`, was attributed to the *next*
declaration: `basin_control_codes%nam1` carried `pet`'s method codes
("0 = Priestley-Taylor ... not used") under `nam1`'s own correct file and line.

Rebuilt on 62.0.0 with both parsers and diffed:

- **102** derived-type fields changed description. Nothing else moved:
  procedures, I/O, loops, writers and module variables are identical, and the
  source fingerprint is unchanged.
- **221 pass, 0 skipped** with source, parser and Ames present (excluding
  `tests/test_ant_harness.py`), before and after.
- The schema scanner is still stdlib-only: both builds ran with `fparser`
  not installed.

The bundled snapshot and its RHS sidecar are rebuilt from the new pin; only
`provenance.parser_commit`, `generated_at` and those field descriptions differ.

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

## Module-level variables are now indexed (format 3)

The index kept two of the three classes of Fortran name -- derived-type
components and procedure arguments/locals -- and dropped the third. Module-level
variables were parsed and discarded: `input_filenames` read a name and a type
off `project.modules` to resolve input filenames and threw the rest away. So
the index could describe the type `aquifer_dynamic` in full while unable to say
that `aqu_d` existed, what type it had, or which module owned it -- although
SWAT+ keeps nearly everything in module-level instances of derived types, as
`field_path` has always noted.

Measured against the pinned tree (SWAT+ 62.0.0, parser `7a6e21ec`):

| | |
|---|---|
| Module-variable declarations | **2,018** across 66 modules |
| Distinct bare names | 2,003 |
| Names declared in more than one module | **15** |
| Records carrying name, type, declaration and line | 2,018 -- all |
| ... also carrying a description | 539 |
| ... also carrying units | 154 |
| ... also carrying an initial value | 474 |
| Carrying the `parameter` attribute | 10 |
| Snapshot cost | **+571,536 bytes on the base file, +8.94%** |
| Source `(module, variable)` pairs matching object symbols | 2,014 of 2,018 |
| Object-only `_mp_` symbols | 119, all module procedures |

Everything above is reproduced from the pinned tree (`de210d6`, parser
`7a6e21ec`) except the last two rows, which need a real ifx build and are
carried from the review that requested this change.

**Three review figures did not survive reproduction.** They were taken on the
same pins, so the discrepancy is in the measurement, not the tree:

- Documentation was reported as 638. It is **539** descriptions and **154**
  units -- 638 is neither, nor their union.
- The `parameter` count was reported as 4, which was the number of source
  declarations with no matching object symbol. **10** carry the attribute. The
  two are different questions, and 10 is the right filter for a symbol map: a
  `parameter` has no runtime storage whether or not the compiler emitted a
  symbol for it.
- The snapshot cost was reported as +382,883 bytes (+5.72%) against a
  6,698,241-byte base. The base is **6,393,448** bytes -- byte-identical to
  what was already bundled, which is how we know the rebuild is reproducible
  -- and the section costs **+571,536** bytes, **+8.94%**. Half again as much
  as reported.

The sidecar decision is unchanged and now rests on the corrected figure: an
equivalent sidecar carries the same ~572 KB of records, so it still saves
nothing while adding matching, versioning and stale-artifact failure modes.

**The keying is the point.** `hsaltb_d` is declared in both
`output_ls_salt_module` and `salt_module`. A lookup on the bare name has to
pick one, and `tools/generate_fortran_symbols.py` in vsc_ifx_debug picks by
sorting the mangled symbols -- so the winner is whichever *module name* sorts
first, unrelated to the scope the question came from, while routines such as
`gwflow_canal_div` explicitly import `salt_module`'s version. So
`module_variables` is keyed `(module, name)` and `module_variables_named`
returns the whole candidate set rather than choosing. `colliding_module_variable_names`
reports which names cannot be resolved on the bare name at all.

`is_parameter` is stored rather than re-derived by consumers: a `parameter` has
no runtime storage and therefore no object symbol, so anything projecting a
debugger symbol map must exclude those 4 -- and should not need its own Fortran
attribute parser to find out which.

**The bundled snapshot was rebuilt** against the pinned source and parser, so
both format counters are at `3` and `tamandua/data/` carries the section. From
a plain install, `aqu_d` now answers `aquifer_module:56` and
`colliding_module_variable_names` returns its 15 entries.

Two tests guard it, and the second exists because the first is not enough on
its own: formats 1 and 2 stay readable and the new section loads *empty* from
them, so a stale bundle would answer every module-variable query with "not
found" while loading without complaint. That is exactly how the format-1
bundle went unnoticed while answering every `breakpoint` query with zero loops,
so `test_the_bundled_snapshot_carries_module_variables` asserts the records are
really there rather than trusting the version number.

## The server now picks its own source (2026-09-16)

A server aimed at the wrong checkout answers confidently, correctly, and about
code the caller is not looking at. That happened: a Codex config pinned
`--source` at `swatplus-main`, the checkout had long since been superseded by a
working branch, and every answer it gave was internally correct and about the
wrong tree. It took a full investigation to notice, and only because that
client thought to call `provenance` unprompted.

Two causes, both now addressed.

**Nothing said which tree was being served.** `initialize` returns an
`instructions` string every client reads before its first tool call. It now
carries the source:

    Source: /repo/src at commit de210d64db4f, re-read whenever that tree
    changes. Confirm it is the checkout you are reasoning about before quoting
    any file and line.

Once per session, not per call, so the warning costs nothing at the scale that
matters. The bundled case says plainly that it is a fixed release and that the
path in its provenance names the machine that built it -- the mistake that path
otherwise invites.

**A path had to be written down at all.** Without `--source`, `--facts` or
`$SWATPLUS_SOURCE`, the server went straight to the bundled snapshot; the
working directory was never consulted, even though `resolve_source` already
knew how to recognise a checkout. So every editor config needed a hand-written
absolute path, and a hand-written path is exactly what goes stale.

`auto_source()` now decides from the working directory. An editor starts an MCP
server inside the project it has open; a desktop chat app has no project and
starts somewhere neutral. That one fact separates the two cases, so the same
config serves both:

| Where it starts | What it serves |
|---|---|
| a SWAT+ checkout | that tree, live |
| anywhere else | the bundled snapshot |
| a checkout it cannot index | the bundled snapshot, **and says why** |

That last row matters: falling back is right -- a caller who merely happens to
be in a checkout without the parser is better served by the release than by a
dead server -- but falling back *silently* is the defect this whole section is
about, so the reason is appended to the source note.

`--source` and `--facts` still pin deliberately and win over the automatic
choice. Verified across all four cases.

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

## Derived-type field declarations are now indexed (format 4)

Module variables kept their full declaration string -- `type (soil_profile),
dimension(:), allocatable :: soil` -- so a consumer could tell an allocatable
array from a scalar. Derived-type *components* kept only the bare type name:
`soil_profile%phys` reported `type (soil_physical_properties)` with no way to
know it was `dimension(:), allocatable`. The parser always carried it --
components are `VariableRef` objects with a `declaration` attribute, same as
module variables -- Tamandua's conversion in `build.py` just never read it off
the component and never stored it on `Field`.

Measured against the pinned tree (SWAT+ 62.0.0, parser `7a6e21ec`), holding
source commit and parser commit fixed so the only difference from the
previous bundled snapshot is this field:

| | |
|---|---|
| Derived-type fields | **6,877**, across 510 types |
| ... now carrying a declaration | **6,877 of 6,877** -- all |
| ... whose declaration says `allocatable` | **355** |
| ... whose declaration says `dimension` (fixed-size arrays included) | 358 |
| Snapshot cost | **+426,549 bytes on the base file, +6.12%** (6,964,984 -> 7,391,533 bytes) |

That last row is the whole reason to measure rather than guess: 355 fields
were reporting as indistinguishable from a scalar to any consumer, for a
6.12% file-size cost to fix it.

**The same two traps as the module-variable section, handled the same way.**
`Field` gained `declaration: str | None = None` -- a default, not a required
argument, so `Field(**f)` still loads every existing snapshot's six-key field
dicts without raising. `INDEX_FORMAT_VERSION` and `SNAPSHOT_FORMAT` both moved
to `4`; `READABLE_SNAPSHOT_FORMATS` now lists `1`, `2`, `3` explicitly ahead of
the current format, so a pre-format-4 file keeps loading -- with `declaration:
None` on every field, exactly as formats 1 and 2 still load with
`module_variables` empty. The guard against that going unnoticed a second time
is `test_the_bundled_snapshot_carries_field_declarations`, which asserts the
shipped file's fields actually carry declarations (and that at least one says
`allocatable`) rather than trusting the version number; a companion test,
`test_a_format_three_snapshot_loads_fields_with_no_declaration`, pins the
old-format read path itself. Both are new in `tests/test_snapshot.py`, next to
`test_the_bundled_snapshot_carries_module_variables`, which they were modelled
on.

`describe_type` (`tamandua/mcp/server.py`) now returns `declaration` alongside
`type`, `units` and `means` -- it was the obvious consumer, since it answers
"what does `aqu_d(iaq)` actually contain" field by field.

`module_variable` had the same gap one layer up: its response already derived
`type`, `units` and `parameter` from `ModuleVariable.declaration`, but never
handed back the declaration string itself, so it could not say "allocatable"
any more directly than `describe_type` could before this change. Fixed the
same day, no format bump needed -- `declaration` was already stored on
`ModuleVariable` since format 3, so this was only the tool's response
shape, guarded by `test_module_variable_surfaces_the_declaration`.

**The bundled snapshot was rebuilt** against the pinned source and parser, so
both format counters read `4` and `tamandua/data/swatplus-facts.json` carries
a `declaration` on all 6,877 fields.

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

**Unrelated, and resolved.** `tests/test_ant_harness.py` uses
`httpx.MockTransport`, which httpx 1.0 removes -- those 5 tests fail on an
httpx 1.0 prerelease. `pyproject.toml` has capped the dev extra at
`httpx>=0.27,<1` since `ae0b9f7`, with a comment saying why; it resolves to
0.28.1 and the 5 pass (2026-09-29). Nothing to do with the parser.

**Verified on real source 2026-08-27.** The pinned parser handles the complete
648-file SWAT+ 62.0.0 tree, and the frozen navigation evaluation is 12/12.
Still unverified against the new scanner: the eight-question byte comparison
(grep 194,675 B · index 11,639 B) and the field-doc coverage figure (4,003 of
6,904), both measured with swatplus-doc-builder.


## What exists

- `tamandua/index/` — parse, render, install pointers, snapshot, scope
- `tamandua/mcp/server.py` — 15 read-only tools over the same objects
- `tamandua/mcp/client.py` — generic MCP stdio client
- `tamandua/output/reader.py` — query a run's output, refuses files it cannot index safely
- `tamandua/index/layouts.py` — input-file column layouts in read order, from the facts
- `swatplus-build` — writes `swatplus-facts.json` plus the optional-by-presence
  `swatplus-rhs.json` expression sidecar (both are release assets; use
  `--no-rhs` for base facts only); `--markdown` adds the greppable rendering and
  the instruction pointers for tools that cannot run a server
- `swatplus-layouts` — writes those layouts as JSON from a facts file (default:
  the bundled one), each column with the rows of other files it references;
  `--links` lists every link and every comparison that shows none



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
| [`index_experiment.md`](https://github.com/tugraskan/SWATPLUS-TACI/blob/8760e3d/docs/index_experiment.md) (archived) | Index vs grep, measured, with the script |
| [`output_reader_experiment.md`](https://github.com/tugraskan/SWATPLUS-TACI/blob/8760e3d/docs/output_reader_experiment.md) (archived) | Reading a run's output, and the files that cannot be indexed safely |
| `layouts_experiment.md` | Deriving input-file layouts from the facts, measured on Ames and across two SWAT+ trees |
| `links_experiment.md` | Which column names a row of another file, from the comparisons SWAT+ makes, against the editor schema's foreign keys |
| `ant_integration.md` | Testing local models, and whether to fold this into ANT |
| [`launch_checklist.md`](https://github.com/tugraskan/SWATPLUS-TACI/blob/8760e3d/docs/launch_checklist.md) (archived) | Everything between "code is ready" and "someone else can install it" |

The three marked archived never came across in the fork; the links pin the
archived repository's last commit, `8760e3d`.

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
The links work added two more of the same kind: a per-row name line read as a
title, which hid six weather files' layouts entirely, and a routine reading a
unit its caller opened, which filed `management.sch`'s operations under
`unit_107`. Both surfaced by following a link to a column that should have
been there.
The two post-fork fixes were adapted from archived commits `c5fa088` (live
freshness) and `8760e3d` (input filenames); Tamandua also retains its stronger
parser-level input resolution at the pinned parser commit.
