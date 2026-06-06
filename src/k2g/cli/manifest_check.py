"""``k2g-manifest-check``: lightweight pre-chunking change detection.

Called by an AI (Claude, etc.) **before** splitting a file into chunks.
If the source file is already loaded with the same content, returns
``unchanged`` so the AI can skip the entire manifest authoring step
(summary, NER = LLM cost). The check compares the *raw byte hash* recorded
by ``k2g-ingest-manifest --source``, making it robust to LLM chunking
variation.

Designed to be lightweight — only BuildManifestStore and the source hash
are needed; the embedding model and DbStore are not started.

Usage:
    k2g-manifest-check --source path/to/original.md --file-path docs/original.md
    # exit 0 = changed (load needed), exit 3 = unchanged (skip).
    # The result token is also printed to stdout.

Domain is forced from server env (K2G_USER_MEMORY_SAVE_DOMAIN),
consistent with the ingest path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXIT_CHANGED = 0       # Source changed or new — proceed with manifest authoring
EXIT_UNCHANGED = 3     # Source unchanged — skip manifest authoring and load
EXIT_ERROR = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="k2g-manifest-check",
        description="Pre-chunking source change check (saves LLM cost when unchanged)",
    )
    p.add_argument("--source", required=True,
                   help="Path to the original or temporary source file — "
                        "change is determined from the byte hash of this file.")
    p.add_argument("--file-path", required=True, dest="file_path",
                   help="Document identity key — must match the file_path field "
                        "in the manifest.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    spath = Path(args.source)
    if not spath.is_file():
        print(f"[error] --source file not found: {spath}", file=sys.stderr)
        return EXIT_ERROR

    from k2g.core.config import get_settings
    from k2g.memory.save_context import resolve_save_domain
    from k2g.updater.incremental import IncrementalBuilder

    settings = get_settings()
    domain, _ = resolve_save_domain(settings)
    file_path = args.file_path.replace("\\", "/").strip("/")

    source_hash = IncrementalBuilder.compute_hash_bytes(spath.read_bytes())

    # Lightweight — only BuildManifestStore (no embedding / DbStore).
    try:
        from k2g.updater.manifest import BuildManifestStore
        mstore = BuildManifestStore()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] failed to open manifest store: {exc}", file=sys.stderr)
        return EXIT_ERROR

    unchanged = bool(mstore.check_file_hash(domain, file_path, source_hash))

    if unchanged:
        print("unchanged")  # same source already loaded — skip manifest authoring
        return EXIT_UNCHANGED
    print("changed")        # new or modified — author manifest then run k2g-ingest-manifest --source
    return EXIT_CHANGED


if __name__ == "__main__":
    sys.exit(main())
