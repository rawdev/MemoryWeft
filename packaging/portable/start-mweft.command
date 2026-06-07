#!/bin/bash
# ===========================================================================
#  MWeft portable launcher (macOS) - docs/mweft_public/06_portable_launcher.md
#  Unzip -> right-click -> "Open" (Gatekeeper approval, first run only).
#  Afterwards a normal double-click works.
#  Open this file in a text editor to inspect it (transparent - unsigned).
# ===========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Package: mweft[embed-onnx,manager]. Entry = the native Manager desktop app (mweft-app).
PKG="mweft[embed-onnx,manager]"
MOD="k2g.desktop"

UV="$HERE/bin/uv"
VENV="$HERE/runtime/venv"
PYEXE="$VENV/bin/python"
READY="$HERE/.mweft-ready"
STAMP="$HERE/runtime/.version"

# --- Self-remove Gatekeeper quarantine (works after the first right-click->Open approval) ---
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true
chmod +x "$UV" 2>/dev/null || true

# --- Self-contained locations ---
export UV_PYTHON_INSTALL_DIR="$HERE/runtime/python"
export UV_CACHE_DIR="$HERE/runtime/cache"

# --- Runtime settings ---
export K2G_DOTENV_FILE=off
export K2G_USE_HUB=auto
export EMBEDDING_PROVIDER=onnx
export EMBEDDING_ONNX_PATH="$HERE/models/bge-m3-onnx"
export EMBEDDING_DIM=1024
# Lazy init: MCP servers the Manager registers defer heavy init to the first
# tool call, so the AI client doesn't hit a startup timeout on first connect.
export K2G_MCP_LAZY_INIT=true

# --- Upgrade detection: rebuild runtime on VERSION mismatch (data is kept) ---
if [ -f "$HERE/VERSION" ] && [ -f "$STAMP" ] && ! cmp -s "$HERE/VERSION" "$STAMP"; then
  echo "[MWeft] New version detected - resetting runtime..."
  rm -rf "$VENV"; rm -f "$READY"
fi

# --- Skip when already set up ---
if [ ! -f "$READY" ] || [ ! -x "$PYEXE" ]; then
  echo "[MWeft] First-time setup... (once only, 1-5 min depending on network/specs)"
  # Pin to 3.11 to match the cp311 wheels bundled by the offline build
  # (release.yml uses Python 3.11). Keep both sides in lockstep when bumping.
  "$UV" venv "$VENV" --python 3.11
  if [ -d "$HERE/wheels" ]; then
    # full-offline: use bundled wheels only (zero network)
    "$UV" pip install --python "$PYEXE" --no-index --find-links "$HERE/wheels" "$PKG"
  else
    # online: install from PyPI
    "$UV" pip install --python "$PYEXE" "$PKG"
  fi
  [ -f "$HERE/VERSION" ] && cp -f "$HERE/VERSION" "$STAMP" || true
  echo ready > "$READY"
fi

mkdir -p "$HERE/data/project"
echo "[MWeft] Starting the Manager app - a window will open shortly."
echo "[MWeft] To quit, close the app window."
exec "$PYEXE" -m "$MOD" --project-dir-default "$HERE/data/project"
