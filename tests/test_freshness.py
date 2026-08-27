"""A long-running MCP process must not silently serve stale facts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tamandua.index import Provenance, SourceIndex, build_source_index, save_snapshot
from tamandua.mcp.server import Current

CORPUS = os.environ.get("SWATPLUS_REFERENCE_CORPUS")


def _corpus_available() -> bool:
    try:
        import swatplus_reference  # noqa: F401
    except ImportError:
        return bool(CORPUS and (Path(CORPUS) / "src").is_dir())
    return True


requires_corpus = pytest.mark.skipif(
    not _corpus_available(), reason="no swatplus-reference-corpus"
)


def _index(fingerprint: str = "aaa", source_path: str = "/nowhere") -> SourceIndex:
    return SourceIndex(provenance=Provenance(
        source_path=source_path,
        source_commit=None,
        source_describe=None,
        source_fingerprint=fingerprint,
        generated_at="2026-08-27T00:00:00Z",
        format_version="1",
        parser_commit=None,
    ))


def test_a_rebuilt_facts_file_is_picked_up(tmp_path: Path) -> None:
    facts = save_snapshot(_index("before"), tmp_path / "f.json")
    current = Current(_index("before"), facts=facts)

    save_snapshot(_index("after"), facts)
    os.utime(facts, ns=(1_000_000_000, 1_000_000_000))

    assert current.get().provenance.source_fingerprint == "after"


def test_an_unchanged_file_is_not_reloaded(tmp_path: Path) -> None:
    facts = save_snapshot(_index("same"), tmp_path / "f.json")
    current = Current(_index("same"), facts=facts)
    assert current.get() is current.get()


def test_a_half_written_file_keeps_the_previous_answer(tmp_path: Path) -> None:
    facts = save_snapshot(_index("good"), tmp_path / "f.json")
    current = Current(_index("good"), facts=facts)
    facts.write_text('{"provenance": {"source_', encoding="utf-8")
    os.utime(facts, ns=(2_000_000_000, 2_000_000_000))
    assert current.get().provenance.source_fingerprint == "good"


def test_a_deleted_file_keeps_the_previous_answer(tmp_path: Path) -> None:
    facts = save_snapshot(_index("good"), tmp_path / "f.json")
    current = Current(_index("good"), facts=facts)
    facts.unlink()
    assert current.get().provenance.source_fingerprint == "good"


def test_a_future_format_keeps_the_previous_answer(tmp_path: Path) -> None:
    facts = save_snapshot(_index("good"), tmp_path / "f.json")
    current = Current(_index("good"), facts=facts)
    payload = json.loads(facts.read_text(encoding="utf-8"))
    payload["snapshot_format"] = "99"
    facts.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(facts, ns=(3_000_000_000, 3_000_000_000))
    assert current.get().provenance.source_fingerprint == "good"


def test_bundled_mode_never_chases_its_provenance_path(tmp_path: Path) -> None:
    """A release snapshot names its build machine, not a live source mode."""
    (tmp_path / "a.f90").write_text("subroutine changed\nend\n", encoding="utf-8")
    original = _index("not-the-tree-hash", source_path=str(tmp_path))
    current = Current(original)
    assert current.get() is original


@requires_corpus
def test_editing_source_rebuilds_without_a_restart(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.f90").write_text(
        "      subroutine alpha\n      x = 1\n      end subroutine alpha\n",
        encoding="utf-8",
    )
    corpus = Path(CORPUS) if CORPUS else None
    current = Current(build_source_index(src, corpus), source=src, corpus=corpus)
    assert current.get().procedure("beta") is None

    (src / "b.f90").write_text(
        "      subroutine beta\n      y = 2\n      end subroutine beta\n",
        encoding="utf-8",
    )
    assert current.get().procedure("beta") is not None


@requires_corpus
def test_untouched_source_is_not_reparsed(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.f90").write_text(
        "      subroutine alpha\n      x = 1\n      end subroutine alpha\n",
        encoding="utf-8",
    )
    corpus = Path(CORPUS) if CORPUS else None
    current = Current(build_source_index(src, corpus), source=src, corpus=corpus)
    assert current.get() is current.get()


def test_a_source_that_disappears_keeps_the_previous_answer(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    original = _index("known", source_path=str(src))
    current = Current(original, source=src)
    src.rmdir()
    assert current.get() is original
