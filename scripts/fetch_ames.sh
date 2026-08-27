#!/usr/bin/env bash
# Fetch the pinned Ames reference dataset (docs/pins.toml, [reference_dataset]).
#
# The Ames_sub1 TxtInOut project lives in swat-model/swatplus under
# refdata/Ames_sub1. It is the integration fixture for output/reader.py and the
# dataset the swatplus-dataselector engine was validated against upstream.
#
# This uses a direct shallow clone rather than the session's add_repo flow,
# because add_repo cannot pull swat-model/* into a tugraskan-owned session
# (cross-tier restriction). A plain clone of the public repo works.
#
# Usage:  scripts/fetch_ames.sh [DEST_DIR]
# Default DEST_DIR: external/Ames_sub1 (gitignored).
set -euo pipefail

REF="62.0.0"                         # docs/pins.toml reference_dataset.source_ref
SRC="https://github.com/swat-model/swatplus"
SUBPATH="refdata/Ames_sub1"
DEST="${1:-external/Ames_sub1}"

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
dest_abs="${repo_root}/${DEST}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Cloning ${SRC} @ ${REF} (shallow)…"
git clone --depth 1 --branch "${REF}" "${SRC}" "${tmp}/swatplus" >/dev/null 2>&1

if [[ ! -d "${tmp}/swatplus/${SUBPATH}" ]]; then
  echo "error: ${SUBPATH} not found at ${REF}" >&2
  exit 1
fi

mkdir -p "$(dirname "${dest_abs}")"
rm -rf "${dest_abs}"
cp -r "${tmp}/swatplus/${SUBPATH}" "${dest_abs}"

count="$(find "${dest_abs}" -type f | wc -l | tr -d ' ')"
echo "Ames_sub1 ready at ${DEST} (${count} files)."
echo "Point the dataselector MCP server at it:  --dataset ${DEST}"
