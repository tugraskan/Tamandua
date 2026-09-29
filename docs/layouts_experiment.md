# Input-file layouts from the facts

**Question.** Can the column layout of each SWAT+ input file -- what the model
reads, in order -- be derived from the facts alone, well enough for a consumer
(the dataselector's schema first) to build on instead of a static schema
extracted once from swatplus-editor?

**Answer.** Yes, for the model's side of it. On Ames, every file with a plain
data row is read exactly as the layout says, and the layouts follow the source:
between 62.0.0 and `97ca231` ten files' layouts changed, which a schema frozen
at one editor commit cannot notice. The layout is what SWAT+ *reads*, not
everything a file holds, and it does not carry header names -- see Caveats.
Links to other files' rows, where the source shows them, are in
`links_experiment.md`.

Script: `scripts/measure_layouts.py`. Code: `tamandua/index/layouts.py`,
`swatplus-layouts`.

## Method

A layout joins two facts the index already had. `IOUse.fields` is the variable
list of each read statement; `DerivedType.fields` gives each type's components
in declaration order, with type, units and inline description. A list-directed
read of `hru_db(i)%dbsc` consumes every component of `hru_databases_char` in
that order, so the read statement and the type together are the layout.

Per file and reader:

- Only the pass after the last `rewind` counts; SWAT+ commonly counts records
  in a first pass and reads them in a second.
- A read followed by a `backspace` before the next read is a peek, not a record.
- Leading scalar metadata is the preamble (titles, counts and headers), with
  text and value lines distinguished, so `data_starts_after` is how many
  there are.
- Of the remaining row-shaped reads, the one at the shallowest loop depth with
  the most columns is `main`; deeper reads are `child` (soil layers, monthly
  weather); a read whose variable list begins the same way as `main`'s is an
  `alternative`; anything else at `main`'s depth is a `line`.
- Whole structures expand component by component; fixed arrays expand by their
  extent, including a `parameter` extent (`dimension(mlyr)`); arrays sized at
  run time and implied-do groups become columns marked `repeat`.

Measured with `scripts/measure_layouts.py --dataset <Ames_sub1> --schema
<dataselector swatplus-editor-schema.json>`: for each dataset file with a
layout, the main record's column count against the first data row's value
count.

## Two build fixes this needed

Both change which file an I/O statement is filed under, so both are in
`tamandua/index/build.py` and the bundled snapshot was rebuilt.

- **Filenames passed as arguments.** `hyd_read_connect(con_file, ...)` opens
  its dummy argument, so all twelve connectivity files were filed under
  `con_file` and `hru.con` answered nothing. `argument_filenames` follows each
  call site's actual argument through the same default-filename map a direct
  `open` uses. On 62.0.0: the twelve `.con` files now resolve by name; three
  other dummy names (`destination`, `source`, `lsu_elem_upd`/`ru_elem_upd`)
  resolve to their callers' expressions, checked against `actions.f90:960`;
  I/O rows 7,099 → 7,176.
- **`backspace 107` without parentheses.** The scanner reports no unit for the
  bare form, so the statement was dropped. There are two in 62.0.0
  (`soil_db_read.f90:64`, `soils_init.f90:222`) against 326 parenthesised; the
  first is the backspace in the peek-then-reread of every soil record. The
  unit is now read from the statement and the file taken from the unit's last
  binding in the same procedure.

## Results

| | 62.0.0 (`de210d6`) | current (`97ca231`) |
|---|---|---|
| Files with a layout | 257 | 260 |
| Main record complete | 257 of 257 | 260 of 260 |
| Ames files | 109 | 110 |
| ... with a layout | **46** | 47 |
| ... in the dataselector's static schema | 37 | 37 |
| Reads every value of the first row | 27 | 28 |
| Reads a leading run; trailing values unread | 9 | 9 |
| Repeating group (count set at run time) | 5 | 5 |
| Reads more values than the row holds | 2 | 2 |
| No data row in the file | 3 | 3 |

Revised the same day, with the links work (`links_experiment.md`), for two
layout defects. A name read once per row (`read pest_soil_ini(ipesti)%name`
inside `do ipesti`) had been taken for preamble: seven files' records gain it
and their `data_starts_after` drops by one, and six files that read nothing
else -- `pcp.cli`, `tmp.cli`, `slr.cli`, `hmd.cli`, `wnd.cli`, `pet.cli` --
have a layout for the first time; on Ames that is `pcp.cli` and `tmp.cli`
(44 → 46), and `cs_hru.ini` and `salt_hru.ini` move from a repeating group to
a name line with no data row. And `management.sch`'s operation lines, read by
`read_mgtops` on a unit its caller opened, are now under `management.sch` as
a child record instead of a `unit_107` layout of their own (252 + 6 - 1 =
257). Every other layout is unchanged.

The 9 with unread trailing values are `cntable.lum` (5 of 8), `cons_practice.lum`
(3/4), `fertilizer.frt` (6/8), `filterstrip.str` (5/6), `grassedww.str` (8/9),
`graze.ops` (6/7), `ovn_table.lum` (4/5), `soils.sol` (7/10) and `tillage.til`
(6/7): SWAT+ stops once its variables are filled, and the tail is usually
`description`.

The 2 that read more than the row holds are both explained:

- `soil_plant.ini` -- main reads 8, Ames has 7. `soil_plant_init` reads the
  record one of two ways on `bsn_cc%nam1`; the 7-column form is the layout's
  `alternative` at `soil_plant_init.f90:47`. The layout is right; which branch
  a dataset uses is not a fact the index holds (below).
- `file.cio` -- each line is a different classification read by its own
  statement; "main" is simply the widest. A consumer should keep treating
  `file.cio` specially.

Between 62.0.0 and `97ca231`, 10 files' records changed -- `parameters.bsn`
(`spcon`, `spexp` → `pestgwfact`, `temp_decay`), `pesticide.pes` (+
`harvest_sink`, `resistance_n`), `recall_db.rec`, `shade_factor.shf`
(`lsu` → `cha`), five `water_*.wal` files, and `file.cio` -- while 8 files
appeared and 5 went (`transplant.plt` → `transplant.ops`, `out_src.wal` →
`outside_src.wal`, `water_allocation.wro` → `place_of_use.wro` /
`point_of_diver.wro`, ...). A full build of that tree took 4.7 s; deriving
every layout from a loaded snapshot takes about half a second.

## Caveats

- **Header names are not Fortran names.** Of 361 columns read across the 34
  Ames files with plain rows, 120 share the header's name at the same
  position, and in 2 files every column does (`land_use_mgt` vs `lu_mgt`,
  `fertnm` vs `name`). A consumer maps by position -- safe here because this is
  the read order, unlike the editor schema's column order -- or keeps its own
  names.
- **Foreign keys are not declared.** A column naming a row elsewhere is
  matched by a runtime string comparison. The comparisons are now facts, and
  where one shows the search the column carries a `reference`; see
  `links_experiment.md` for what that finds and what it cannot.
- **Alternatives do not say when.** The parser records each I/O statement's
  enclosing condition (`IOOperation.condition`), but `IOUse` does not carry it,
  so `alternative` says "read two ways" and not "`bsn_cc%nam1 == 0`". Storing
  it is a facts-format change and was left for when a consumer needs it.
- **Records read by a called routine need every call to agree.**
  `management.sch`'s operation lines are read in `read_mgtops` on a unit
  `mgt_read_mgtops` opened; the build now takes the file the unit is open on
  at every call site, and keeps the unit when any call site does not have it
  open. That is the only such read in 62.0.0; the other `unit_...` layouts are
  internal reads of a string (`read(split_fields(2),*)`), not files.
- **A conditional backspace looks like a peek.** `mgt_read_mgtops` re-reads an
  auto line only for `pl_hv_*` names; the layout keeps the re-read form.
- **Filenames named in another file stay expressions.** Weather data files are
  keyed `pcp(i)%filename` etc., with `filename_is_default: false`.
- **Generic line readers have no layout.** The `self%file_name` class reads
  whole lines as text; nothing reads columns, so nothing is reported.
