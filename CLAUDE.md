# Tamandua

**Read `docs/status.md` before anything else** — it is the one-page map of what
is current, what is measured, and what is open.

## What this is

A facts-only index of SWAT+ Fortran source -- procedures with locations, the
call graph, file I/O with unit numbers, variable assignments by field path,
loop headers with their index variables, derived types with the units and
meaning the source documents inline. Everything is static analysis; nothing is
written by a model.

Two deliveries over one implementation:

- `tamandua/mcp/server.py` -- 14 read-only tools
- `tamandua/index/` -- writes `SWATPLUS_INDEX.md` into a checkout,
  plus a pointer in each assistant's instruction file

`tamandua/output/reader.py` answers the one thing no index can: what
a run's numbers actually did.

Building needs the parser in swatplus-reference-corpus; **serving does not**.
`swatplus-build` writes `swatplus-facts.json`, which the server answers from
with neither the parser nor a SWAT+ checkout present (docs/decisions.md D-8).

## Working here

```bash
export SWATPLUS_SOURCE=/path/to/swatplus
export SWATPLUS_REFERENCE_CORPUS=/path/to/swatplus-reference-corpus  # the parser
pip install -e ".[dev]"
python -m pytest -q          # 140 pass, 9 skipped (119/30 without the corpus)
```

`swatplus-build` from inside a SWAT+ checkout writes the facts file and
nothing else; `--markdown` adds the greppable rendering and the instruction
pointers, for a tool that cannot run a server. It rebuilds when either the
source or parser has changed, so it is safe to run before every question.

## Conventions

- **Facts only.** If a model wrote it, it does not go in the index. Only
  swatplus-reference-corpus's *schema scanner* is used; its 1,095 generated
  prose pages are deliberately unused. The same rule retired its predecessor's
  overlays, where an audit found ~1 in 5 AI-generated equation mappings
  pointed at the wrong routine.
- **Every claim carries its evidence.** A row without a file and line is not
  worth shipping.
- **Measure before concluding.** Findings live in `docs/*_experiment.md`, each
  with the script that produced them and its caveats. Where a later measurement
  overturned an earlier conclusion, the earlier write-up goes -- `docs/status.md`
  records what survived.
- Six defects in this work were found by ordinary use and none by any harness.
  Prefer running the thing over reasoning about it.

## The chatbot

An earlier design -- router, retrieval, agent loop, answer assembly, model
providers, CLI and hosted web interfaces -- was removed on 2026-08-26 and
preserved at `222092d` in the archived predecessor repository
(https://github.com/tugraskan/SWATPLUS-TACI). It is meant to be revisited, but it
is **not** the current focus: do not reason about this repo as though that
system were still part of it.
