<!-- swatplus-mcp -->
## SWAT+ source tools

The `swatplus-source` MCP server indexes this checkout. **Prefer its tools over
searching the source tree** — they answer from static analysis of the whole
tree, so they do not miss occurrences the way a reading pass does.

| To find | Call |
|---|---|
| Where a routine is, what it calls, what calls it | `find_procedure` · `callers` · `callees` |
| Which routine reads or writes a file | `file_io` |
| Which routines use a unit number | `unit_users` |
| What assigns a variable | `writers` |
| **Every loop in a routine, with its index variable** | `loops` |
| A variable from ordinary words, e.g. "lateral flow" | `search_fields` |
| What a derived type contains, with units | `describe_type` |
| How execution reaches a routine | `call_path` |
| Where to break to watch a variable, and on what condition | `breakpoint` · `scope_at` |
| What a run's numbers did | `read_output` |

**`loops` in particular:** reading a long routine by hand reliably misses
loops nested inside conditionals. The tool returns every one.

Grep the source when you need the code itself. Use the tools when you need to
know where something is or what touches it.
<!-- swatplus-mcp -->
