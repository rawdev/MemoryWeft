"""``k2g-ingest-manifest`` console entry point (AI manifest ingestion).

When an AI (Claude, etc.) writes a ``[content, summary, entities]`` bundle
as a JSON manifest, this CLI reads it and loads it into K2G MemoryWeft.
Unlike the traditional ``k2g-build`` pipeline, **K2G does not re-run
summarization or NER via an LLM** — the manifest's AI-produced output is
trusted (``ner_method="caller_provided"``). Embedding (local BGE-M3), event
creation, ``chunk_order`` chains, entity_connection, and atomic load all
reuse the existing producer machinery.

domain / save-root / tag / forced save_tags / community freshness follow
**the same settings-based behavior as MCP ``mweft_remember``** — content is
stored under the same group/tag structure as items saved with ``mw save``.

Usage (installed project):
    k2g-ingest-manifest path/to/ingest.manifest.json
    k2g-ingest-manifest m.json --remove-after   # delete manifest after load

domain / working_folder are governed by server env
(K2G_USER_MEMORY_SAVE_DOMAIN / K2G_USER_MEMORY_SAVE_GROUP);
the manifest's domain field is ignored.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("k2g.cli.ingest_manifest")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="k2g-ingest-manifest",
        description="AI manifest ingestion (zero LLM calls during load)",
    )
    p.add_argument("manifest", type=Path, help="Path to the *.manifest.json file to ingest")
    p.add_argument("--session-id", default=None,
                   help="Staging session ID (default: timestamp). Used for staging isolation.")
    p.add_argument("--stage-root", default="",
                   help="Staging root override (default: from settings).")
    p.add_argument("--remove-after", action="store_true",
                   help="Delete the manifest file after a successful load.")
    p.add_argument("--incremental", action="store_true",
                   help="Reload only changed items, keyed by the manifest's file_path "
                        "(file/segment hash comparison + supersede). "
                        "Requires a file_path field in the manifest.")
    p.add_argument("--source", default=None,
                   help="(Recommended) Path to the original or temporary source file. "
                        "MWeft uses the *raw byte* hash of this file to detect changes "
                        "(robust to LLM chunking variation). If unchanged, the entire "
                        "manifest is skipped. Requires file_path in the manifest. "
                        "Automatically enables --incremental.")
    p.add_argument("--tag", action="append", dest="tags", default=None,
                   metavar="TAG",
                   help="User-confirmed tag for document builds (repeatable). "
                        "Activates curated mode — autotag (category) is not used, "
                        "env session forced tags are not inherited, and this tag is "
                        "attached as forced (Discovery). The LLM should confirm the "
                        "value with the user before passing it here.")
    p.add_argument("--working-folder", default=None, metavar="ROOT",
                   help="Dedicated storage root for document builds "
                        "(avoids polluting the conversation memory root). "
                        "Recommended when --tag is used.")
    p.add_argument("--no-community", action="store_true",
                   help="Skip synchronous community recompute_if_stale after load.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def _post_load_sync(db, settings, domain: str, event_ids: list[str]) -> None:
    """Run centroid + Jaccard update once after a manifest load completes.

    MCP remember runs these per event; the manifest path runs them once per
    batch over the affected entities/events after load. Uses the same
    entity_vector_sync_recompute gate. All operations are best-effort —
    already-loaded events are preserved even if post-load sync fails.
    """
    if not getattr(settings, "entity_vector_sync_recompute", True):
        return
    if not event_ids:
        return

    graph = db.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    ph = "%s" if backend == "postgres" else "?"

    # Collect affected entities (event -> participated_in)
    entity_ids: list[str] = []
    try:
        cur = graph._conn.cursor()
        placeholders = ", ".join([ph] * len(event_ids))
        cur.execute(
            f"SELECT DISTINCT entity_id FROM participated_in "
            f"WHERE event_id IN ({placeholders})",
            tuple(event_ids),
        )
        entity_ids = [r[0] for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to query affected entities for centroid update: %s", exc)

    # Centroid update (one pass per entity)
    if entity_ids:
        try:
            from k2g.trainer.projection import ProjectionEngine
            engine = ProjectionEngine(
                graph_store=graph, vector_store=db.vector,
                content_store=None, object_storage=None, embedding_client=None,
            )
            for eid in entity_ids:
                try:
                    engine.compute_entity_centroid(eid, use_cache=False)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Centroid update failed entity=%s: %s", eid, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProjectionEngine init failed for centroid update: %s", exc)

    # Incremental Jaccard (one pass per event)
    try:
        from k2g.trainer.jaccard import JaccardPhase
        jp = JaccardPhase()
        for event_id in event_ids:
            try:
                jp.incremental(db, domain=domain, event_id=event_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Incremental Jaccard failed event=%s: %s", event_id, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("JaccardPhase init failed: %s", exc)


def _post_load_community(db, domain: str) -> None:
    """Run synchronous community recompute_if_stale after a manifest load.

    The dirty signal (derived version) is raised automatically when events
    are appended. Both 'event' and 'entity' kinds are processed. The stale
    gate and OCC CAS are handled inside recompute_if_stale. Best-effort.
    """
    try:
        from k2g.trainer.community_runner import recompute_if_stale
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_runner import failed (skipping): %s", exc)
        return
    for kind in ("event", "entity"):
        try:
            res = recompute_if_stale(db, kind, domain=domain, triggered_by="manifest")
            logger.info(
                "community recompute kind=%s -> %s",
                kind, {k: res.get(k) for k in ("skipped", "ran", "advanced", "run_id")},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("community recompute failed kind=%s: %s", kind, exc)


def _empty_scope():
    """Return an empty Scope — ManifestProducer.discover ignores scope
    (it uses manifest_path instead)."""
    from k2g.producer.base import Scope
    return Scope(data_root=Path("."))


def main(argv: list[str] | None = None) -> int:
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream and getattr(_stream, "encoding", "").lower() != "utf-8":
            try:
                _stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.manifest.is_file():
        print(f"[error] manifest file not found: {args.manifest}", file=sys.stderr)
        return 2

    from k2g.core.config import DomainConfig
    from k2g.mcp.factory import build_dependencies
    from k2g.memory.save_context import resolve_save_domain
    from k2g.producer.manifest import ManifestProducer
    from k2g.producer.manifest_schema import ManifestError
    from k2g.staging import StagingLoader, StagingWriter

    session_id = args.session_id or f"manifest_{time.strftime('%Y%m%d_%H%M%S')}"

    deps = build_dependencies()
    db = deps.db
    settings = deps.settings

    # domain — forced from env. manifest's domain field is ignored.
    domain, _ = resolve_save_domain(settings)
    domain_config = DomainConfig({"domain": domain})

    print(f"[manifest] session={session_id} domain={domain} path={args.manifest}")

    # --source — compute raw byte hash (robust to LLM chunking variation).
    source_hash = None
    if args.source:
        from k2g.updater.incremental import IncrementalBuilder
        spath = Path(args.source)
        if not spath.is_file():
            print(f"[error] --source file not found: {spath}", file=sys.stderr)
            return 2
        source_hash = IncrementalBuilder.compute_hash_bytes(spath.read_bytes())
        args.incremental = True   # --source implies --incremental

    # Curated document-build mode: activated when --tag is provided.
    curated_tags = [t for t in (args.tags or []) if t and t.strip()] \
        if args.tags is not None else None
    if curated_tags is not None:
        if not args.working_folder:
            print(
                "[warn] --tag (curated mode) specified without --working-folder "
                "— falling back to manifest/env root "
                "(conversation memory root may be polluted). "
                "--working-folder is recommended for document builds.",
                file=sys.stderr,
            )
        print(f"[manifest] curated document build: tags={curated_tags} "
              f"working_folder={args.working_folder or '(fallback)'}")

    producer = ManifestProducer(
        embedding_client=deps.embedding,
        content=db.content,
        object_storage=db.object,
        graph=db.graph,
        settings=settings,
        manifest_path=str(args.manifest),
        source_hash=source_hash,
        working_folder_override=args.working_folder,
        curated_tags=curated_tags,
    )
    producer.set_domain_config(domain_config)

    # --incremental: inject IncrementalBuilder (deps.manifest = BuildManifestStore).
    updater = None
    if args.incremental:
        if deps.manifest is None:
            print("[warn] --incremental requested but manifest store is unavailable "
                  "— proceeding with full load",
                  file=sys.stderr)
        else:
            from k2g.updater.incremental import IncrementalBuilder
            updater = IncrementalBuilder(deps.manifest, build_id=session_id)

    embedding_dim = int(getattr(deps.embedding, "dim", 1024))

    # ── PRODUCE ──────────────────────────────────────────────────────────
    writer = StagingWriter(
        session_id=session_id, domain=domain,
        embedding_dim=embedding_dim, configured_root=args.stage_root,
    ).open()
    produce_ok = False
    item_count = 0
    try:
        try:
            units = list(producer.discover(domain_config, _empty_scope()))
        except ManifestError as exc:
            writer.finalize_failed(f"manifest validation failed: {exc}")
            print("[error] manifest validation failed:\n  - " +
                  "\n  - ".join(exc.errors), file=sys.stderr)
            return 1
        for unit in units:
            item_count = len(unit.manifest.items)
            res = producer.produce(unit, writer=writer, updater=updater)
            if res.skipped:
                writer.finalize_done()
                print("[manifest] incremental: no changes detected — load skipped (already up to date)")
                if args.remove_after:
                    try:
                        args.manifest.unlink()
                    except OSError:
                        pass
                return 0
            if not res.success:
                writer.finalize_failed(res.error or "produce failed")
                print(f"[error] produce failed (all-or-nothing): {res.error}",
                      file=sys.stderr)
                return 1
        writer.finalize_done()
        produce_ok = True
    except Exception as exc:  # noqa: BLE001
        writer.finalize_failed(f"{type(exc).__name__}: {exc}")
        logger.exception("Exception during produce stage")
        print(f"[error] produce exception: {exc}", file=sys.stderr)
        return 1

    if not produce_ok:
        return 1

    # ── LOAD ─────────────────────────────────────────────────────────────
    loader = StagingLoader(
        session_id=session_id, domain=domain, configured_root=args.stage_root,
    )
    if not loader.is_ready():
        print("[error] staging not ready (produce.state != done)", file=sys.stderr)
        return 1

    report = producer.load(loader, db, domain_config=domain_config, updater=updater)
    if report.fail_count > 0 or not report.event_ids:
        print(
            f"[error] load failed — success={report.success_count} "
            f"fail={report.fail_count} (DB rolled back, no partial load)",
            file=sys.stderr,
        )
        for err in report.errors[:10]:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"[manifest] load complete: {report.success_count} events stored")

    # ── post-load: centroid + Jaccard sync + community (synchronous) ─────
    _post_load_sync(db, settings, domain, report.event_ids)
    if not args.no_community:
        _post_load_community(db, domain)

    # ── remove temporary manifest ─────────────────────────────────────────
    if args.remove_after:
        try:
            args.manifest.unlink()
            print(f"[manifest] removed: {args.manifest}")
        except OSError as exc:
            logger.warning("Failed to remove manifest: %s", exc)

    print(
        f"[manifest] done — {report.success_count}/{item_count} events "
        f"(domain={domain})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
