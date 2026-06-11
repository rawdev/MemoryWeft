"""Per-project FastAPI backend.

BP-78: separately invoked from ``mweft-ui`` (the hub).  Users run

    mweft-ui-project --project-dir <path> --port <N>

after starting ``mweft-ui``.  At startup the subprocess registers
itself with the hub via ``POST /api/internal/projects/register`` so the
hub's write-forward router can route MCP write calls back to it.

Loads ``.mweft/project.yaml`` into env vars, then boots a FastAPI
server that includes the existing ``k2g.web.routes`` (timeline,
entity-graph, plan, etc.) plus UI-specific ``/health`` / ``/warmup``
and the internal write dispatcher.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

# Option A: the per-project UI server takes ALL its config from
# mweft_manager.json — never from a stray ``.mwf`` dotenv in the CWD. Disable
# dotenv loading *before* k2g.core.config is imported (its ``env_file`` is
# resolved at import time, so the registry's later ``K2G_DOTENV_FILE=""`` is too
# late). Otherwise a leftover repo-root ``.mwf`` (e.g. an old POSTGRES_GRAPH_DSN
# pointing at a deleted project) silently shadows the registry. ``setdefault``
# honors an explicit value the caller set on purpose.
os.environ.setdefault("K2G_DOTENV_FILE", "")

from k2g.launcher_hub.discovery import HubInfo, read_global_hub_info  # noqa: E402
from k2g.mcp.proxy.mode import get_hub_use_mode  # noqa: E402

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mweft-ui-project", description=__doc__)
    parser.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project DB folder (anchor). If omitted, the last-active project "
             "from ~/.mweft/mweft_manager.json is used.",
    )
    parser.add_argument(
        "--slug", default=None,
        help="Registry entry slug. Resolves the *exact* project even when several "
             "entries share the same folder (overrides folder-based lookup).",
    )
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--open", action="store_true",
        help="Open the Manager UI in the default browser once the server binds.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _project_env(project_dir: Path, slug: str | None = None) -> dict[str, str]:
    """Env for this project — from the ``mweft_manager.json`` entry (Option A).

    ``slug`` pins the *exact* entry: when several entries share one folder (e.g.
    two projects whose IDE folder is the same path), folder lookup
    (:func:`find_by_dir`) returns whichever is listed first, which would load the
    wrong DB. Resolving by slug avoids that. Falls back to folder lookup, then to
    a legacy ``.mweft/project.yaml`` so an un-migrated folder still boots.
    """
    from k2g.ui.project_registry import entry_env_dict, find_by_dir, get_project

    if slug:
        entry = get_project(slug)
        if entry is not None:
            return entry_env_dict(entry)
    entry = find_by_dir(project_dir)
    if entry is not None:
        return entry_env_dict(entry)
    from k2g.ui.project_config import load_project_config, project_config_path
    if project_config_path(project_dir).is_file():
        return load_project_config(project_dir).to_env_dict()
    raise FileNotFoundError(
        f"No mweft_manager.json entry (and no .mweft/project.yaml) for "
        f"{project_dir} — register the project in the Manager UI first.")


def _ensure_registry_entry(project_dir: Path) -> None:
    """Make sure ``mweft_manager.json`` has an entry for ``project_dir``.

    Already registered → no-op. A legacy ``.mweft/project.yaml`` present →
    import it (Option A migration). Otherwise (fresh/portable) → seed a minimal
    sqlite entry anchored at this folder.
    """
    from k2g.ui.project_registry import add_project, find_by_dir, update_project

    if find_by_dir(project_dir) is not None:
        return
    from k2g.ui.project_config import load_project_config, project_config_path
    if project_config_path(project_dir).is_file():
        cfg = load_project_config(project_dir)
        e = add_project(
            name=project_dir.name or "project",
            project_dir=str(project_dir),
            db_dir=cfg.data_dir or str(project_dir),
        )
        update_project(
            slug=e.slug, group=cfg.group, domain=cfg.domain,
            search_targets=list(cfg.search_targets or []),
            save_tags=list(cfg.save_tags or []),
            backend=("postgres" if cfg.postgres_dsn else "sqlite"),
            postgres_dsn=cfg.postgres_dsn or None,
            embedding={
                "provider": cfg.embedding.provider,
                "model": cfg.embedding.model,
                "dim": cfg.embedding.dim,
            },
        )
        logger.info("Migrated project.yaml → mweft_manager.json for %s", project_dir)
    else:
        add_project(
            name=project_dir.name or "project",
            project_dir=str(project_dir), db_dir=str(project_dir),
            backend="sqlite", domain="default", group="default",
        )
        logger.info("Seeded new project entry for %s", project_dir)


def _apply_env(project_dir: Path, slug: str | None = None) -> None:
    # An explicit ``--slug`` means "use THIS registry entry's config" — the
    # registry is the single source of truth, so it must WIN over anything
    # inherited from the launching shell. A leftover ``K2G_POSTGRES_DSN`` /
    # ``DATA_DIR`` in the terminal would otherwise shadow the registry (setdefault
    # keeps the pre-set value) and point the server at a stale/deleted DB.
    #
    # Without a slug (the portable launcher: ``--project-dir`` only) keep
    # setdefault — the launcher pins EMBEDDING_PROVIDER=onnx + EMBEDDING_ONNX_PATH
    # + EMBEDDING_DIM before exec and the registry's provider="local" would
    # otherwise clobber it and crash the torch-free portable.
    authoritative = slug is not None
    for k, v in _project_env(project_dir, slug).items():
        if authoritative:
            os.environ[k] = v
        else:
            os.environ.setdefault(k, v)
    # BP-78: the subprocess should always know its own project_dir + slug so MCP
    # forwarding and "current project" lookups resolve unambiguously.
    if authoritative:
        os.environ["K2G_PROJECT_DIR"] = str(project_dir)
        os.environ["K2G_PROJECT_SLUG"] = slug  # type: ignore[assignment]
    else:
        os.environ.setdefault("K2G_PROJECT_DIR", str(project_dir))


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# The uvicorn Server for THIS process — set in main() so /projects/activate can
# shut this server down after spawning the replacement (single-active switch).
_UVICORN_SERVER: object | None = None


def request_self_shutdown(delay: float = 3.0) -> bool:
    """Gracefully stop THIS uvicorn server after ``delay`` seconds.

    Called by the ``/projects/activate`` route once the freshly-spawned server
    for the target project reports healthy: the old server (the one handling the
    request) bows out so activating doesn't pile up one process per switch. The
    delay lets the HTTP response reach the browser and the page redirect to the
    new server first. No-op (returns False) when no server is registered — e.g.
    under TestClient or the standalone web app.
    """
    srv = _UVICORN_SERVER
    if srv is None:
        return False
    import threading

    def _stop() -> None:
        # uvicorn's serve loop polls this flag and shuts down gracefully
        # (runs lifespan teardown → hub unregister + store shutdown).
        try:
            srv.should_exit = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            os._exit(0)

    threading.Timer(max(0.0, delay), _stop).start()
    return True


_UI_PORT_RE = re.compile(r"--port[=\s]+(\d+)")


def kill_other_ui_servers(keep_port: int | None = None) -> int:
    """Single-active enforcement: terminate every OTHER per-project UI server so
    exactly one runs. Best-effort; never raises. Opt out with
    ``K2G_UI_NO_SINGLETON=1``. MCP servers and the hub (``k2g.ui.cli``) are not
    matched, so they're untouched. Returns the count killed.

    Two modes:

    * ``keep_port`` set (the bound port) — **port identity**: our own launcher
      pair shares ``keep_port`` and is spared; any ``k2g.ui_project.cli`` on a
      *different* port is a separate server and is killed. This is deterministic
      and does NOT rely on the old server's self-shutdown firing, so it also
      reaps an activation's *spawner* once it has handed off. Schedule it on a
      short delay after startup (see lifespan) so the spawner can return its
      redirect URL first.

    * ``keep_port is None`` — spare this process's whole tree (ancestors +
      descendants) and kill the rest. Used for an *immediate* startup sweep that
      clears unrelated zombies fast while sparing the spawner (an ancestor).
    """
    if os.environ.get("K2G_UI_NO_SINGLETON", "").strip().lower() in ("1", "true", "yes"):
        return 0
    try:
        import psutil
    except Exception:  # noqa: BLE001 — optional dep; skip silently
        return 0
    keep: set[int] = {os.getpid()}
    if keep_port is None:
        try:
            me = psutil.Process()
            keep.update(p.pid for p in me.parents())
            keep.update(c.pid for c in me.children(recursive=True))
        except Exception:  # noqa: BLE001
            try:
                keep.add(os.getppid())
            except Exception:  # noqa: BLE001
                pass
    killed = 0
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid in keep:
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "k2g.ui_project.cli" not in cmd and "mweft-ui-project" not in cmd:
                continue
            if keep_port is not None:
                m = _UI_PORT_RE.search(cmd)
                # Spare our own pair (same bound port); skip if the port is
                # unreadable rather than risk killing something unexpected.
                if m is None or int(m.group(1)) == keep_port:
                    continue
            proc.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:  # noqa: BLE001
            continue
    if killed:
        logger.info("single-active: terminated %d other UI server process(es)", killed)
    return killed


def _hub_required() -> bool:
    return get_hub_use_mode() == "true"


def _print_hub_missing_guidance(project_dir: Path) -> None:
    msg = (
        "[mweft-ui-project] No hub discovered at ~/.mweft/hub.json.\n"
        "[mweft-ui-project] Start the hub first:  mweft-ui\n"
        "[mweft-ui-project]\n"
        f"[mweft-ui-project] Project: {project_dir}\n"
        "[mweft-ui-project] (set K2G_USE_HUB=auto to allow standalone "
        "mode without the hub.)"
    )
    print(msg, file=sys.stderr)


def _register_with_hub(
    hub_info: HubInfo,
    project_dir: Path,
    project_port: int,
    *,
    timeout: float = 5.0,
) -> None:
    """Best-effort registration with the hub.

    Raises only on K2G_USE_HUB=true failures (caller decides to exit).
    """
    headers: dict[str, str] = {}
    if hub_info.token:
        headers["X-Mweft-Token"] = hub_info.token
    payload = {
        "project_dir": str(project_dir),
        "project_url": f"http://127.0.0.1:{project_port}",
        "pid": os.getpid(),
    }
    url = f"{hub_info.base_url()}/api/internal/projects/register"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "Hub register returned %d: %s", resp.status_code, resp.text,
            )
            if _hub_required():
                raise RuntimeError(
                    f"hub register failed with HTTP {resp.status_code}",
                )
            return
        logger.info("Registered with hub at %s", hub_info.base_url())
    except (httpx.RequestError, RuntimeError) as exc:
        if _hub_required():
            raise
        logger.warning(
            "Hub register failed (continuing standalone): %s", exc,
        )


def _unregister_from_hub(
    hub_info: HubInfo,
    project_dir: Path,
    *,
    timeout: float = 5.0,
) -> None:
    headers: dict[str, str] = {}
    if hub_info.token:
        headers["X-Mweft-Token"] = hub_info.token
    payload = {"project_dir": str(project_dir)}
    url = f"{hub_info.base_url()}/api/internal/projects/unregister"
    try:
        with httpx.Client(timeout=timeout) as client:
            client.post(url, json=payload, headers=headers)
        logger.info("Unregistered from hub")
    except httpx.RequestError as exc:
        logger.debug("Hub unregister failed (ignored): %s", exc)


def _build_app(project_dir: Path, *, hub_info: HubInfo | None = None,
               project_port: int | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles

    from k2g.ui_project.warmup import warmup as run_warmup
    from k2g.web.app import SafeJSONResponse
    from k2g.web.deps import shutdown, startup
    from k2g.ui_project.internal_routes import router as internal_router
    from k2g.web.routes import (
        app_meta, archive, browse, community, control, entity_admin, event,
        fs_browser, generate, installer, persona, predefine, project_init,
        search, search_explorer, tag, train,
    )
    # Plan Studio is excluded from the OSS build (web.routes.plan dropped) —
    # import it optionally so the same code runs with or without it.
    try:
        from k2g.web.routes import plan
    except ImportError:
        plan = None

    @asynccontextmanager
    async def lifespan(app):  # noqa: ANN201
        startup()
        # Single-active: keep exactly one UI server alive.
        #  1) Immediate own-tree sweep — clears unrelated zombies right away while
        #     sparing the spawner (an ancestor that's still finishing the request).
        #  2) Delayed port sweep — reaps the spawner too once it has handed off
        #     (it's on a different port), so we converge to one even if the old
        #     server's self-shutdown never fires. The delay lets the spawner
        #     return its redirect URL first.
        try:
            kill_other_ui_servers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("single-active sweep failed: %s", exc)
        if (project_port and os.environ.get("K2G_UI_NO_SINGLETON", "").strip().lower()
                not in ("1", "true", "yes")):
            import threading
            delay = 6.0
            try:
                delay = float(os.environ.get("K2G_UI_SINGLETON_DELAY", "6") or 6)
            except ValueError:
                delay = 6.0
            threading.Timer(
                delay, kill_other_ui_servers, kwargs={"keep_port": project_port},
            ).start()
        # BP-78: register with hub at lifespan startup (after uvicorn binds
        # the port) so the hub can immediately forward writes back.
        if hub_info is not None and project_port is not None:
            try:
                _register_with_hub(hub_info, project_dir, project_port)
            except Exception as exc:  # noqa: BLE001 — already logged inside
                logger.error("BP-78 register failed: %s", exc)
        try:
            yield
        finally:
            if hub_info is not None:
                _unregister_from_hub(hub_info, project_dir)
            shutdown()

    app = FastAPI(
        title="MWeft Manager (per-project)",
        version=app_meta._current_version(),
        lifespan=lifespan,
        default_response_class=SafeJSONResponse,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Store-init failures → actionable 503 instead of a bare 500. This app is
    # what the desktop ASGI bridge runs, so without it a Postgres project with
    # no psycopg2 (or an embedding fingerprint mismatch) returns an opaque 500.
    from k2g.web.deps import register_store_init_handler
    register_store_init_handler(app)
    routers = [
        search.router, browse.router, event.router, generate.router,
        tag.router, control.router, persona.router, train.router,
        community.router, entity_admin.router, predefine.router,
        search_explorer.router, archive.router, installer.router,
        fs_browser.router, project_init.router, app_meta.router,
    ]
    if plan is not None:
        routers.append(plan.router)
    for router in routers:
        app.include_router(router, prefix="/api")
    # BP-75 internal write router — own /api prefix, no extra
    app.include_router(internal_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "project_dir": str(project_dir)}

    @app.post("/warmup")
    def warmup_endpoint() -> dict[str, float | int]:
        try:
            result = run_warmup()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)) from e
        return {
            "embed_dim": result.embed_dim,
            "embed_elapsed": result.embed_elapsed,
            "deps_elapsed": result.deps_elapsed,
            "total_elapsed": result.total_elapsed,
        }

    # UI frontend static assets — mount last so /api and /health win on prefix match.
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[mweft-ui-project] %(message)s",
    )

    # Identity resolution priority: --slug (exact entry, survives folder
    # collisions) > --project-dir (folder lookup) > last-active.
    slug = args.slug
    if slug:
        from k2g.ui.project_registry import get_project
        entry = get_project(slug)
        if entry is None:
            logger.error("unknown --slug '%s' in ~/.mweft/mweft_manager.json", slug)
            return 2
        project_dir = Path(entry.db_dir or entry.project_dir).resolve()
        logger.info("Opening project '%s' (by slug) at %s", slug, project_dir)
    elif args.project_dir is not None:
        project_dir = args.project_dir.resolve()
    else:
        # No --project-dir / --slug: fall back to the last-active project.
        from k2g.ui.project_registry import get_last_active, get_project
        slug = get_last_active()
        entry = get_project(slug) if slug else None
        if entry is None:
            logger.error(
                "no --project-dir/--slug and no last-active project in "
                "~/.mweft/mweft_manager.json — pass --project-dir <folder>.")
            return 2
        project_dir = Path(entry.db_dir or entry.project_dir).resolve()
        logger.info("Reopening last-active project '%s' at %s", slug, project_dir)
    # First-run bootstrap: create the folder + seed a registry entry when this
    # project_dir is unknown (the portable launcher points --project-dir at a
    # fresh data/project that does not exist yet — single-user standalone, no
    # hub/init step). Option A: config lives in mweft_manager.json, not a yaml.
    project_dir.mkdir(parents=True, exist_ok=True)
    if not slug:
        _ensure_registry_entry(project_dir)

    try:
        _apply_env(project_dir, slug=slug)
    except FileNotFoundError as e:
        logger.error("Project not initialized: %s", e)
        return 5

    # BP-78: hub discovery + fail-loud when K2G_USE_HUB=true and no hub.
    hub_info = read_global_hub_info()
    if hub_info is None and _hub_required():
        _print_hub_missing_guidance(project_dir)
        return 5
    if hub_info is not None:
        logger.info(
            "Hub discovered at %s (will register on startup)",
            hub_info.base_url(),
        )
    else:
        logger.info("No hub discovered — running standalone (K2G_USE_HUB=auto)")

    try:
        import uvicorn
    except ImportError:
        logger.error("uvicorn not installed. Install with: pip install 'k2g[web]'")
        return 3

    # Decide port up front so we can register the correct URL with the hub.
    project_port = args.port or _pick_free_port()

    app = _build_app(
        project_dir,
        hub_info=hub_info,
        project_port=project_port,
    )

    if args.open:
        # Open the browser shortly after uvicorn binds the port.
        import threading
        import webbrowser
        url = f"http://{args.host}:{project_port}/"
        logger.info("Manager UI: %s (opening browser)", url)
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # Run via an explicit Server (not uvicorn.run) so /projects/activate can
    # request a graceful self-shutdown after spawning the replacement server.
    global _UVICORN_SERVER
    config = uvicorn.Config(app, host=args.host, port=project_port, log_level="info")
    server = uvicorn.Server(config)
    _UVICORN_SERVER = server
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
