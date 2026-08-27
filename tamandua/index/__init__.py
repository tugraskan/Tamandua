"""Facts-only index over SWAT+ Fortran source.

One implementation, two consumers: ``scripts/build_index.py`` renders it to a
checked-in file, ``tamandua.mcp.server`` serves the same objects over
MCP. Both call :func:`build_source_index`; neither parses anything itself.
"""

from tamandua.index.build import (
    INDEX_FORMAT_VERSION,
    DerivedType,
    Field,
    IndexError_,
    IOUse,
    Loop,
    Procedure,
    Provenance,
    SourceIndex,
    build_source_index,
    field_path,
    find_corpus,
    index_is_current,
    input_filenames,
    looks_like_swatplus,
    output_unit_filenames,
    resolve_source,
    source_fingerprint,
    split_field_doc,
    stored_fingerprint,
    stored_parser_commit,
)
from tamandua.index.install import (
    FACTS_NAME,
    HOOK_EVENTS,
    INDEX_NAME,
    POINTER_FILES,
    install_hooks,
    install_pointer,
    install_pointers,
    pointer_text,
)
from tamandua.index.scope import (
    LoopScope,
    condition_for,
    loop_ranges,
    scope_at,
)
from tamandua.index.render import render_index
from tamandua.index.snapshot import (
    SNAPSHOT_FORMAT,
    load_snapshot,
    save_snapshot,
)

__all__ = [
    "INDEX_FORMAT_VERSION",
    "DerivedType",
    "FACTS_NAME",
    "Field",
    "HOOK_EVENTS",
    "INDEX_NAME",
    "POINTER_FILES",
    "IOUse",
    "IndexError_",
    "Loop",
    "LoopScope",
    "Procedure",
    "Provenance",
    "SNAPSHOT_FORMAT",
    "SourceIndex",
    "build_source_index",
    "condition_for",
    "load_snapshot",
    "save_snapshot",
    "loop_ranges",
    "scope_at",
    "field_path",
    "find_corpus",
    "index_is_current",
    "input_filenames",
    "install_hooks",
    "install_pointer",
    "install_pointers",
    "output_unit_filenames",
    "pointer_text",
    "render_index",
    "resolve_source",
    "source_fingerprint",
    "split_field_doc",
    "stored_fingerprint",
    "stored_parser_commit",
]
