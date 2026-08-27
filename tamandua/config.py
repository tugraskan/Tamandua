"""Configuration and dependency pins.

Loads ``docs/pins.toml`` — the single source of truth for the exact versions of
every external repository and dataset this work is built and tested against.
Nothing floats to a branch tip: pins are bumped deliberately, so an index can
always be traced to the source revision it describes.

``.github/workflows/release.yml`` reads the same file, so a published snapshot
is built against the same commits a local build uses.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# docs/pins.toml lives at the repository root, two parents up from this file
# (tamandua/config.py -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PINS_PATH = _REPO_ROOT / "docs" / "pins.toml"

# Sentinel used in pins.toml for a version that is intentionally not yet fixed.
UNRESOLVED = "TBD"


@dataclass(frozen=True)
class Pin:
    """A single dependency pin. ``ref`` or ``commit`` may be ``UNRESOLVED``."""

    name: str
    repo: str | None = None
    commit: str | None = None
    ref: str | None = None

    @property
    def resolved(self) -> bool:
        """True once this pin points at a concrete commit/ref."""
        value = self.commit or self.ref
        return value is not None and value != UNRESOLVED

    def version(self) -> str | None:
        """The concrete commit or ref, or ``None`` if unresolved."""
        return (self.commit or self.ref) if self.resolved else None


@dataclass(frozen=True)
class Pins:
    """Parsed contents of pins.toml."""

    swatplus_version: str
    raw: dict
    path: Path

    def get(self, section: str) -> Pin:
        """Return the pin for a named section (e.g. ``reference_corpus``)."""
        data = self.raw.get(section)
        if not isinstance(data, dict):
            raise KeyError(f"No pin section '{section}' in {self.path}")
        return Pin(
            name=section,
            repo=data.get("repo") or data.get("source_repo"),
            commit=data.get("commit"),
            ref=data.get("ref") or data.get("source_ref"),
        )

    def unresolved(self) -> list[str]:
        """Names of pin sections still set to the ``TBD`` placeholder.

        Useful as a preflight: a retriever whose pin is unresolved cannot run
        against a fixed version yet.
        """
        names: list[str] = []
        for section, data in self.raw.items():
            # Only repository/dataset pins; skip plain config tables such as
            # [version_comparison], which holds an old/new version pair.
            if not isinstance(data, dict):
                continue
            if not (data.get("repo") or data.get("source_repo")):
                continue
            values = [
                data.get(k)
                for k in ("commit", "ref", "source_ref", "path_in_repo")
                if k in data
            ]
            if any(v == UNRESOLVED for v in values):
                names.append(section)
        return names


# Where local checkouts of the pinned dependencies live. These are paths, not
# versions, so they belong in the environment rather than the pin manifest:
# the manifest says *which commit*, the environment says *where on disk*.
_CHECKOUT_ENV: dict[str, str] = {
    "reference_corpus": "SWATPLUS_REFERENCE_CORPUS",
    "dataselector": "SWATPLUS_DATASELECTOR",
    "swatplus_source": "SWATPLUS_SOURCE",
}

_CHECKOUT_DEFAULT_ROOT = Path("/workspace")

# Default on-disk directory name per dependency, under the default root.
_CHECKOUT_DIRNAME: dict[str, str] = {
    "reference_corpus": "swatplus-reference-corpus",
    "dataselector": "swatplus-dataselector",
    "swatplus_source": "swatplus-62.0.0",
}


# Ames is a subdirectory of the swatplus_source checkout
# (refdata/Ames_sub1), not a repo root, so it needs its own resolver rather
# than the generic per-section convention above.
_AMES_ENV = "SWATPLUS_AMES"
_AMES_SUBPATH = "refdata/Ames_sub1"
_AMES_FALLBACK_CANDIDATES = (
    Path("external/Ames_sub1"),  # scripts/fetch_ames.sh's default destination
    _CHECKOUT_DEFAULT_ROOT / "swatplus-62.0.0" / _AMES_SUBPATH,
)


def resolve_ames_dataset() -> Path | None:
    """Locate a local checkout of the pinned Ames reference dataset.

    Resolution order: ``SWATPLUS_AMES``, then the fetch script's default
    destination, then the conventional swatplus_source checkout's
    refdata/Ames_sub1. See scripts/fetch_ames.sh and docs/pins.toml
    [reference_dataset].
    """
    raw = os.environ.get(_AMES_ENV)
    if raw:
        candidate = Path(raw)
        return candidate if candidate.is_dir() else None
    for candidate in _AMES_FALLBACK_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return None


def resolve_checkout(section: str) -> Path | None:
    """Locate a local checkout of a pinned dependency.

    Resolution order: the section's environment variable, then the conventional
    directory under ``/workspace``. Returns ``None`` when neither exists, so
    callers can degrade gracefully rather than crash.
    """
    env_var = _CHECKOUT_ENV.get(section)
    if env_var:
        raw = os.environ.get(env_var)
        if raw:
            candidate = Path(raw)
            return candidate if candidate.is_dir() else None

    dirname = _CHECKOUT_DIRNAME.get(section)
    if dirname:
        candidate = _CHECKOUT_DEFAULT_ROOT / dirname
        if candidate.is_dir():
            return candidate
    return None


def load_pins(path: Path | str | None = None) -> Pins:
    """Load and parse the pin manifest."""
    pins_path = Path(path) if path is not None else DEFAULT_PINS_PATH
    with pins_path.open("rb") as fh:
        raw = tomllib.load(fh)
    version = raw.get("swatplus_version")
    if not version:
        raise ValueError(f"'swatplus_version' missing from {pins_path}")
    return Pins(swatplus_version=version, raw=raw, path=pins_path)
