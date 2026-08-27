# Grep vs. index: measured

Does handing an assistant a pre-built source index actually save it work, or
does it just move the cost around? Measured, not estimated.

Run it yourself:

```bash
python scripts/build_index.py --source /path/to/swatplus/src --out SWATPLUS_INDEX.md
python scripts/compare_index_vs_grep.py
```

## Method

Eight questions, answered twice. The **grep** arm runs the commands a careful
agent would actually run against raw Fortran — locate, then read the region it
needs. The **index** arm greps the generated index. We count the bytes each arm
returns, because that payload is what lands in the model's context.

Grep gets good patterns, not naive ones, and the set deliberately includes
questions where grep should win.

## Result

| Question | grep | index | saving |
|---|---:|---:|---:|
| Where is `aqu_read` defined? | 701 B | 40 B | 94% |
| Which routine reads `aquifer.aqu`? | 2,459 B | 191 B | 92% |
| What calls `hru_control`? | 73 B | 8 B | 89% |
| What does `hru_control` call? | 34,958 B | 788 B | 98% |
| What writes `sw_volume_begin`? | 269 B | 48 B | 82% |
| Which routine writes `aquifer_day.txt`? | 3,591 B | 192 B | 95% |
| Loops in `hru_control`, with index vars | 34,958 B | 884 B | 97% |
| Which routines open unit 107? | 159,315 B | 5,967 B | 96% |
| **Total** | **236 KB** | **8 KB** | **97%** |

Roughly 59,000 tokens down to 2,000. All eight answered; loop lines and callee
lists were spot-checked against source and match.

## Caveats, so the number is not oversold

- **The unit-107 row flatters the index.** Grepping a bare number drags in
  noise. A smarter pattern would narrow it; the honest saving on that question
  is smaller than 96%.
- **This measures retrieval, not reasoning.** Once the agent is in the right
  file it still has to read and think. The index removes the hunt, not the work.
- **Where grep is already fine, it stays fine** — 89% off a 73-byte answer is
  not worth building anything for. The wins are concentrated in the questions
  that force a whole-file read.

## Two things this settled

**The parser is fast enough to run live.** 6.2 s for 734 procedures and 66
modules, pure-Python regex, standard library only. There is no reason to ship a
stale snapshot: index whatever checkout is in front of you, on startup.

**Output files needed a fix to be findable at all.** SWAT+ opens them through a
helper rather than a bare `open`:

```fortran
call open_output_file(2520, "aquifer_day.txt", 1500)
```

so the scanner recorded the writers of every output file as `unit_2520`.
`build_index.py` resolves 627 unit numbers back to filenames. Before the fix,
"which routine writes `aquifer_day.txt`" returned nothing — the one question
the index could not answer, and the entry point for any debugging workflow that
starts from a wrong number in an output file.

## Not answered here

Whether an MCP server beats a checked-in file. That is the next comparison, and
it is only worth running because this one came back positive.

## Using it

The index only helps if it is where the work happens. Install it into a SWAT+
checkout and any assistant opened on that repo finds it:

```bash
swatplus-build --markdown --install /path/to/swatplus
```

That writes `SWATPLUS_INDEX.md` next to the source and adds a section to the
checkout's `CLAUDE.md` explaining how to grep it. Both are regenerated in place
and the pointer is idempotent, so this is safe to re-run — and should be re-run
after changing code, since the `## Provenance` block records the commit the
index describes.

A worked example, four greps and about a kilobyte, starting from a wrong number
in an output file:

```
$ grep '^aquifer_day.txt|write|2520|' SWATPLUS_INDEX.md
aquifer_day.txt|write|2520|aquifer_output|22|time%day,...,iaq,aqu_d(iaq)

$ grep -m1 '^aquifer_output|' SWATPLUS_INDEX.md      # called by command
$ grep -m1 '^aqu_d|' SWATPLUS_INDEX.md               # written by aqu_1d_control, ~40 lines
```

Written at `aquifer_output.f90:22`, reached from `command`, carrying
`aqu_d(iaq)` — which `aqu_1d_control` assigns. The breakpoint terms come from
the write statement itself: `iaq` and `time%day`.

That last part was missing until the example was actually run. `aquifer_output`
has no loop of its own — `iaq` is set by its caller — so the loop listing came
back empty and the index could locate the write but not say what to break on.
The scanner had the statement's variables all along; the renderer was dropping
them. They are now the last column of every I/O row.
