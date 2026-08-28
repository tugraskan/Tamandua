"""Command line for ``swatplus-build``.

The real implementation lives here rather than in ``scripts/`` so it works from
an installed package, from ``python -m tamandua.index.cli``, and from
the git hooks -- which fall back to ``python -m`` when the console script is not
on PATH.

From inside a SWAT+ checkout, once, with no arguments:

    swatplus-build

That writes the base facts file and its assignment-expression sidecar. Pass
``--no-rhs`` for base facts only. Pass ``--markdown`` to also render the
greppable index and point every assistant's instruction file at it
-- the fallback for a tool that cannot run a server.

Run it again any time -- it is a cheap no-op when the index already matches the
source and parser on disk, and a rebuild when either changed. That is what makes
self-healing practical: an assistant runs this before answering,
unconditionally, without needing to decide whether it is worth it.

The check hashes the *working tree*, not the last commit. Questions get asked
about code that is being edited and has not been committed, which is precisely
when a commit-based check would report a stale index as current.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tamandua.index.build import (
    IndexError_,
    build_source_index,
    index_is_current,
    resolve_source,
)
from tamandua.index.install import (
    FACTS_NAME,
    INDEX_NAME,
    RHS_NAME,
    install_hooks,
    install_pointers,
)
from tamandua.index.render import render_index
from tamandua.index.snapshot import rhs_matches_snapshot, save_rhs, save_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swatplus-build", description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=None,
        help="SWAT+ checkout or its src/ directory (default: the working directory)",
    )
    parser.add_argument(
        "--corpus", type=Path, default=None,
        help="swatplus-reference-corpus checkout (default: installed, or $SWATPLUS_REFERENCE_CORPUS)",
    )
    parser.add_argument(
        "--facts", type=Path, metavar="FILE", default=None,
        help=f"where to write the facts JSON (default: ./{FACTS_NAME}). This is "
             "what the MCP server reads, and what a release publishes",
    )
    parser.add_argument(
        "--no-rhs", action="store_true",
        help=f"do not write the optional assignment-expression sidecar {RHS_NAME}",
    )
    parser.add_argument(
        "--markdown", action="store_true",
        help=f"also write {INDEX_NAME} and the instruction-file pointers into "
             "the checkout, for a tool that cannot run an MCP server",
    )
    parser.add_argument(
        "--install", type=Path, metavar="DIR", default=None,
        help=f"where --markdown writes (default: the source checkout)",
    )
    parser.add_argument("--out", type=Path, default=None,
                        help=f"write only {INDEX_NAME}, to this path")
    parser.add_argument("--hooks", action="store_true",
                        help="also install git hooks that rebuild after commit, "
                             "merge and checkout (optional: it rebuilds on "
                             "demand anyway)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even when the output already matches the source")
    parser.add_argument("--check", action="store_true",
                        help="report whether the output is current; exit 1 if not. "
                             "Does not write anything")
    parser.add_argument("--quiet", action="store_true",
                        help="print nothing on success (used by the git hooks)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    wants_markdown = args.markdown or args.install is not None or args.out is not None
    # Zero-argument default: the facts file for this checkout, and nothing
    # else. The markdown rendering and the instruction pointers are the
    # fallback delivery for tools that cannot run a server, so they are opt-in
    # rather than something a bare invocation drops into someone's repository.
    if args.facts is None and not wants_markdown:
        args.facts = Path.cwd() / FACTS_NAME
    if wants_markdown and args.install is None and args.out is None:
        args.install = args.source or Path.cwd()

    say = (lambda *_: None) if args.quiet else print

    # Cheap path first: hashing costs milliseconds against a multi-second build, so
    # a caller can run this before every question without thinking about it.
    try:
        source_dir = resolve_source(args.source)
    except IndexError_ as exc:
        parser.exit(2, f"error: {exc}\n")

    # Staleness is judged on whichever artifact this invocation is producing.
    if args.facts is not None:
        existing = args.facts
    elif args.install is not None:
        existing = args.install.resolve() / INDEX_NAME
    else:
        existing = args.out

    base_current = bool(
        existing is not None
        and index_is_current(existing, source_dir, args.corpus)
    )
    rhs_path = existing.parent / RHS_NAME if args.facts is not None else None
    sidecar_current = bool(
        args.facts is None
        or (args.no_rhs and rhs_path is not None and not rhs_path.exists())
        or (not args.no_rhs and rhs_path is not None
            and rhs_matches_snapshot(rhs_path, existing))
    )
    if existing is not None and not args.force and base_current and sidecar_current:
        say(f"current  {existing} already matches the source and parser")
        return 0
    if args.check:
        say(f"stale    {existing} does not match the source and parser -- run swatplus-build")
        return 1

    try:
        index = build_source_index(args.source, args.corpus)
    except IndexError_ as exc:
        parser.exit(2, f"error: {exc}\n")

    if args.facts is not None:
        written = save_snapshot(index, args.facts)
        say(f"facts    {written} ({written.stat().st_size / 1024:.0f} KB)")
        if not args.no_rhs:
            rhs = save_rhs(index, written.parent / RHS_NAME)
            say(f"rhs      {rhs} ({rhs.stat().st_size / 1024:.0f} KB)")
        else:
            # Presence is the server's only switch. Leaving an older sidecar
            # here would make --no-rhs ineffective or deliberately mismatch
            # the newly written base snapshot.
            (written.parent / RHS_NAME).unlink(missing_ok=True)

    if wants_markdown:
        text = render_index(index)
        if args.install is not None:
            target = args.install.resolve()
            if not target.is_dir():
                parser.exit(2, f"error: --install target is not a directory: {target}\n")
            index_path = target / INDEX_NAME
            index_path.write_text(text, encoding="utf-8", newline="\n")
            say(f"markdown {index_path}")
            for name, action in install_pointers(target).items():
                say(f"pointer  {target / name} ({action})")
            if args.hooks:
                for event, action in install_hooks(target).items():
                    if event:
                        say(f"hook     .git/hooks/{event} ({action})")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8", newline="\n")
            say(f"markdown {args.out}")

    say(f"source   {index.provenance.source_path}")
    say(f"commit   {index.provenance.source_commit or 'unknown'}")
    say(f"size     {len(index.procedures)} procedures, {len(index.types)} types")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
