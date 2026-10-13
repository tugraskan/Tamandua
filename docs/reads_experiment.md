# The main program, and where a value is read (format 6)

**Question.** Two gaps, both found by use. First: `callers("lsu_read_elements")`
answered none and a dozen readers had no call path, although `main.f90.in:69`
calls them -- is the main program indexable? Second: documentation review asks
of every input "is it ever used, and as what -- a fraction, a percent, in which
formula?", and the index could only say what *assigns* a variable. Can "where
is X read" be answered from static facts, exhaustively, and with the kind of
use?

**Answer.** Yes to both. The main program is now a procedure of kind
`program` in its real file, and every call path roots there first: on 62.0.0
51 procedures gain a call path and none loses one (55 on 62.0.1). Reads are a
new index -- 99,918 on 62.0.0 across 9,230 variable paths, each with its kind
and the statement -- served by the `uses` tool. Every `%component` reference
in executable code is a recorded read, a write, or one of four deliberate
exclusions. The cost is 1.9 s of build time and 8.5 MB of sidecar (1.1 MB
gzipped).

Script: `scripts/measure_reads.py`. Code: `tamandua/index/programs.py`,
`tamandua/index/reads.py`, `tamandua/index/analyze.py`, the `uses` tool.

## The main program

SWAT+ keeps `program main` in `src/main.f90.in`, a CMake template configured
into `main.f90` at build time. Two separate things kept it out:

- **The file was never listed.** The scanner selects files by their last
  suffix, which for `main.f90.in` is `.in`. Every enumeration Tamandua does --
  the source fingerprint, checkout detection, the scanner's own file list --
  now also takes a Fortran suffix followed by `.in`, case-insensitively
  (`is_fortran_source`). So an edit to `main.f90.in` makes facts stale.
- **A program was not a procedure.** For a `program` unit the scanner keeps
  calls and control flow only: no declarations, assignments or I/O, and no
  node in the call graph. Its body is the same Fortran as a subroutine's, so
  `program_scanner` hands the scanner the text with the opening and closing
  statements spelled as a subroutine's -- every other byte and line number
  unchanged -- and marks what comes back `kind = "program"`. The rewrite is
  never stored or shown; locations name `main.f90.in` and its real lines.

Call paths walk from programs first, then from the other uncalled routines
into the slots left. Walked together, a dead routine one call away crowded out
the program's real route (`reg_read_elements`, whose own call is commented
out, calls `lsu_read_elements` directly).

Rebuilt on 62.0.0 (`de210d6`, parser `110c2a2`) and diffed against the format-5
bundle built from the same pins: types, module variables, comparisons, scanner
warnings and unresolved files are identical; every earlier assignment
statement is preserved. What moved, all of it the program:

| | |
|---|---|
| Procedures | 734 -> **735**: `main`, `main.f90.in:1-178` |
| `called_by` gaining `main` | 56 procedures |
| Call paths gained | **51** procedures, none lost; 614 of the 617 procedures with a call path reach it from `main` |
| New I/O rows | `main` opens `simulation.out`, `erosion.txt`, `success.fin` |
| I/O rows resolved | **10** writes to unit 9003 in `proc_date_time` and `time_control`, filed as `unit_9003` because the `open` is in `main.f90.in`, are now `simulation.out` -- the scanner's own cross-file unit binding, given the file |
| Writers, loops, copies | `main`'s own, nothing else |

Each procedure also keeps `called_at`, every resolved call as `caller:line`, so
`callers` gives `main.f90.in:69 (main)` rather than a bare name.

## Where a value is read

`tamandua/index/reads.py` examines every executable statement inside a
procedure once, at build time: continuations joined, comments removed, `;`
statements split, one-line `if` and `where` split into their test and their
statement. Every variable reference read is recorded under its field path
(subscripts stripped, like `writers`) with one kind:

| Kind | Read as | `detail` |
|---|---|---|
| `assignment` | computed with on a right-hand side | the variable assigned |
| `copy` | the whole right-hand side, value only moved (`=`, `=>`, `trim`) | where it went |
| `condition` | `if`, `else if`, `do while`, `select case`, `case`, `where` | the construct |
| `loop_bounds` | a counted `do`'s bounds or step | the loop index |
| `call_argument` | in a `call`'s argument list | the routine |
| `function_argument` | in the argument list of a function defined in the tree | the routine |
| `output` | a `write` or `print` output list | the file, else the unit |
| `io_control` | an I/O control list: unit, `file=`, internal file | the statement |
| `index` | a subscript | the variable indexed |
| `allocate` | an `allocate` bound | the object allocated |

A subscript is an `index` read whatever the statement; otherwise the innermost
routine in the tree whose argument list holds the reference decides, and the
statement decides the rest. Intrinsics are transparent: `x = exp(-k)` computes
with `k`.

Not reads: declarations, the input list of a `read` (it *fills* the value),
a loop's or implied do's index, a keyword argument's name, an `inquire`
answer, `iostat=`, the object allocated or deallocated, comments. A name is a
variable only where Fortran puts it in scope -- the procedure's arguments and
declarations, its host's, its module's, and the module variables its `use`
statements reach (transitively, honouring `only` and renames). A numeric or
logical scalar followed by a bracket is a function: `real :: chg_par` types an
external's result and `real :: amin1` an intrinsic. Before that rule, both
were read as arrays, and every argument to `chg_par` -- 1,335 reads in
`cal_parm_select` alone -- was misfiled as a subscript.

