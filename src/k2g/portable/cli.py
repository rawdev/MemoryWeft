"""BP-74 — `mweft-export` / `mweft-import` CLI entry points.

Run modes:
- ``mweft-export <domain> --to <dir|file> [options]``
- ``mweft-import <archive> [--strategy=...] [--dry-run]``

By default, the runtime is resolved from environment variables (the same
settings the MCP launcher uses).  Pass ``--project-dir <path>`` to load a
project-specific ``.mweft/project.yaml`` first (BP-73).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _setup_env_from_project(project_dir: Path | None) -> None:
    """Apply ``.mweft/project.yaml`` env to ``os.environ`` if requested.

    Must run *before* ``k2g.core.config.Settings`` is imported anywhere in
    this process (Settings is ``@lru_cache``'d).
    """
    if project_dir is None:
        return
    from k2g.ui.project_registry import entry_env_dict, find_by_dir

    entry = find_by_dir(project_dir)
    if entry is not None:
        env = entry_env_dict(entry)
    else:
        # Transition fallback: un-migrated folder still has a project.yaml.
        from k2g.ui.project_config import load_project_config
        env = load_project_config(project_dir).to_env_dict()
    for k, v in env.items():
        os.environ[k] = v
    logger.info("loaded project config from %s", project_dir)


def _build_runtime():
    """Build graph store + content store + backend mode + project metadata."""
    from k2g.core.config import Settings
    from k2g.db_store.factory import (
        _resolve_backend_mode,
        build_content_backend,
        build_graph_backend,
    )

    settings = Settings()
    graph = build_graph_backend(settings)
    backend = _resolve_backend_mode(settings)
    content = build_content_backend(settings)
    group = os.environ.get("K2G_USER_MEMORY_SAVE_GROUP") or "default"
    embedding = {
        "provider": str(getattr(settings, "embedding_provider", "")),
        "model": str(getattr(settings, "embedding_model", "")),
    }
    return graph, backend, group, embedding, content


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _build_export_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mweft-export",
        description="Export an MWeft domain to a portable .mweft.tar.gz archive.",
    )
    p.add_argument("domain", help="Domain to export (e.g., 'ai_memory').")
    p.add_argument(
        "--to",
        required=True,
        help="Output path. --format archive: a directory or .mweft.tar.gz "
             "file. --format dir: the export folder to populate.",
    )
    p.add_argument(
        "--format",
        choices=["archive", "dir"],
        default="archive",
        help="archive = single .mweft.tar.gz; dir = unpacked folder of "
             "JSONL files (human-readable / diffable). Default: archive.",
    )
    p.add_argument(
        "--no-segments",
        action="store_true",
        help="Skip k2g_raw_document / k2g_segment_blob.",
    )
    p.add_argument(
        "--no-plan",
        action="store_true",
        help="Skip plan_nodes / plan_direction_nodes.",
    )
    p.add_argument(
        "--no-content",
        action="store_true",
        help="Skip the content store (raw text bodies / inline content).",
    )
    p.add_argument(
        "--no-vectors",
        action="store_true",
        help="Skip entity/event embeddings. By default vectors travel with the "
             "archive so a migration (e.g. SQLite → Postgres) is lossless.",
    )
    p.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Project dir containing .mweft/project.yaml. "
             "Default: env-based runtime.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def export_main(argv: list[str] | None = None) -> int:
    args = _build_export_parser().parse_args(argv)
    _setup_logging(args.verbose)
    _setup_env_from_project(args.project_dir)

    from k2g.portable.exporter import DomainExporter
    from k2g.portable.manifest import ArchiveOptions

    graph, backend, group, embedding, content = _build_runtime()
    opts = ArchiveOptions(
        include_segments=not args.no_segments,
        include_plan=not args.no_plan,
        include_vectors=not args.no_vectors,
        include_content=not args.no_content,
    )

    exporter = DomainExporter(
        graph, backend, group=group, content_store=content,
    )
    manifest = exporter.export(
        args.domain,
        args.to,
        options=opts,
        embedding=embedding,
        archive_format=args.format,
    )

    total = sum(manifest.row_counts.values())
    print(
        f"exported domain={args.domain} format={args.format} rows={total}",
        file=sys.stderr,
    )
    print(f"  -> {exporter.last_archive_path}", file=sys.stderr)
    for k, v in sorted(manifest.row_counts.items()):
        print(f"  {k}: {v}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def _build_import_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mweft-import",
        description="Import a .mweft.tar.gz archive into an MWeft instance.",
    )
    p.add_argument(
        "archive",
        help="Path to a .mweft.tar.gz archive, or an unpacked export folder "
             "(auto-detected).",
    )
    p.add_argument(
        "--strategy",
        choices=["skip", "overwrite", "fail"],
        default="skip",
        help="PK conflict strategy.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without writing.",
    )
    p.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Project dir containing .mweft/project.yaml. "
             "Default: env-based runtime.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def import_main(argv: list[str] | None = None) -> int:
    args = _build_import_parser().parse_args(argv)
    _setup_logging(args.verbose)
    _setup_env_from_project(args.project_dir)

    from k2g.portable.importer import DomainImporter

    graph, backend, _, _, content = _build_runtime()
    importer = DomainImporter(graph, backend, content_store=content)
    result = importer.import_archive(
        args.archive,
        strategy=args.strategy,
        dry_run=args.dry_run,
    )

    prefix = "[DRY-RUN] " if args.dry_run else ""
    src_domain = result["manifest"].source.domain
    print(
        f"{prefix}imported archive='{args.archive}' "
        f"domain={src_domain} strategy={args.strategy}",
        file=sys.stderr,
    )
    for table, counts in sorted(result["results"].items()):
        non_zero = {k: v for k, v in counts.items() if v > 0}
        if non_zero:
            print(f"  {table}: {non_zero}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(export_main())
