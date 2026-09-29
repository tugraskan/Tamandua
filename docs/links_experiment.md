# Input-file links from the facts

**Question.** Can the index say which input-file column names a row in another
input file -- a link, or foreign key -- from the SWAT+ Fortran alone, well
enough for the dataselector to stop taking its links from a static schema
extracted from swatplus-editor?

**Answer.** Yes, where the source shows the search, and that is most places.
SWAT+ never declares a link; it finds rows at run time by comparing names:

```fortran
do ilum = 1, db_mx%landuse
  if (hru_db(i)%dbsc%land_use_mgt == lum(ilum)%name) then
```

The index now keeps every such comparison as a fact, and derives from it that
`hru-data.hru`'s `land_use_mgt` names a row of `landuse.lum` by its `name`,
evidence `hru_read.f90:71`. On SWAT+ 62.0.0 that gives **163 links** from 159
comparison sites. **75** file pairs are in both Tamandua's links and the
editor schema; on 73 of them the two name the same column, and in the other
two the editor has a column SWAT+ never compares (`urb_ro`) or names a
different one from the one the code searches with (`nut` where it uses
`hydc`). Of the 113 sites a simple grep for `if (x == a(i)%name)` finds,
**105** become links and the other 8 say why not.

Script: `scripts/measure_links.py`. Code: `Comparison` and `Copy` in
`tamandua/index/build.py`; `input_links` and `Reference` in
`tamandua/index/layouts.py`; `swatplus-layouts --links`.

## The facts (format 5)

Two sections were added to the facts, and nothing else in them moved.

**`comparisons`** -- every `==`/`.eq.` or `/=`/`.ne.` test between two
variables in an `if` or `else if` condition, per procedure: the line, both
operands as written (whitespace and `trim`/`adjustl`/`adjustr` removed,
subscripts kept), the operator, and the enclosing loops by index variable.
Conditions are split on `.and.`/`.or.`/`.not.` first, so each conjunct is its
own record; literals, numbers, expressions and intrinsic calls are not
variables and are not kept. Both operands must be declared in scope.

Some comparisons are made in a called routine on its dummy arguments.
`search` is SWAT+'s binary search over a sorted name array, and every `.con`
file reaches `weather-sta.cli` through it:

```fortran
call search (wst_n, db_mx%wst, ob(i)%wst_c, ob(i)%wst)   ! hyd_read_connect.f90:338
...
if (sch(nn) == cfind) then                               ! search.f90:22
```

Each such test is restated at every call with what the call passes -- an
array handed whole becomes `wst_n(:)`, every element -- and stored in the
caller with `via: "search:22"`. This is the same argument association
`argument_filenames` already does for filenames; one level only.

**`copies`** -- assignments of one variable to another (`=`, and pointer
association `=>`) into anything a comparison tests, or a structure holding it,
followed up to three hops back. `mgt = sched(isched)%mgt_ops(...)` is why
`mgt%op_char` is a column of `management.sch`; `wst_n(i) = wst(i)%name` is why
the `search` above is a search of `weather-sta.cli`.

| On 62.0.0 (`de210d6`, parser `110c2a2`) | |
|---|---|
| Comparisons in `if`/`else if` conditions | **389**, in 147 procedures: 352 `==`, 37 `/=` |
| ... restated at a call | **122** (9 through `search`; the rest mostly `cond_real`/`cond_integer` tests of a value against a limit, none a search) |
| Copies | **277**, into 137 paths |
| Snapshot cost | **+150,941 bytes, +2.04%** (7,410,377 → 7,561,318) |

The rebuild changed exactly: the two new sections, `snapshot_format` and
`index_format` 4 → 5, `format_version` and `generated_at` in the provenance,
and one I/O row (below). Procedures, loops, writers, types, module variables,
call paths and scanner warnings are identical to the previous bundle, the
source fingerprint is unchanged, and the RHS sidecar is byte-identical. A
second rebuild was identical apart from `generated_at`.

Format 4 and older still load, with no comparisons, no copies and so no links;
`test_the_bundled_snapshot_carries_comparisons` guards the shipped file the
way the format-3 and format-4 tests do.

## The rule

A comparison shows a link when all of these hold. Each part is a fact in the
index; nothing is matched by name.

