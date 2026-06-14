"""``k2g-ingest-manifest`` console entry point (AI manifest ingestion).

When an AI (Claude, etc.) writes a ``[content, summary, entities]`` bundle
as a JSON manifest, this CLI reads it and loads it into K2G MemoryWeft.
Unlike a server-side LLM build pipeline, **K2G does not re-run
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

When this CLI runs as a *subprocess* (the AI spawns it from a shell), it does
NOT inherit the MCP server's env, so the env-resolved domain would fall back to
``ai_memory`` — the wrong store. Pass ``--domain`` to convey the MCP's configured
write domain explicitly; it takes precedence over env. (Still a single domain —
never the manifest's own field — so domain scatter stays prevented.)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("k2g.cli.ingest_manifest")


# Backend-routing env vars: their presence flips graph/vector/content stores
# between sqlite and Postgres (K2G_POSTGRES_DSN auto-selects Postgres). A resolved
# project (registry entry / .mcp.json) must decide the backend AUTHORITATIVELY, so
# when we apply one we CLEAR any of these it does not itself set — otherwise a
# stale K2G_POSTGRES_DSN lingering in the shell/session silently overrides a sqlite
# entry and the build lands in Postgres (DATA_DIR set but ignored).
_BACKEND_ROUTING_KEYS = (
    "K2G_POSTGRES_DSN",
    "GRAPH_DB_PROVIDER",
    "VECTOR_STORE_PROVIDER",
    "CONTENT_STORE_MODE",
)


class _AmbiguousRegistry(Exception):
    """Raised when >1 registry project maps to the same directory.

    The backend (sqlite vs Postgres) can't be guessed, so resolution refuses
    instead of silently picking one. ``main`` turns this into a hard error unless
    an explicit override (--data-dir / --mcp-config) disambiguates.
    """

    def __init__(self, directory: str, slugs: list[str]) -> None:
        super().__init__(f"{len(slugs)} projects for {directory}: {slugs}")
        self.directory = directory
        self.slugs = slugs


def _apply_resolved_env(env: dict) -> None:
    """Apply a resolved project env to os.environ, authoritative for the backend.

    Routing keys (``_BACKEND_ROUTING_KEYS``) are set when the entry provides a
    non-empty value, else CLEARED (so a stale value can't flip the backend).
    Non-routing keys are set when provided (empty/None skipped).
    """
    import os
    provided = {k: v for k, v in env.items() if v is not None and str(v) != ""}
    for k in _BACKEND_ROUTING_KEYS:
        if k in provided:
            os.environ[k] = str(provided[k])
        else:
            os.environ.pop(k, None)
    for k, v in provided.items():
        if k not in _BACKEND_ROUTING_KEYS:
            os.environ[str(k)] = str(v)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="k2g-ingest-manifest",
        description="AI manifest ingestion (zero LLM calls during load)",
    )
    p.add_argument("manifest", type=Path, help="Path to the *.manifest.json file to ingest")
    p.add_argument("--domain", default=None, metavar="DOMAIN",
                   help="Target write domain (single). Overrides the server env "
                        "K2G_USER_MEMORY_SAVE_DOMAIN — pass the MCP's configured write "
                        "domain here when running as a subprocess that doesn't inherit "
                        "that env (otherwise it falls back to 'ai_memory'). The "
                        "manifest's own domain field is still ignored.")
    p.add_argument("--data-dir", default=None, metavar="DIR",
                   help="Target data directory (the project root that holds "
                        "k2g_all_in_one.db / content_store.db / objects). Overrides "
                        "the DATA_DIR env. Pass the MCP's configured data dir here when "
                        "running as a subprocess that doesn't inherit that env — "
                        "otherwise data_dir falls back to './data' relative to the "
                        "current working directory, writing the build to the wrong place "
                        "(e.g. a temp folder the Manager never reads).")
    p.add_argument("--mcp-config", default=None, metavar="PATH",
                   help="Path to the .mcp.json that configures the mweft MCP server. "
                        "Its `mcpServers.mweft.env` block (DATA_DIR / K2G_POSTGRES_DSN / "
                        "CONTENT_STORE_MODE / K2G_USER_MEMORY_SAVE_DOMAIN / EMBEDDING_* …) "
                        "is loaded into the environment so this CLI writes to the SAME "
                        "backend (SQLite file OR Postgres DSN) and domain as the MCP — no "
                        "need to pass --data-dir/--domain separately. If omitted, the CLI "
                        "auto-discovers a .mcp.json upward from the manifest dir / cwd. "
                        "Use --no-mcp-config to disable. Explicit --domain/--data-dir still override.")
    p.add_argument("--no-mcp-config", action="store_true",
                   help="Disable .mcp.json auto-discovery/loading (use only env + flags).")
    # Internal staging mechanics — kept functional for the Manager / scripts that
    # drive staging programmatically, but hidden from --help so an AI agent does
    # not try to "resume" a leftover build_stage by hand (a fragile recovery path
    # that misreads a stale staging marker as "load not done").
    p.add_argument("--session-id", default=None, help=argparse.SUPPRESS)
    p.add_argument("--stage-root", default="", help=argparse.SUPPRESS)
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


def _load_registry_env(start: Path) -> tuple[str, dict] | None:
    """Resolve config from K2G's client-agnostic project registry.

    `~/.mweft/mweft_manager.json` maps each project's directory to its backend
    config (DATA_DIR / Postgres DSN / domain / embedding). Unlike a client's MCP
    config (`.mcp.json` for Claude, `.cursor/mcp.json` for Cursor, …) whose
    location varies per AI, this registry is K2G's own, so the CLI can resolve
    the SAME backend/domain the MCP uses by project directory alone — no need to
    know which AI client launched it.

    Walks up from ``start`` matching each ancestor against the registry; on a hit
    applies ``entry_env_dict`` (the single source of truth that also generates the
    MCP env) authoritatively to os.environ (clearing stale backend-routing vars).
    If more than one project is registered for the same directory the resolution is
    ambiguous (the backends may differ) — raise ``_AmbiguousRegistry`` rather than
    silently routing the build to the wrong backend. Returns (slug, applied_env)
    or None.
    """
    try:
        from k2g.ui.project_registry import entry_env_dict, list_projects
    except Exception:  # noqa: BLE001 — registry module optional
        return None
    try:
        projects = list(list_projects())
    except Exception:  # noqa: BLE001
        return None

    def _matches(d: Path) -> list:
        try:
            target = str(d.resolve())
        except OSError:
            target = str(d)
        out = []
        for e in projects:
            for cand in (getattr(e, "project_dir", None), getattr(e, "db_dir", None)):
                if not cand:
                    continue
                try:
                    same = str(Path(cand).resolve()) == target
                except OSError:
                    same = str(cand) == target
                if same:
                    out.append(e)
                    break
        return out

    cur = start.resolve()
    for d in (cur, *cur.parents):
        ms = _matches(d)
        if not ms:
            continue
        if len(ms) > 1:
            # Ambiguous: the entries may differ in backend (sqlite vs Postgres),
            # so picking one silently routes the build to the wrong store. Refuse
            # — main() decides whether an explicit override rescues it.
            slugs = [getattr(m, "slug", "?") or "?" for m in ms]
            raise _AmbiguousRegistry(str(d), slugs)
        entry = ms[0]
        env = entry_env_dict(entry)
        _apply_resolved_env(env)  # authoritative — clears stale backend-routing vars
        return (getattr(entry, "slug", "") or "", env)
    return None


def _find_mcp_config(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``.mcp.json``. Returns the first hit."""
    cur = start.resolve()
    for d in (cur, *cur.parents):
        cand = d / ".mcp.json"
        if cand.is_file():
            return cand
    return None


def _load_mcp_env(config_path: Path) -> tuple[str, dict]:
    """Load the mweft MCP server's ``env`` block from a .mcp.json into os.environ.

    The CLI runs as a subprocess that does not inherit the MCP server's env, so
    everything (DATA_DIR / DSN / CONTENT_STORE_MODE / domain / embedding) silently
    falls back to defaults (sqlite + ./data). Loading the same .mcp.json that
    configures the connected MCP makes this CLI write to the *same* backend and
    domain — the single source of truth. .mcp.json is authoritative here, so its
    values override the (usually empty) shell env; explicit --domain/--data-dir
    flags are applied afterwards and still win.

    Returns (server_name, applied_env). Raises on unreadable/invalid JSON.
    """
    import json

    data = json.loads(config_path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict) or not servers:
        return ("", {})
    # Prefer a server literally named "mweft"; else one whose command looks like
    # the k2g/mweft MCP; else the first entry.
    name = None
    if "mweft" in servers:
        name = "mweft"
    else:
        for n, spec in servers.items():
            cmd = str((spec or {}).get("command", "")).lower()
            if "k2g-mcp" in cmd or "mweft" in cmd:
                name = n
                break
        if name is None:
            name = next(iter(servers))
    env = ((servers.get(name) or {}).get("env")) or {}
    if not isinstance(env, dict):
        return (name, {})
    _apply_resolved_env(env)  # authoritative — clears stale backend-routing vars
    return (name, env)


def _resolve_domain(args_domain: str | None, settings) -> str:
    """Resolve the write domain for this ingest.

    Precedence: explicit ``--domain`` (the MCP's configured write domain,
    conveyed by the caller because a subprocess doesn't inherit the MCP env) >
    server env ``K2G_USER_MEMORY_SAVE_DOMAIN`` > ``ai_memory`` fallback. The
    manifest's own domain field never participates (scatter prevention).
    """
    if args_domain and args_domain.strip():
        return args_domain.strip()
    from k2g.memory.save_context import resolve_save_domain
    domain, _ = resolve_save_domain(settings)
    return domain


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
    from k2g.producer.manifest import ManifestProducer
    from k2g.producer.manifest_schema import ManifestError
    from k2g.staging import StagingLoader, StagingWriter

    session_id = args.session_id or f"manifest_{time.strftime('%Y%m%d_%H%M%S')}"

    import os as _os
    from k2g.core.config import get_settings as _gs

    # 1) Resolve the project's backend/domain config — the single source of truth.
    # A subprocess does NOT inherit the MCP server's env, so without this the
    # backend (SQLite vs PG), DATA_DIR, domain and embedding silently fall back to
    # defaults (sqlite + ./data). Resolution order:
    #   a) K2G's client-agnostic project registry (~/.mweft/mweft_manager.json),
    #      keyed by project dir — works regardless of which AI client launched us.
    #   b) the client's .mcp.json (explicit --mcp-config or auto-discovered) as a
    #      fallback for projects not in the registry.
    # Explicit --domain / --data-dir below still override either.
    if not (args.mcp_config or args.no_mcp_config):
        try:
            reg = _load_registry_env(args.manifest.parent) or _load_registry_env(Path.cwd())
        except _AmbiguousRegistry as exc:
            if args.data_dir and args.data_dir.strip():
                # An explicit --data-dir rescues the ambiguity (forces sqlite below).
                print(f"[warn] {len(exc.slugs)} projects registered for "
                      f"{exc.directory}: {exc.slugs} — ignoring the registry; "
                      f"using your --data-dir override.", file=sys.stderr)
                reg = None
            else:
                print(f"[error] {len(exc.slugs)} projects registered for "
                      f"{exc.directory}: {exc.slugs}. Refusing to guess the backend "
                      f"(sqlite vs Postgres). Disambiguate with --data-dir <dir> "
                      f"(sqlite) or --mcp-config <path>, or remove the duplicate "
                      f"entry in ~/.mweft/mweft_manager.json.", file=sys.stderr)
                return 2
        if reg is not None:
            slug, applied = reg
            print(f"[manifest] loaded project registry env "
                  f"(slug={slug or '?'}, keys={sorted(applied)})")
    if not args.no_mcp_config:
        cfg_path = Path(args.mcp_config) if args.mcp_config else (
            _find_mcp_config(args.manifest.parent) or _find_mcp_config(Path.cwd())
        )
        # Only fall back to .mcp.json when the registry did not already anchor us.
        if cfg_path and cfg_path.is_file() and not _os.environ.get("DATA_DIR") \
                and not _os.environ.get("K2G_POSTGRES_DSN"):
            try:
                name, applied = _load_mcp_env(cfg_path)
                if applied:
                    print(f"[manifest] loaded MCP env from {cfg_path} "
                          f"(server={name or '?'}, keys={sorted(applied)})")
            except Exception as exc:  # noqa: BLE001 — bad config shouldn't be silent
                print(f"[warn] failed to load --mcp-config {cfg_path}: {exc}",
                      file=sys.stderr)
        elif args.mcp_config and not (cfg_path and cfg_path.is_file()):
            print(f"[error] --mcp-config not found: {args.mcp_config}", file=sys.stderr)
            return 2

    # 2) --data-dir override — applied AFTER the MCP env so an explicit flag wins.
    # Resolves DATA_DIR and all derived sub-paths (k2g_all_in_one.db /
    # content_store.db / objects) under the intended project root. --data-dir means
    # "use a local SQLite project here", so force sqlite by clearing any
    # backend-routing vars first (a stale K2G_POSTGRES_DSN would otherwise keep the
    # build on Postgres and DATA_DIR would be silently ignored).
    if args.data_dir and args.data_dir.strip():
        for _k in _BACKEND_ROUTING_KEYS:
            _os.environ.pop(_k, None)
        _os.environ["DATA_DIR"] = _os.path.abspath(args.data_dir.strip())
        print(f"[manifest] data_dir override={_os.environ['DATA_DIR']} (forcing sqlite)")

    # Settings are cached (lru_cache) and build_dependencies() is the first call —
    # clear so the env we just set (MCP config / --data-dir) is picked up.
    _gs.cache_clear()

    deps = build_dependencies()
    db = deps.db
    settings = deps.settings

    # domain — explicit --domain (the MCP's write domain, conveyed by the caller)
    # takes precedence; otherwise forced from server env (see _resolve_domain).
    domain = _resolve_domain(args.domain, settings)
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
