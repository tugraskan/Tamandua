# Decision Record

Decisions still in force. Each is **Accepted**, **Implemented**, or **Open**.

The record was trimmed on 2026-08-26 alongside the chatbot removal: eleven of
the original fourteen decisions (D-1 … D-4, D-6, D-9 … D-14) settled questions
about the MkDocs/CLI assistant — its two-tier design, hosted backend,
streaming, telemetry, provider choice, reference-database scope — and went out
with it. They remain in the archived predecessor repository (https://github.com/tugraskan/SWATPLUS-TACI), should
that work be revisited.

| ID | Decision | Status |
|----|----------|--------|
| D-5 | Dataselector engine extraction | **Open** |
| D-7 | License | **Accepted — MIT** |
| D-8 | Index storage/distribution | **Implemented** (2026-08-26) |

---

## D-8 — Index storage/distribution: **release assets** (Implemented)

**The problem.** Building an index needs a SWAT+ checkout *and* an importable
parser — `swatplus-reference-corpus` today, `swatplus-doc-builder` before it.
Both are private repositories, so this repo's tools were unusable by anyone
outside the team — not because the facts were unavailable, but because the
thing that extracts them was.

That the parser has already been swapped once is itself an argument for this
decision: a snapshot is a stable artifact across a changing build chain.

**The decision.** Split building from serving, and publish the built artifact.

```bash
swatplus-build                                # needs the parser
swatplus-mcp --facts swatplus-facts.json      # needs neither
```

`tamandua/index/snapshot.py` writes `swatplus-facts.json` as plain JSON —
not a pickle, so it can be diffed and read by something that is not this
program. `.github/workflows/release.yml` builds it against the commits pinned
in `docs/pins.toml`, **deletes both checkouts, verifies the snapshot still
answers**, and only then publishes. That last step is the claim worth failing
CI over.

**What made it possible.** `SourceIndex` had been holding the parser's own
procedure objects, of which queries read five attributes. Reducing them to a
`Procedure` record made the index self-contained and serialisable, and dropped
the parser from a runtime dependency to a build-time one.

**Consistent with the neighbours.** `swatplus-dataselector` already publishes
`mcp-server.js` and its schema JSONs as tagged release assets, and says in its
own CI that it does so for downstream consumers like this one.

**Known limit.** `scope_at` and `breakpoint` read the Fortran files themselves,
so on a machine with no source they degrade to "unresolved" rather than
returning something wrong. Every other tool is fully served from the snapshot.

## D-7 — License: **MIT** (Accepted)

MIT (`LICENSE`, `pyproject.toml`), for maximum reuse across the SWAT+ tool
family and minimal friction for research and government users.

## D-5 — Dataselector engine extraction (Open)

Keep the `swatplus-dataselector` relationship as two separate MCP servers run
side by side, or extract a shared engine package?

**Currently: two servers, and the case for merging is weak.** They do not
overlap — this one knows what the Fortran does, that one knows what the input
files hold and what a real dataset contains. Merging would mean either
reimplementing its six tools in Python against its schema JSON (duplicate logic
to keep in sync, which is the drift this project exists to avoid) or wrapping
its Node bundle in a subprocess (a Node dependency on a currently pure-`pip`
install).

The cheap half of "folding in" is not code: name both servers in the
instruction pointer and say which is for which. Availability is not adoption —
an explicit instruction is what changes assistant behaviour, and that is a doc
change, not an architecture.

**Revisit if** a question needs both sides in one answer often enough that
round-tripping through two servers is the bottleneck.