1. It tests `==`.
2. One side is **searched**: walking the enclosing loops from the innermost,
   the first loop whose index subscripts exactly one side marks that side, and
   that loop's index must subscript its **root** array -- `lum(ilum)` under
   `do ilum`. Or a called routine was handed the side as a whole array
   (`wst_n(:)`).
3. The loop is not the one reading the searched array's own rows: a test
   inside `do i; read ... hru_db(i)` checks each row as it arrives; it looks
   nothing up.
4. Both sides hold a **column** some input file is read into -- any read of
   the file, not only the layout's records, since `pcp.cli` reads its station
   names into `pcp_n(i)` in a pass the layout does not show. A module variable
   is the same variable everywhere; a local matches only its own routine's
   reads, and only the nearest read before the comparison (gwflow_read reads
   `dum_id` from ponds.gw and then from pond_cell.gw).
5. Or it holds one **through copies**: every assignment to the path, or to a
   structure holding it, is a stored copy, and all of them lead back to one
   column. A copy back from itself adds nothing (`hru = hru_init` after
   `hru_init = hru`).
6. The two sides are not the same column.

The link is attached to the source column of every layout record it appears
in, as a `references` entry: target file, column, path, the target's record
role and 1-based position, `evidence` (each comparison's `file.f90:line`) and
`through` (the called routine's test and each copy followed).

```json
{"file": "wetland.wet", "column": "name", "path": "wet_dat_c%name",
 "role": "main", "position": 2,
 "evidence": ["wet_initial.f90:43"],
 "through": ["cal_allo_init.f90:69", "hrudb_init.f90:23", "re_initialize.f90:20"]}
```

`swatplus-layouts --links` lists every link with its evidence, and every
comparison that shows none with the reason.

## Results on 62.0.0

| | |
|---|---|
| Links (source column → target column) | **163**, from 159 comparison sites |
| ... through `search()` | 20 (12 `.con` → `weather-sta.cli`; 8 `weather-sta.cli` → weather files) |
| ... through a copy | 18 |
| File pairs | 161 |
| Input files with a column that references another | 51 |
| Comparisons that show no link | 352 |

Why the 352 do not link:

| Count | Reason |
|---|---|
| 152 | no enclosing loop runs over one side only (time and counter tests: `time%yrc == pco%yrc_end`) |
| 95 | the loop runs over part of a row, not the rows (`pcomdb(icom)%pl(ipl)%cpnm`, the plants of one community) |
| 85 | an inequality |
| 9 | assigned from more than one column (`d_tbl` points at four decision tables) |
| 8 | not a column any input file is read into (`plts_bsn`, built at run time) |
| 3 | both sides hold the same column |

**Against the grep the work started from.** `if (<path> == <array>(<i>)%name)`,
either way round, finds 113 sites in 32 files. 105 are links. The 8 that are
not: five `d_tbl%act(iac)%file_pointer` tests in `actions.f90` and one
`d_tbl%act(iac)%option` (`d_tbl` is a pointer aimed at `dtbl_lum`, `dtbl_flo`,
`dtbl_res`, `dtbl_scen` -- and `sched%auto_crop` -- at different call sites,
so which file's column it holds is not one answer); `actions.f90:885`, where
`wet_dat%name` is filled from `wet_dat_c`, not read; and `conditions.f90:682`,
a loop over one table's conditions.

**Every target is a key.** The rule does not assume the searched column is the
one that names its rows; the script checks it held. Every distinct target is
its record's first character column, except `ponds.gw`'s integer `id`, which
is its first column.

## Against the editor schema

Baseline: `resources/schema/swatplus-editor-schema.json` →
`tables.<file>.foreign_keys` in `tugraskan/swatplus-dataselector` at v0.2.0
(`0c73c64`), 190 foreign keys. It is a comparison only; nothing from it
reaches Tamandua. File names are compared the way the dataselector matches
files to tables -- lowercased, `-` read as `_` -- and, in a second view, with
two of the editor's names read as the files SWAT+ opens: `d_table.dtl` as the
four `.dtl` files (the editor keeps every decision table in one model) and
`cons-prac.lum` as `cons_practice.lum` (the name Ames, which the editor wrote,
holds).

| File pairs | strict | aliased |
|---|---|---|
| Tamandua | 161 | 161 |
| Editor | 185 | 201 |
| **Both** | **75** | **79** |
| Only the editor | 110 | 122 |
| Only Tamandua | 86 | 82 |

**Where both have the pair, they almost always agree on the column.** 46
(strict) match by name: the editor's column is the Fortran name, or the name
the Ames file's header gives the column at the layout's position (`lu_mgt` for
`land_use_mgt`). The other 29 were read by hand:

- 26 are the same column under two names, with no Ames header to map by:
  `wst` / `wst_c` in six `.con` files (`hru.con`, in Ames, matches by header,
  and all twelve are read by one statement, `hyd_read_connect.f90:298`);
  `hmet`, `om`, `path`, `pest`, `salt` / `*_file` in `delratio.del` and
  `exco.exc`; `topo`, `field`, `dlr`, `plnt_name`, `plnt_typ`, `soil_text`,
  `cal_parm`; and three of the four `channel-lte.cha` pairs (`hyd`/`hydc`,
  `ini`/`initc`, `cha_nut`/`nutc`, the editor carrying a second name for the
  first two).
- `soil_plant.ini → nutrients.sol`: the editor's `nutrients` is the column
  Ames heads `nut`; Tamandua's `nutc` is the same column, the only one
  compared against `nutrients.sol`.
- `landuse.lum → urban.urb`: both have `urb_lu` (headed `urban`). The editor
  also makes `urb_ro` a key to `urban.urb`; in SWAT+ it is the urban runoff
  method, a `select case` subject (`hru_urban.f90:91`), never compared with a
  row.
- `channel-lte.cha → sed_nut.cha`: the editor says the `nut` column; SWAT+
  62.0.0 searches `sed_nut.cha` with `hydc`, the same column it searches
  `hyd-sed-lte.cha` with (`sd_channel_read.f90:274` and `:282`). The facts
  report what the model does; whether that is intended is a question for
  SWAT+.

**Only the editor (strict, 110).** 98 involve a table SWAT+ reads under no
such name: the editor's database-only tables (`*.item`, `*.val`, `*.col`,
`gwflow-*.txt`, `management-sch.op`, ...) and differently named files. The 12
where SWAT+ reads both files:

- `aquifer.aqu → initial.aqu`, and `initial.aqu →` `om_water.ini`,
  `path_water.ini`, `pest_water.ini`, `salt_aqu.ini`: 62.0.0 does not resolve
  these. `aqu_read_init` reads `initial.aqu` and its organic-mineral lookup is
  commented out ("initializing organics in aqu_initial - do it here later");
  `aqu_ini` is looked up only in `initial.aqu_cs`, which Tamandua links.
- `initial.cha → path_water.ini`, `salt_channel.ini`: SWAT+ reads those
  columns from `initial.cha_cs`, and Tamandua links them from there.
- `channel-lte.cha → sediment.cha`: 62.0.0 declares `sedc` and never compares
  it with anything.
- `septic.str → septic.sep`: SWAT+ uses `typ` as an index,
  `sepdb(sep(isep)%typ)` -- a link, but not by name. Not covered (below).
- `salt_fertilizer.frt → fertilizer.frt`: rows line up by position
  (`fert_salt(ifrt)` beside `fertdb(ifrt)`). Not covered.
- `object.prt → print.prt`, `rout-unit.ele → rout-unit.rtu`: no comparison
  joins them; routing-unit elements are assigned by `rout_unit.def`'s
  numbers.

**Only Tamandua (strict, 86).** 72 involve a file the editor has no table
for. Half of those, 36, involve a decision table under the name SWAT+ opens
it by (`lum.dtl`, `flo_con.dtl`, `res_rel.dtl`, `scen_lu.dtl`; the editor
keeps them as one `d_table.dtl`), among them all twelve `.con` files'
`ruleset` → `flo_con.dtl`. The other 36 involve the constituent files
(`initial.aqu_cs`, `initial.cha_cs`, `reservoir.res_cs`, `wetland.wet_cs`,
`cs_hru.ini`, `cs_res`, `salt_res`, `cs_urban`, `salt_urban`), five `.con`
files the editor has no table for, `transplant.plt`, `puddle.ops`, the
`manure*.frt` files, `pond_cell.gw` → `ponds.gw`, `res_conds.dat`,
`atmodep.cli`, `pet.cli` and a few more. 14 are between files the editor has
but does not link: the twelve `management.sch` operation columns (`op_char`,
`op_plant` → `plant.ini`, `plants.plt`, `tillage.til`, `harv.ops`, `irr.ops`,
`fertilizer.frt`, `chem_app.ops`, `pesticide.pes`, `graze.ops`, `fire.ops`,
`sweep.ops`, `weir.res`; the editor stores them as free text), and
`constituents.cs` → `pesticide.pes`, `pathogens.pth`.

**Following the source.** On the current tree (`97ca231`, same parser) the
same run gives 173 links. Nine 62.0.0 links went with the files that went
(`transplant.plt` → `transplant.ops`, the `.wal` organic-mineral columns
reorganised under `conc`), and 19 arrived with new files and columns
(`place_of_use.wro`'s decision tables, `wtps_wuses.wal`, `recall.rec`'s
`filename` → `exco.exc`). One new target is not a first column:
`water_treatment_read.f90:83` and two siblings search `recall_db.rec` by its
`org_min` column rather than its `name` -- what the code does, flagged by the
check above.

## Two fixes to the layouts

Both change layouts other consumers already read, and both came from following
a link to a column that was missing.

- **A name read once per row is a record, not preamble.** A one-string read
  was taken for a title or header while no record had been seen. That is right
  for `titldum`, and wrong for `read (107,*) pest_soil_ini(ipesti)%name`
  inside `do ipesti`, which names each block that follows. Now a read whose
  variable is subscripted by an enclosing loop's index is data. Seven layouts
  changed (`pest_hru.ini`, `path_hru.ini`, `pest_water.ini`,
  `path_water.ini`, `hmet_hru.ini`: `data_starts_after` 3 → 2; `cs_hru.ini`,
  `salt_hru.ini`: 6 → 5), and **six files gained a layout they never had**:
  `pcp.cli`, `tmp.cli`, `slr.cli`, `hmd.cli`, `wnd.cli`, `pet.cli`, whose every
  read had been a one-string read. On Ames, files with a layout go 44 → 46.
- **A reader called by the opener reads child records.** `read_mgtops` reads
  each schedule's operation lines on unit 107, which it never opens; its
  caller `mgt_read_mgtops` has `management.sch` open there when it calls. The
  scanner filed the statement under `unit_107`, so `management.sch` had no
  operation layout. The build now takes the file a unit is open on at every
  call site of a routine that reads it without opening it
  (`caller_opened_files`: all call sites must agree, or the unit is kept).
  On 62.0.0 that moves exactly one I/O row. The layout then makes a reader
  that never opens the file, called by one that does, a source of `child`
  records rather than a competing main.

With `references` set aside, every other layout is identical to what the
previous code derived from the previous bundle, and `unit_107` is gone;
`docs/layouts_experiment.md` has the updated figures.

## Caveats

- **One-line `if` assignments are invisible.** The parser records no
  assignment inside `if (...) x = y`, so neither `writers` nor `copies` sees
  one -- 723 of them in 62.0.0. A copy is followed only when every assignment
  to the path is a copy, so a hidden writer could make a link through copies
  wrong. `--source` re-checks this on the Fortran: none of the 32 paths the
  copy-based links rely on is written by a one-line `if`.
- **Nor are writes through a call.** A routine that fills its `intent(out)`
  argument writes the caller's variable with no assignment in the caller, and
  neither the facts nor the script look for one.
- **"Nearest read before" is source order**, not control flow. A local read
  later in a loop body reaches the top of the next iteration; no link in
  62.0.0 depends on that.
- **Links by index or by position are not covered.** `sepdb(sep(isep)%typ)`
  uses a column as a row number; `fert_salt(ifrt)` aligns two files row by
  row. Neither is a comparison.
- **A pointer aimed at several tables is not resolved.** `d_tbl` accounts for
  six of the eight unlinked grep sites. Which table it points at depends on
  the caller; the facts could carry that per call path, and do not.
- **Call-through is one level.** A routine passing its own dummies on to
  `search` would not be followed.
- **The key side is inferred from the loop.** A loop scanning rows to filter
  them by a foreign-key column would read as a link the wrong way round.
  Rule 3 removes the case inside a reader; the first-text-column check found
  none elsewhere in 62.0.0.
- **Column names differ between the two schemas** (`wst` / `wst_c`), as they
  do for layouts; a consumer maps by position, which the reference carries.
