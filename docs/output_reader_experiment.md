# Reading run output: measured

The index answers questions about code. It cannot answer questions about a run —
those numbers only exist on the machine that produced them. So this is a separate
question with a separate answer: is a tool worth it there?

Tested against the real Ames reference project (`refdata/Ames_sub1`, 25 output
files) with `tamandua/output/reader.py`.

## Correctness first

`surq_gen` across all HRUs:

```
n=12 first=29.899 last=29.231 min=24.082@hru0002 max=33.134@hru0005 trend=declining
```

Independent `awk` check returns `n=12 min=24.082@hru0002 max=33.134@hru0005`.
Exact match.

## Cost

| Question | bash | tool | saved |
|---|---:|---:|---:|
| surq_gen | 746 B | 93 B | **653 B** |
| perc | 34 B | 56 B | −22 B |
| et | 38 B | 88 B | −50 B |
| precip | 37 B | 79 B | −42 B |
| **Total** | 855 B | 316 B | 539 B (63%) |

Break-even against a 710 B three-tool schema: **5.3 questions**.

**The tool loses three of the four.** Once bash has oriented on a file, an `awk`
reduction answers in 34 bytes; the structured summary always reports n, first,
last, min, max and trend whether or not they were asked for. All the saving is in
the first question about a file, where bash pays ~700 B to read the header and
locate a column.

So the efficiency claim is narrow: **the tool wins the first question about a
file, and loses the rest.** An earlier projection of 0.6 questions was wrong — it
assumed a summary tighter than the one actually built.

## The result that does hold

**9 of the 25 Ames output files have a header whose field count does not match
their data rows.** Causes vary: a header declaring more names than are ever
written (`hru_carbon_aa.txt`, 32 names / 11 fields), extra data fields
(`basin_wb_aa.txt`, 49 / 52), and files with an entirely different layout
(`crop_yld_aa.txt`).

On those files, indexing a column by position returns a wrong number with nothing
to indicate it. `awk '{print $11}'` answers confidently and incorrectly.

`read_layout` detects the mismatch and `query` refuses:

```
hru_carbon_aa.txt: header declares 32 names but rows have 11 fields;
                   columns cannot be matched by position
```

That is the honest argument for this tool. Not that it is cheaper — it barely is
— but that it does not silently lie on a third of the corpus.

## Status

Built and tested (14 tests, synthetic layouts plus the real Ames project). Not
wired into the MCP server: the summary should be trimmed first, since as written
it loses every question after the first.
