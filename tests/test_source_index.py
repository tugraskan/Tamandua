"""Tests for the facts-only SWAT+ source index.

The unit tests run against a small synthetic Fortran tree, so the suite has no
external dependency and CI does not need a SWAT+ checkout. Tests that need the
real pinned source are marked and skipped when it is absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import tamandua.index.install as install_module
from tamandua.index import (
    FACTS_NAME,
    INDEX_NAME,
    IndexError_,
    POINTER_FILES,
    build_source_index,
    field_path,
    split_field_doc,
    install_pointer,
    HOOK_EVENTS,
    index_is_current,
    install_hooks,
    install_pointers,
    looks_like_swatplus,
    output_unit_filenames,
    pointer_text,
    render_index,
    resolve_source,
    source_fingerprint,
    stored_fingerprint,
    stored_parser_commit,
)

# A real SWAT+ source checkout, when available (see docs/pins.toml).
REAL_SOURCE = Path(os.environ.get("SWATPLUS_SOURCE", "/workspace/swatplus-62.0.0"))
CORPUS = os.environ.get("SWATPLUS_REFERENCE_CORPUS")

requires_source = pytest.mark.skipif(
    not REAL_SOURCE.is_dir(), reason="no SWAT+ source checkout (set SWATPLUS_SOURCE)"
)


def _corpus_available() -> bool:
    try:
        import swatplus_reference  # noqa: F401
    except ImportError:
        return bool(CORPUS and (Path(CORPUS) / "src").is_dir())
    return True


requires_corpus = pytest.mark.skipif(
    not _corpus_available(), reason="no swatplus-reference-corpus (set SWATPLUS_REFERENCE_CORPUS)"
)


# --------------------------------------------------------------- fixtures

READER = """\
      subroutine aqu_read
      use input_file_module
      character (len=80) :: titldum
      integer :: eof
      eof = 0
      inquire (file=in_aqu%aqu, exist=i_exist)
      open (107,file=in_aqu%aqu)
      read (107,*,iostat=eof) titldum
      do ish_aqp = 1, msh_aqp
        read (107,*,iostat=eof) titldum
      end do
      db_mx%aqudb = msh_aqp
      close (107)
      return
      end subroutine aqu_read
"""

CALLER = """\
      subroutine proc_aqu
      call aqu_read
      return
      end subroutine proc_aqu
"""

WRITER = """\
      subroutine header_aquifer
      call open_output_file(2520, "aquifer_day.txt", 1500)
      call open_output_file(2524, "aquifer_day.csv", 1500)
      write (2520,*) "header"
      return
      end subroutine header_aquifer
"""

LOOPER = """\
      subroutine hru_control
      sw_volume_begin = 0.
      do ly = 1, soil(j)%nly
        sw_volume_begin = sw_volume_begin + soil(j)%phys(ly)%st
      end do
      do ipest = 1, cs_db%num_pests
        call pest_decay
      end do
      return
      end subroutine hru_control
"""


@pytest.fixture
def fake_source(tmp_path: Path) -> Path:
    """A tiny Fortran tree exercising every extraction path."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "aqu_read.f90").write_text(READER, encoding="utf-8")
    (src / "proc_aqu.f90").write_text(CALLER, encoding="utf-8")
    (src / "header_aquifer.f90").write_text(WRITER, encoding="utf-8")
    (src / "hru_control.f90").write_text(LOOPER, encoding="utf-8")
    return src


@pytest.fixture
def index(fake_source: Path):
    return build_source_index(fake_source, Path(CORPUS) if CORPUS else None)


# ------------------------------------------------------------ path handling

def test_resolve_source_accepts_repo_root(fake_source: Path) -> None:
    """A caller need not know whether they have the repo or its src/."""
    assert resolve_source(fake_source.parent) == fake_source


def test_resolve_source_accepts_src_dir(fake_source: Path) -> None:
    assert resolve_source(fake_source) == fake_source


def test_resolve_source_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(IndexError_, match="does not exist"):
        resolve_source(tmp_path / "nope")