### Measured

| | 62.0.0 | 62.0.1 |
|---|---|---|
| Reads | **99,918** | 100,750 |
| Variable paths read | 9,230 | 9,368 |
| Statements read from | 34,711 | 35,232 |
| `index` | 41,069 | 41,203 |
| `assignment` | 22,517 | 22,693 |
| `output` | 11,829 | 12,070 |
| `condition` | 9,724 | 9,854 |
| `copy` | 5,660 | 5,706 |
| `loop_bounds` | 2,592 | 2,645 |
| `call_argument` | 1,998 | 2,015 |
| `function_argument` | 1,788 | 1,798 |
| `allocate` | 1,741 | 1,762 |
| `io_control` | 1,000 | 1,004 |

**Recall, on 62.0.1.** Of 62,537 `%component` references in executable
statements (literals and comments stripped), 98.46% are a recorded read at
that line, the target of the line's assignment, or the input list of a
`read`. The other 961 are all exclusions by design: 908 are the object being
allocated, 36 deallocated, 16 in the target of a one-line `if` assignment, 1
an `inquire`'s `exist=`. `measure_reads.py` reports this as `recall`; on
62.0.0 it is 61,263 of 62,215 (98.47%), the rest the same four exclusions.

**The questions that prompted it**, on 62.0.1 (`97ca231`):

| Question | Answer |
|---|---|
| `uses bmp_sed` | `smp_bmpfixed.f90:46` assignment into `sedyld`: `sedyld(j) = sedyld(j) * (1. - hru(j)%lumv%bmp_sed)` -- a fraction, where the type documents `%`; and a copy at `structure_set_parms.f90:103` |
| `uses bmp_bac` | only `structure_set_parms.f90:108`, a copy into `hru%lumv%bmp_bac`, which nothing reads: never computed with |
| `uses frsw_gro`, `uses inject_dep` | none: columns of `plants.plt` and `chem_app.ops` (their layouts list them), never used |
| `uses husc` | `mgt_operatn.f90:71`, a `do while` condition |
| `uses aq_hlife` | five reads: two arguments to `decay_temp_adjust` (one is `ch_rtpest.f90:124`), one to `exp_w`, one to `chg_par`, one `if` |
| `writers sedyld` | 21 assignments, each with its statement -- before, statements were kept only for derived-type paths |

### Cost

| | format 5 | format 6 |
|---|---|---|
| `swatplus-facts.json`, 62.0.0 | 7,561,318 B | 7,646,561 B (**+1.13%**) |
| `swatplus-rhs.json`, 62.0.0 | 1,381,499 B | 9,874,393 B |
| ... gzipped | 136,575 B | 1,222,783 B |
| Loading the 62.0.1 pair | 106 ms | 244 ms |
| Full build, 62.0.1, same machine | 4.77 s | 6.68 s |

The sidecar's three sections: `assignments` 2.68 MB (every assignment now,
not only derived-type targets), `reads` 4.67 MB, `statements` 2.52 MB. Reads
live in the sidecar, beside the statements they need, so the base facts that
every consumer loads -- the layouts, the dataselector -- grow by 1%. A query
takes 1 ms for a field and 9 ms for `j`, the most-read name (8,199 reads).

## Formats

`INDEX_FORMAT_VERSION` and `SNAPSHOT_FORMAT` are `6`; `RHS_FORMAT` is `2`.
Format 5 base facts still load, with no `kind` and no `called_at`; an RHS
format-1 sidecar still loads, with no reads -- and then `uses` says the facts
carry no reads rather than "none", which would be the silent wrong answer the
format-1 bundle once gave. `test_the_bundled_snapshot_carries_the_main_program`
and `test_the_bundled_sidecar_carries_reads_and_plain_formulas` assert the
shipped pair has both, not just the version numbers. A rebuild of an unchanged
tree is byte-identical, and the JSON is now written with `\n` on every system.

## Caveats

- **A structure read whole** -- `call f(hru(j))`, `a = hru(j)` -- is recorded
  under the structure's path, not under each field it holds; a bare-name query
  for a field does not see it. Through a dummy argument, the callee's own
  reads of the field are found by name (`uses bmp_sed` matches any parent).
- **A pointer** is read under its own name, not its target's.
- **`call_argument`** says the value is passed, not that the callee reads it:
  an `intent(out)` argument is listed too. Follow `detail` to the routine.
- **An undeclared, implicitly typed variable** is not seen; SWAT+ declares
  with `implicit none`. A component path is a variable whatever its root.
- **One-line `if` assignments** are reads here, but still not `writers`: the
  scanner records no assignment for them (16 component references in their
  targets on 62.0.1).
- **Free-form only**; SWAT+ has no fixed-form source. A computed `go to`,
  `stop` code or statement function reads nothing here.
- **Still no call path**: routines reached only through `interface
  operator(...)` (`aqu_add` and the other `+`, `/`, `*` overloads) and dead
  code -- 120 procedures on 62.0.1, besides `main` itself.
- The template's CMake placeholders (`@SWAT_VERSION@`) sit in string literals
  in SWAT+, so the rewrite never needs to touch them; a placeholder in code
  would be scanned as written.
