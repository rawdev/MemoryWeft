#!/bin/bash
# ===========================================================================
#  MWeft portable launcher (macOS) - docs/mweft_public/06_portable_launcher.md
#  Unzip -> right-click -> "Open" (Gatekeeper approval, first run only).
#  Afterwards a normal double-click works.
#  Open this file in a text editor to inspect it (transparent - unsigned).
# ===========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Package: mweft[embed-onnx,manager,postgres]. Entry = the native Manager desktop app (mweft-app).
PKG="mweft[embed-onnx,manager,postgres]"
MOD="k2g.desktop"

UV="$HERE/bin/uv"
VENV="$HERE/runtime/venv"
PYEXE="$VENV/bin/python"
READY="$HERE/.mweft-ready"
STAMP="$HERE/runtime/.version"

# --- Self-remove Gatekeeper quarantine (works after the first right-click->Open approval) ---
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true
chmod +x "$UV" 2>/dev/null || true

# --- macOS privacy (TCC) warning -------------------------------------------
# Downloads / Desktop / Documents are protected: a sandboxed AI client (Claude,
# etc.) cannot read files there, so it is blocked from launching the MCP server
# out of such a folder — the server then dies with "No module named 'encodings'".
# The Manager (run from this Terminal) still works, so warn rather than block.
case "$HERE/" in
  "$HOME/Downloads/"*|"$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Library/Mobile Documents/"*)
    echo "============================================================"
    echo "[MWeft] WARNING: this folder is in a macOS privacy-protected location:"
    echo "          $HERE"
    echo "  Your AI client (Claude, etc.) will likely be BLOCKED from starting"
    echo "  the memory server here — the MCP server fails with"
    echo "  \"ModuleNotFoundError: No module named 'encodings'\"."
    echo "  Fix: move this folder to your home directory, e.g.:"
    echo "          mv \"$HERE\" ~/"
    echo "  (or grant the AI client Full Disk Access in System Settings ->"
    echo "   Privacy & Security). Keep your memory/data folder out of those"
    echo "  protected locations too."
    echo "============================================================"
    ;;
esac

# --- Self-contained locations ---
export UV_PYTHON_INSTALL_DIR="$HERE/runtime/python"
export UV_CACHE_DIR="$HERE/runtime/cache"
# Use a uv-managed standalone Python ONLY. Never adopt a Python already on the
# host (e.g. a conda/homebrew base): an inherited interpreter is not
# self-contained and leaks its own library search paths into our process.
# only-managed makes uv download a python-build-standalone into
# UV_PYTHON_INSTALL_DIR (needs network on first setup).
export UV_PYTHON_PREFERENCE=only-managed

# --- Runtime settings ---
export K2G_DOTENV_FILE=off
export K2G_USE_HUB=auto
export EMBEDDING_PROVIDER=onnx
export EMBEDDING_ONNX_PATH="$HERE/models/bge-m3-onnx"
export EMBEDDING_DIM=1024
# Bundled first-run sample DB — a fresh install seeds explorable demo data
# (domain sample_work) + a one-time intro instead of a blank create-DB prompt.
export MWEFT_SAMPLE_DIR="$HERE/asset/mweft_sample"
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
  "$UV" venv "$VENV" --python 3.11 --python-preference only-managed
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
