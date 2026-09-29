"""Command line for ``swatplus-layouts``.

Writes the column layout of every SWAT+ input file -- what each read statement
consumes, in order -- as JSON, derived from a facts file:

    swatplus-layouts --facts swatplus-facts.json --out swatplus-layouts.json

With no ``--facts`` it reads the snapshot bundled with the package, so it
needs neither the parser nor a SWAT+ checkout. To follow a checkout, build its
facts first with ``swatplus-build`` (a no-op when they are already current)
and pass that file. ``--file hru-data.hru`` prints one layout.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from tamandua.index.build import IndexError_, SourceIndex
from tamandua.index.layouts import file_layout, layout_json, layouts_json
from tamandua.index.snapshot import load_snapshot

BUNDLED_FACTS = "data/swatplus-facts.json"


def _load(facts: Path | None) -> SourceIndex:
    if facts is not None:
        return load_snapshot(facts)
    resource = resources.files("tamandua").joinpath(BUNDLED_FACTS)
    if not resource.is_file():
        raise IndexError_("no bundled SWAT+ facts file is installed; pass --facts")
    with resources.as_file(resource) as path:
        return load_snapshot(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swatplus-layouts", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--facts", type=Path, default=None,
                        help="facts file from swatplus-build (default: the "
                             "snapshot bundled with this package)")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the JSON here (default: standard output)")
    parser.add_argument("--file", default=None,
                        help="only this input file's layout, e.g. hru-data.hru")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        index = _load(args.facts)
    except (IndexError_, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")

    if args.file is not None:
        layout = file_layout(index, args.file)
        if layout is None:
            parser.exit(1, f"error: nothing in the facts reads {args.file}\n")
        payload = layout_json(layout)
    else:
        payload = layouts_json(index)

    text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
