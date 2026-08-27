"""Put the index where the work happens.

An index in a scratchpad helps nobody. This drops it into a SWAT+ checkout
alongside instruction-file pointers, so an assistant opened on that repo finds
it without being told.

The pointer is not a formality. Measured on both Claude Code and Copilot,
availability alone produced erratic use; an explicit "prefer these" line is
what changed behaviour. Each assistant reads a *different* instruction file,
and one that never sees the pointer never learns the index exists -- so all
three are written, not just CLAUDE.md.
"""

from __future__ import annotations

from pathlib import Path

#: The facts file the MCP server reads -- the normal delivery.
FACTS_NAME = "swatplus-facts.json"

#: The greppable markdown rendering, for a tool that cannot run an MCP server.
#: Opt-in (``--markdown``), not the default.
INDEX_NAME = "SWATPLUS_INDEX.md"

#: Where each assistant looks for repository instructions. Same pointer text
#: to each: the index is one file, and every client greps it the same way.
POINTER_FILES: tuple[str, ...] = (
    "CLAUDE.md",                          # Claude Code
    "AGENTS.md",                          # Codex, and increasingly others
    ".github/copilot-instructions.md",    # GitHub Copilot
)

# Keeps the old name deliberately. These are opaque delimiters, not labels:
# install_pointer finds its own section by matching them, so renaming one
# orphans the section it wraps in every instruction file already written.
_MARKER = "<!-- swatplus-index -->"

#: Git events after which the tree may differ from the index. post-commit and
#: post-merge cover ordinary work; post-checkout covers switching branches,
#: which is where a stale index is least expected and most misleading.
HOOK_EVENTS: tuple[str, ...] = ("post-commit", "post-merge", "post-checkout")

# Same reasoning as _MARKER: an installed hook is found by this string.
_HOOK_MARKER = "# swatplus-index"

_HOOK = f"""#!/bin/sh
{_HOOK_MARKER} -- regenerate the SWAT+ source index after the tree changes.
# Written by `swatplus-build --markdown --hooks`. Delete this file to stop.
#
# Never blocks the git operation: a failure here means a stale index, which the
# provenance block already makes visible, and is not worth failing a commit.
if command -v swatplus-build >/dev/null 2>&1; then
    swatplus-build --markdown --quiet || true
elif command -v python3 >/dev/null 2>&1; then
    python3 -m tamandua.index.cli --quiet >/dev/null 2>&1 || true
elif command -v python >/dev/null 2>&1; then
    python -m tamandua.index.cli --quiet >/dev/null 2>&1 || true
fi
"""


def install_hooks(root: Path, events: tuple[str, ...] = HOOK_EVENTS) -> dict[str, str]:
    """Regenerate the index automatically after the working tree changes.

    Nobody re-runs a build command every time they edit code, so an index that
    depends on being asked for is stale in practice. A hook makes it the git
    operation's problem instead of the author's.

    Refuses to touch a hook this did not write, so a repository with its own
    tooling is left alone. Returns one action per event.
    """
    hooks_dir = root / ".git" / "hooks"
    if not (root / ".git").is_dir():
        return {"": "not a git repository"}
    hooks_dir.mkdir(parents=True, exist_ok=True)

    actions: dict[str, str] = {}
    for event in events:
        target = hooks_dir / event
        if target.exists():
            existing = target.read_text(encoding="utf-8", errors="replace")
            if _HOOK_MARKER not in existing:
                actions[event] = "skipped (not ours)"
                continue
            if existing == _HOOK:
                actions[event] = "unchanged"
                continue
            actions[event] = "updated"
        else:
            actions[event] = "created"
        target.write_text(_HOOK, encoding="utf-8", newline="\n")
        target.chmod(0o755)
    return actions

_POINTER = rf"""{_MARKER}
## SWAT+ source index

`{INDEX_NAME}` in this directory is a generated index of this checkout.

**Before using it, run `swatplus-build --markdown`.** It costs about 14 ms and prints
`current` when the index already matches the source on disk; when it does not,
it rebuilds in about six seconds. Do this even for uncommitted edits -- the
check hashes the working tree, so a file you have changed and not committed
makes the index stale and the rebuild picks it up.

**Then grep it instead of searching the source tree.**

One record per line, `|`-separated. A grep hit arrives without its section
heading, so the field order for each section is repeated here:

```
Procedures   name | file:line | module | calls | called-by
File I/O     file | op | unit=N | routine | lines | variables in scope there
Assignments  variable | routine:line,routine:line,...   (full field path,
             e.g. aqu_d%rchrg -- subscripts stripped)
Loops        routine | line | loop header
```

| To find | Run |
|---|---|
| A routine: where it is, what it calls, what calls it | `grep '^aqu_read\|' {INDEX_NAME}` |
| Which routine reads or writes a file | `grep '^aquifer.aqu\|' {INDEX_NAME}` |
| Which routines use a unit number | `grep '\|unit=107\|' {INDEX_NAME}` |
| What assigns a variable | `grep '^sw_volume_begin\|' {INDEX_NAME}` |
| What computes a derived-type field, wherever it lives | `grep 'rchrg\|' {INDEX_NAME}` |
| Every field of a structure | `grep '^aqu_d%' {INDEX_NAME}` |
| Every loop in a routine, with its index variable | `grep '^hru_control\|' {INDEX_NAME} \| grep '\|do '` |

The loops query returns **every** loop, including nested ones -- reading the
source by hand tends to miss a few.

Measured at ~97% fewer bytes than searching the source for the same answers.

Generated, never edited by hand. `swatplus-build --markdown` is safe to run at any time:
it rebuilds only when the source has changed, so running it before every
question costs nothing when nothing has changed.

`swatplus-build --check` reports staleness without writing anything, exiting 1
when a rebuild is needed. `swatplus-build --markdown --hooks` additionally installs git
hooks that rebuild after commit, merge and checkout -- optional, since the
on-demand rebuild already covers it, and it does nothing for the uncommitted
edits you are most likely asking about.
{_MARKER}"""


def pointer_text() -> str:
    """The CLAUDE.md section that tells an assistant the index exists."""
    return _POINTER


def install_pointers(root: Path, names: tuple[str, ...] = POINTER_FILES) -> dict[str, str]:
    """Write the pointer into every assistant's instruction file.

    Returns one action per file. Directories are created as needed, so
    ``.github/copilot-instructions.md`` works in a checkout that has no
    ``.github`` yet.
    """
    actions: dict[str, str] = {}
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        actions[name] = install_pointer(target)
    return actions


def install_pointer(claude_md: Path) -> str:
    """Add or refresh the index section in a CLAUDE.md.

    Idempotent: an existing section between the markers is replaced, so
    regenerating never stacks duplicates, and anything else in the file is
    left alone.
    """
    new = pointer_text()
    if not claude_md.exists():
        claude_md.write_text(new + "\n", encoding="utf-8", newline="\n")
        return "created"

    existing = claude_md.read_text(encoding="utf-8")
    if _MARKER in existing:
        start = existing.index(_MARKER)
        end = existing.index(_MARKER, start + len(_MARKER)) + len(_MARKER)
        updated = existing[:start] + new + existing[end:]
        if updated == existing:
            return "unchanged"
        claude_md.write_text(updated, encoding="utf-8", newline="\n")
        return "updated"

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    claude_md.write_text(existing + separator + new + "\n", encoding="utf-8", newline="\n")
    return "appended"
