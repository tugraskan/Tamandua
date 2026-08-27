#!/usr/bin/env python3
"""Grep-vs-index comparison: measure the bytes an agent pulls into context.

Each question is answered twice. The GREP arm runs the commands a careful agent
would actually run against raw source (locate, then read the region it needs).
The INDEX arm greps the generated index. We count the bytes each arm returns --
that payload is what lands in the model's context, so bytes/4 approximates
tokens.

Grep is given good patterns, not naive ones, and questions are chosen to
include cases where grep should win.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SRC = Path("/home/user/swatplus-src/src")
IDX = Path(__file__).parent / "SWATPLUS_INDEX.md"


def run(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout


def read_window(path: Path, line: int, span: int = 40) -> str:
    """What an agent gets when it reads around a hit."""
    lines = path.read_text(errors="replace").splitlines()
    lo, hi = max(0, line - span // 2), min(len(lines), line + span // 2)
    return "\n".join(lines[lo:hi])


def whole(path: Path) -> str:
    return path.read_text(errors="replace")


CASES = []


def case(name, hard):
    def deco(fn):
        CASES.append((name, hard, fn))
        return fn
    return deco


# ---------------------------------------------------------------- questions

@case("Where is aqu_read defined?", False)
def q1():
    g = run(f"grep -rn 'subroutine aqu_read' {SRC}")
    i = run(f"grep -m1 '^aqu_read|' {IDX}")
    return g, i


@case("Which routine reads aquifer.aqu?", True)
def q2():
    # agent greps the filename, finds the string, must then read the routine
    # to learn the unit number and confirm it is the reader
    g = run(f"grep -rn 'aquifer.aqu' {SRC}")
    g += whole(SRC / "aqu_read.f90")
    i = run(f"grep '^aquifer.aqu|' {IDX}")
    return g, i


@case("What calls hru_control?", False)
def q3():
    g = run(f"grep -rn 'call hru_control' {SRC}")
    i = run(f"grep -m1 '^hru_control|' {IDX} | cut -d'|' -f5")
    return g, i


@case("What does hru_control call?", True)
def q4():
    # grep for call statements inside the file requires having the file
    g = whole(SRC / "hru_control.f90")
    i = run(f"grep -m1 '^hru_control|' {IDX} | cut -d'|' -f4")
    return g, i


@case("What writes sw_volume_begin?", True)
def q5():
    g = run(f"grep -rn 'sw_volume_begin *=' {SRC}")
    i = run(f"grep -m1 '^sw_volume_begin|' {IDX}")
    return g, i


@case("Which routine writes aquifer_day.txt?", True)
def q6():
    g = run(f"grep -rn 'aquifer_day' {SRC}")
    g += whole(SRC / "header_aquifer.f90")
    i = run(f"grep '^aquifer_day' {IDX}")
    return g, i


@case("What loops are in hru_control, with index vars?", True)
def q7():
    g = whole(SRC / "hru_control.f90")
    i = run(f"grep '^hru_control|' {IDX} | grep -E '\\|[0-9]+\\|do '")
    return g, i


@case("Which routines open unit 107?", True)
def q8():
    g = run(f"grep -rn '107' {SRC} | grep -iE 'open|read|write'")
    i = run(f"grep -E '^[^|]+\\|open\\|107\\|' {IDX}")
    return g, i


# ------------------------------------------------------------------- report

def main() -> None:
    print(f"{'question':<48} {'grep':>10} {'index':>9} {'saving':>8}  found")
    print("-" * 88)
    tg = ti = 0
    for name, hard, fn in CASES:
        g, i = fn()
        gb, ib = len(g.encode()), len(i.encode())
        tg += gb
        ti += ib
        save = f"{100 * (1 - ib / gb):.0f}%" if gb else "-"
        ok = "yes" if i.strip() else "NO"
        print(f"{name:<48} {gb:>9,}B {ib:>8,}B {save:>8}  {ok}")
    print("-" * 88)
    print(f"{'TOTAL':<48} {tg:>9,}B {ti:>8,}B {100 * (1 - ti / tg):>7.0f}%")
    print()
    print(f"approx tokens   grep {tg // 4:,}   index {ti // 4:,}")


if __name__ == "__main__":
    main()