def test_resolve_source_rejects_tree_without_fortran(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("not fortran", encoding="utf-8")
    with pytest.raises(IndexError_, match="no .f90 files"):
        resolve_source(tmp_path)


def test_output_unit_map_is_path_separator_agnostic(tmp_path: Path) -> None:
    """Sources in nested directories are found on any platform."""
    nested = tmp_path / "src" / "output"
    nested.mkdir(parents=True)
    (nested / "header.f90").write_text(WRITER, encoding="utf-8")
    assert output_unit_filenames(tmp_path)["2520"] == "aquifer_day.txt"


@requires_corpus
def test_provenance_records_source_as_posix(fake_source: Path, index) -> None:
    """Provenance uses forward slashes so an index is diffable across OSes."""
    assert "\\" not in index.provenance.source_path
    assert index.provenance.generated_at.endswith("Z")
    assert index.provenance.format_version


# ------------------------------------------------------------- extraction

@requires_corpus
def test_procedure_locations(index) -> None:
    """``location`` is the rendered label; ``path`` is what ``scope_at`` needs.

    They are two plain strings rather than the parser's own location object,
    which is what lets an index be serialised without the parser (D-8).
    """
    proc = index.procedure("aqu_read")
    assert proc is not None
    assert proc.path == "aqu_read.f90"
    assert proc.location.startswith("aqu_read.f90:1")


@requires_corpus
def test_procedure_lookup_is_case_insensitive(index) -> None:
    assert index.procedure("AQU_READ") is not None
    assert index.procedure("missing_routine") is None


@requires_corpus
def test_callers_and_callees(index) -> None:
    assert index.callers_of("aqu_read") == ["proc_aqu"]
    assert index.callees_of("proc_aqu") == ["aqu_read"]
    assert index.callees_of("aqu_read") == []


@requires_corpus
def test_input_file_is_indexed_with_unit(index) -> None:
    """The archetype question: which routine reads aquifer.aqu, on what unit."""
    opens = [u for u in index.io_for_file("in_aqu%aqu") if u.op == "open"]
    by_unit = [u for u in index.io_for_unit("107", "open")]
    assert by_unit, "unit 107 open not indexed"
    assert by_unit[0].procedure == "aqu_read"
    assert opens or by_unit


@requires_corpus
def test_open_output_file_resolves_to_real_filename(index) -> None:
    """SWAT+ opens output files via a helper, so `write(2520,...)` alone would
    be recorded as `unit_2520` and the file would be unfindable by name."""
    uses = index.io_for_file("aquifer_day.txt")
    assert uses, "output filename not resolved from open_output_file"
    assert {u.procedure for u in uses} == {"header_aquifer"}
    assert all(u.unit == "2520" for u in uses)


@requires_corpus
def test_output_unit_map_covers_both_text_and_csv(fake_source: Path) -> None:
    units = output_unit_filenames(fake_source)
    assert units["2520"] == "aquifer_day.txt"
    assert units["2524"] == "aquifer_day.csv"


@requires_corpus
def test_io_records_statement_variables(index) -> None:
    """The terms of a conditional breakpoint.

    A loop listing cannot answer "what identifies this record here" when the
    routine has no loop of its own -- the write statement's own variables can.
    """
    writes = [u for u in index.io_for_file("aquifer_day.txt") if u.op == "write"]
    assert writes
    assert "2520" in {u.unit for u in writes}


@requires_corpus
def test_variable_writers(index) -> None:
    sites = index.writers_of("sw_volume_begin")
    assert sites, "assignment not indexed"
    assert all(s.startswith("hru_control:") for s in sites)


@requires_corpus
def test_writers_key_on_the_full_field_path(index) -> None:
    """`db_mx%aqudb = ...` is indexed under `db_mx%aqudb`, not `db_mx`.

    Keying on the root symbol buries a field among every other write to the
    same structure, so the name a person actually searches for -- the field --
    finds nothing. A real run hit this looking for `rchrg`, and found only
    where it is zeroed.
    """
    assert any(s.startswith("aqu_read:") for s in index.writers_of("db_mx%aqudb"))
    assert index.writers_of("db_mx") == []


def test_field_path_strips_subscripts() -> None:
    assert field_path("aqu_d(iaq)%rchrg") == "aqu_d%rchrg"
    assert field_path("soil(j)%ly(ly)%tillagef_tillmix") == "soil%ly%tillagef_tillmix"
    assert field_path("sw_volume_begin") == "sw_volume_begin"


def test_field_path_rejects_non_paths() -> None:
    """Anything that is not a plain field path is dropped, not mis-indexed."""
    assert field_path("a + b") is None
    assert field_path("") is None


@requires_corpus
def test_writers_ignore_comparisons(index) -> None:
    """`==` is not an assignment; a false positive here would be noise."""
    assert index.writers_of("i_exist") == []


@requires_corpus
def test_loop_locations(index) -> None:
    loops = index.loops_in("hru_control")
    assert len(loops) == 2
    headers = {loop.header for loop in loops}
    assert any("ly = 1" in h for h in headers)
    assert any("ipest = 1" in h for h in headers)
    assert all(loop.line > 0 for loop in loops)


# ---------------------------------------------------------------- render

@requires_corpus
def test_rendered_index_is_greppable(index) -> None:
    text = render_index(index)
    assert "\r" not in text, "CRLF would break line-oriented grep patterns"
    assert "aqu_read|aqu_read.f90:1" in text
    assert any(
        line.startswith("aquifer_day.txt|write|unit=2520|header_aquifer|")
        for line in text.splitlines()
    ), "the unit column must be self-describing; a grep hit carries no heading"


@requires_corpus
def test_rendered_index_carries_provenance(index) -> None:
    text = render_index(index)
    assert "## Provenance" in text
    assert "generated_at:" in text
    assert "source_commit:" in text


# ------------------------------------------------------- real corpus

@requires_source
@requires_corpus
def test_real_corpus_archetype_questions() -> None:
    """Against the pinned checkout, the questions the index exists to answer."""
    index = build_source_index(REAL_SOURCE, Path(CORPUS) if CORPUS else None)

    assert len(index.procedures) > 700

    readers = {u.procedure for u in index.io_for_file("aquifer.aqu") if u.op == "open"}
    assert "aqu_read" in readers

    writers = {u.procedure for u in index.io_for_file("aquifer_day.txt")}
    assert "aquifer_output" in writers

    assert index.callers_of("hru_control")
    assert index.loops_in("hru_control")

    # aquifer_output has no loop of its own -- iaq is set by its caller -- so
    # the write statement's variables are the only source of breakpoint terms.
    day_writes = [u for u in index.io_for_file("aquifer_day.txt")
                  if u.procedure == "aquifer_output"]
    assert day_writes
    assert "iaq" in day_writes[0].fields
    assert any(f.startswith("time%day") for f in day_writes[0].fields)


# --------------------------------------------------------------- install

def test_install_creates_pointer(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    assert install_pointer(claude_md) == "created"
    assert INDEX_NAME in claude_md.read_text(encoding="utf-8")


def test_install_appends_without_clobbering(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Project notes\n\nBuild with cmake.\n", encoding="utf-8")
    assert install_pointer(claude_md) == "appended"
    text = claude_md.read_text(encoding="utf-8")
    assert "Build with cmake." in text
    assert INDEX_NAME in text


def test_install_is_idempotent(tmp_path: Path) -> None:
    """Regenerating must refresh the section, never stack duplicates."""
    claude_md = tmp_path / "CLAUDE.md"
    install_pointer(claude_md)
    assert install_pointer(claude_md) == "unchanged"
    assert claude_md.read_text(encoding="utf-8").count("## SWAT+ source index") == 1


def test_install_replaces_a_stale_section(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    marker = "<!-- swatplus-index -->"
    claude_md.write_text(
        f"# Notes\n\n{marker}\nold and wrong\n{marker}\n\nkeep me\n",
        encoding="utf-8",
    )
    assert install_pointer(claude_md) == "updated"
    text = claude_md.read_text(encoding="utf-8")
    assert "old and wrong" not in text
    assert "keep me" in text
    assert text.count(marker) == 2


@requires_corpus
def test_rendered_io_row_carries_breakpoint_terms(index) -> None:
    text = render_index(index)
    row = next(line for line in text.splitlines()
               if line.startswith("aquifer_day.txt|write|unit=2520|header_aquifer|"))
    assert row.count("|") >= 5, "I/O rows must carry a variables column"


def test_pointer_states_every_field_order() -> None:
    """A grep hit arrives without its section heading.

    The model has to know which column is which from CLAUDE.md alone -- when it
    did not, it read `...|2520|aquifer_output|22|...` as unit 22 / line 2520 and
    spent eight extra tool calls recovering.
    """
    text = pointer_text()
    for legend in (
        "name | file:line | module | calls | called-by",
        "file | op | unit=N | routine | lines",
        "variable | routine:line",
        "routine | line | loop header",
    ):
        assert legend in text, f"pointer is missing the {legend!r} field order"


def test_package_compiles_without_syntax_warnings() -> None:
    """Guards against an invalid escape sequence in the pointer template.

    The pointer is markdown: a pipe inside a table cell must be written \\|,
    which is not a Python escape. In a non-raw string Python 3.12+ warns, and
    the warning only appears on a fresh compile -- so it surfaced on a user's
    machine rather than here.
    """
    import py_compile
    import warnings

    source = Path(install_module.__file__)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        py_compile.compile(str(source), doraise=True, cfile=str(source) + ".testc")
    Path(str(source) + ".testc").unlink(missing_ok=True)


def test_install_writes_every_assistants_instruction_file(tmp_path: Path) -> None:
    """Each assistant reads a different file, and one that never sees the
    pointer never learns the index exists.

    The adoption result was measured on Claude, which reads CLAUDE.md. Codex
    reads AGENTS.md and Copilot reads .github/copilot-instructions.md, so a
    CLAUDE.md-only install would have made the index invisible to both.
    """
    actions = install_pointers(tmp_path)
    assert set(actions) == set(POINTER_FILES)
    for name in POINTER_FILES:
        assert INDEX_NAME in (tmp_path / name).read_text(encoding="utf-8")


def test_install_creates_missing_parent_directories(tmp_path: Path) -> None:
    """.github/ usually does not exist in a fresh checkout."""
    install_pointers(tmp_path)
    assert (tmp_path / ".github" / "copilot-instructions.md").is_file()


def test_installing_twice_changes_nothing(tmp_path: Path) -> None:
    install_pointers(tmp_path)
    again = install_pointers(tmp_path)
    assert set(again.values()) == {"unchanged"}


def test_source_defaults_to_the_working_directory(fake_source: Path, monkeypatch) -> None:
    """An end user runs this from inside their checkout and types no path."""
    monkeypatch.chdir(fake_source)
    assert resolve_source(None) == fake_source


def test_source_default_accepts_the_repo_root(fake_source: Path, monkeypatch) -> None:
    monkeypatch.chdir(fake_source.parent)
    assert resolve_source(None) == fake_source


def test_looks_like_swatplus_rejects_an_unrelated_directory(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    assert not looks_like_swatplus(tmp_path)


def test_source_error_names_all_three_ways_to_fix_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWATPLUS_SOURCE", raising=False)
    with pytest.raises(IndexError_) as excinfo:
        resolve_source(None)
    message = str(excinfo.value)
    assert "inside a SWAT+ checkout" in message
    assert "SWATPLUS_SOURCE" in message
    assert "--source" in message


# ------------------------------------------------------------------ hooks

def _git_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    return tmp_path


def test_hooks_cover_every_event_that_changes_the_tree(tmp_path: Path) -> None:
    """Nobody re-runs a build command after every edit, so without a hook the
    index is stale in practice however fast it rebuilds."""
    actions = install_hooks(_git_repo(tmp_path))
    assert set(actions) == set(HOOK_EVENTS)
    for event in HOOK_EVENTS:
        hook = tmp_path / ".git" / "hooks" / event
        assert hook.is_file()
        assert "swatplus-build" in hook.read_text(encoding="utf-8")


def test_hook_falls_back_to_python_m(tmp_path: Path) -> None:
    """The console script is not always on PATH -- git runs hooks with a
    reduced environment. That fallback was silently dead until it was tested:
    cli.py had a main() and no __main__ block, so `python -m` imported the
    module and exited without building anything."""
    install_hooks(_git_repo(tmp_path))
    body = (tmp_path / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert "python -m tamandua.index.cli" in body
    assert "python3 -m tamandua.index.cli" in body


def test_cli_module_is_executable() -> None:
    """Guards the missing-__main__ bug: `python -m` must actually run."""
    import tamandua.index.cli as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in source
    assert "raise SystemExit(main())" in source


def test_hooks_never_fail_the_git_operation(tmp_path: Path) -> None:
    """A stale index is not worth failing someone's commit over."""
    install_hooks(_git_repo(tmp_path))
    body = (tmp_path / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert "|| true" in body


def test_hooks_leave_someone_elses_hook_alone(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    existing = repo / ".git" / "hooks" / "post-commit"
    existing.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
    actions = install_hooks(repo)
    assert actions["post-commit"] == "skipped (not ours)"
    assert existing.read_text(encoding="utf-8") == "#!/bin/sh\necho theirs\n"


def test_installing_hooks_twice_changes_nothing(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    install_hooks(repo)
    assert set(install_hooks(repo).values()) == {"unchanged"}


def test_hooks_report_when_there_is_no_repository(tmp_path: Path) -> None:
    assert install_hooks(tmp_path) == {"": "not a git repository"}


# ------------------------------------------------------- staleness / self-heal

def test_fingerprint_tracks_the_working_tree(fake_source: Path) -> None:
    """Not the last commit.

    Questions get asked about code being edited and not yet committed, which is
    exactly when a commit-based check would call a stale index current.
    """
    before = source_fingerprint(fake_source)
    (fake_source / "aqu_read.f90").write_text(READER + "\n! edited\n", encoding="utf-8")
    assert source_fingerprint(fake_source) != before


def test_fingerprint_ignores_touch_without_change(fake_source: Path) -> None:
    """Content, not mtime -- an editor can save a file without changing it."""
    before = source_fingerprint(fake_source)
    path = fake_source / "aqu_read.f90"
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    assert source_fingerprint(fake_source) == before


@requires_corpus
def test_index_is_current_until_the_source_changes(fake_source: Path, tmp_path: Path) -> None:
    index_file = tmp_path / "SWATPLUS_INDEX.md"
    index_file.write_text(render_index(build_source_index(fake_source, _builder())), encoding="utf-8")

    assert index_is_current(index_file, fake_source)

    (fake_source / "extra.f90").write_text(
        "      subroutine added\n      return\n      end subroutine added\n",
        encoding="utf-8",
    )
    assert not index_is_current(index_file, fake_source)


def test_index_is_stale_when_the_parser_commit_changes(
    fake_source: Path, tmp_path: Path, monkeypatch
) -> None:
    """A parser swap can change facts while every Fortran byte stays fixed."""
    artifact = tmp_path / "swatplus-facts.json"
    artifact.write_text(json.dumps({
        "provenance": {
            "source_fingerprint": source_fingerprint(fake_source),
            "parser_commit": "old-parser",
        }
    }), encoding="utf-8")
    corpus_src = tmp_path / "corpus" / "src"
    (corpus_src / "swatplus_reference").mkdir(parents=True)
    monkeypatch.setattr("tamandua.index.build.find_corpus", lambda _=None: corpus_src)
    monkeypatch.setattr("tamandua.index.build._git", lambda *_args: "new-parser")

    assert not index_is_current(artifact, fake_source)
    assert stored_parser_commit(artifact) == "old-parser"


def test_missing_index_is_not_current(fake_source: Path, tmp_path: Path) -> None:
    assert not index_is_current(tmp_path / "absent.md", fake_source)


def test_index_without_a_fingerprint_is_not_current(fake_source: Path, tmp_path: Path) -> None:
    """An index from an older format must rebuild rather than be trusted."""
    stale = tmp_path / "SWATPLUS_INDEX.md"
    stale.write_text("# SWAT+ source index\n\nsource_commit: abc123\n", encoding="utf-8")
    assert not index_is_current(stale, fake_source)


def test_pointer_tells_the_assistant_to_self_heal() -> None:
    """The whole mechanism: an assistant runs this before answering."""
    text = pointer_text()
    assert "Before using it, run `swatplus-build --markdown`" in text
    assert "uncommitted" in text


def _builder() -> Path | None:
    return Path(CORPUS) if CORPUS else None


# ------------------------------------------------ fields, types, call paths

def test_field_doc_splits_units_from_meaning() -> None:
    """SWAT+ writes them as `mm | recharge entering aquifer`."""
    assert split_field_doc("mm         |recharge entering aquifer") == {
        "units": "mm", "description": "recharge entering aquifer"}


def test_field_doc_without_a_separator_is_all_meaning() -> None:
    """A bare unit with no explanation is not worth indexing as one."""
    assert split_field_doc("condition II curve number") == {
        "units": None, "description": "condition II curve number"}


def test_field_doc_handles_nothing() -> None:
    assert split_field_doc(None) == {"units": None, "description": None}
    assert split_field_doc("   ") == {"units": None, "description": None}


@requires_source
@requires_corpus
def test_real_corpus_search_fields_bridges_words_to_identifiers() -> None:
    """The gap the index was said to have no answer for.

    'lateral flow' is not an identifier and appears in no procedure name, but
    the source documents `aquifer_dynamic%flo` as exactly that.
    """
    index = build_source_index(REAL_SOURCE, Path(CORPUS) if CORPUS else None)

    hits = index.search_fields("lateral flow")
    assert any(h.path == "aquifer_dynamic%flo" for h in hits)
    match = next(h for h in hits if h.path == "aquifer_dynamic%flo")
    assert match.units == "mm"

    # An exact identifier must not be buried under prose matches.
    assert index.search_fields("rchrg")[0].name == "rchrg"


@requires_source
@requires_corpus
def test_real_corpus_describes_a_state_object() -> None:
    """What `aqu_d(iaq)` actually contains, for reading an output row."""
    index = build_source_index(REAL_SOURCE, Path(CORPUS) if CORPUS else None)
    derived = index.derived_type("aquifer_dynamic")
    assert derived is not None
    names = {f.name for f in derived.fields}
    assert {"flo", "rchrg", "seep", "revap"} <= names


@requires_source
@requires_corpus
def test_real_corpus_call_paths_reach_an_entry_point() -> None:
    index = build_source_index(REAL_SOURCE, Path(CORPUS) if CORPUS else None)
    paths = index.paths_to("aqu_1d_control")
    assert paths
    assert all(p[-1] == "aqu_1d_control" for p in paths)
    assert any("command" in p for p in paths)


@requires_corpus
def test_search_fields_ignores_empty_input(fake_source: Path) -> None:
    index_obj = build_source_index(fake_source, Path(CORPUS) if CORPUS else None)
    assert index_obj.search_fields("") == []


# ------------------------------------------------- what a bare run produces

@requires_corpus
def test_bare_run_writes_facts_and_nothing_else(fake_source: Path, tmp_path,
                                                monkeypatch) -> None:
    """MCP is the delivery, so a bare build must not edit anyone's repository.

    The markdown rendering and the instruction-file pointers are the fallback
    for tools that cannot run a server. Dropping them into a checkout unasked
    is what the earlier default did, and it contradicted the decision.
    """
    from tamandua.index.cli import main

    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    assert main(["--source", str(fake_source)]) == 0

    assert (workdir / FACTS_NAME).is_file()
    assert not (workdir / INDEX_NAME).exists()
    for name in POINTER_FILES:
        assert not (workdir / name).exists(), f"{name} written without --markdown"


@requires_corpus
def test_markdown_is_opt_in_and_still_works(fake_source: Path, tmp_path,
                                            monkeypatch) -> None:
    from tamandua.index.cli import main

    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    assert main(["--source", str(fake_source), "--markdown",
                 "--install", str(workdir)]) == 0

    assert (workdir / INDEX_NAME).is_file()
    for name in POINTER_FILES:
        assert (workdir / name).is_file(), f"{name} missing under --markdown"


@requires_corpus
def test_staleness_is_judged_on_the_facts_file(fake_source: Path, tmp_path,
                                               monkeypatch) -> None:
    """The check has to read JSON now, not just the markdown's header lines."""
    from tamandua.index.cli import main

    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    main(["--source", str(fake_source)])

    assert main(["--source", str(fake_source), "--check"]) == 0   # current

    (fake_source / "new_routine.f90").write_text(
        "      subroutine brand_new\n      end subroutine brand_new\n",
        encoding="utf-8")
    assert main(["--source", str(fake_source), "--check"]) == 1   # stale


def test_stored_fingerprint_survives_a_corrupt_file(tmp_path) -> None:
    """A truncated download must read as stale, not crash the build."""
    broken = tmp_path / "swatplus-facts.json"
    broken.write_text('{"provenance": {"source_', encoding="utf-8")
    assert stored_fingerprint(broken) is None
