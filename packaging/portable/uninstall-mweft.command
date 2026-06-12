#!/bin/bash
# ===========================================================================
#  MWeft portable uninstaller (macOS)
#  Removes the local runtime + ALL local memory data. Read the steps below.
#  Open this file in TextEdit to inspect it (transparent - unsigned).
# ===========================================================================
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

cat <<EOF
============================================================
 MemoryWeft - uninstall
============================================================
This removes MemoryWeft's runtime and ALL local memory data.

Recommended order:
  1. FIRST, in the Manager (Settings -> each AI -> Remove),
     remove the mweft MCP server from your AI clients, then
     RESTART any global AIs (Claude Desktop, etc.).
     A script cannot safely edit each AI's config - do it there.
  2. This script then deletes:
       - \$HOME/.mweft     (project list / global config)
       - $HERE/data        (your SQLite memory DB)
       - $HERE/runtime     (the bundled Python venv)
  3. After it finishes, delete this folder itself:
       $HERE

Postgres users: your memory DB lives on the server - delete it separately.
EOF

printf 'Type DELETE (all caps) to proceed, or anything else to cancel: '
read -r ANS
if [ "$ANS" != "DELETE" ]; then
  echo "Cancelled - nothing was removed."
  exit 1
fi

echo
for target in "$HOME/.mweft" "$HERE/data" "$HERE/runtime"; do
  if [ -e "$target" ]; then rm -rf "$target" && echo "removed: $target"; fi
done
rm -f "$HERE/.mweft-ready" 2>/dev/null || true

echo
echo "Done. The memory data and runtime are gone."
echo "Now close this window and delete the folder:  $HERE"
echo "(the rest is just the launcher and bundled binaries.)"
