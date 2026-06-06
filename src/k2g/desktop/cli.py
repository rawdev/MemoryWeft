"""Desktop launcher — native pywebview window over the per-project Manager,
**with no HTTP server** (no listening socket of any kind).

The SPA loads from disk via ``file://``; every ``fetch()`` is intercepted by
``ui_project/static/desktop-bridge.js`` and routed through pywebview's ``js_api``
into the FastAPI app run in-process by ``k2g.desktop.bridge.AsgiBridge`` (httpx
ASGITransport). One standalone process; project switching is an in-memory
``Settings`` swap (``k2g.desktop.switch``), never a subprocess, never a socket.

    mweft-app --slug <project-slug>
    mweft-app --project-dir <folder>
    python -m k2g.desktop --no-window          # headless self-test (no window/socket)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Option A: take config from the registry / in-memory Settings, never a stray
# repo-root ``.mwf`` dotenv. Must be set before ``k2g.core.config`` is imported.
os.environ.setdefault("K2G_DOTENV_FILE", "")

logger = logging.getLogger(__name__)

WINDOW_TITLE = "MemoryWeft Manager"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mweft-app", description=__doc__)
    p.add_argument(
        "--project-dir", type=Path, default=None,
        help="Project DB folder (anchor). Omit to use --slug or the last-active "
             "project from ~/.mweft/mweft_manager.json.",
    )
    p.add_argument(
        "--slug", default=None,
        help="Registry entry slug — pins the exact project (overrides folder lookup).",
    )
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=860)
    p.add_argument(
        "--no-window", action="store_true",
        help="Run the in-process ASGI bridge self-test and exit — no native "
             "window, no socket. For headless / CI smoke tests.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _resolve_entry(args: argparse.Namespace):
    """Resolve (project_dir, slug, entry). Identity priority: slug > dir > last-active.

    Creates the folder + seeds a registry entry when an unknown ``--project-dir``
    is given (fresh / portable first run), then resolves its slug.
    """
    from k2g.ui.project_registry import (
        find_by_dir, get_last_active, get_project,
    )
    from k2g.ui_project.cli import _ensure_registry_entry

    if args.slug:
        entry = get_project(args.slug)
        if entry is None:
            raise SystemExit(f"unknown --slug '{args.slug}' in mweft_manager.json")
        project_dir = Path(entry.db_dir or entry.project_dir).resolve()
        return project_dir, args.slug, entry

    if args.project_dir is not None:
        project_dir = args.project_dir.resolve()
        project_dir.mkdir(parents=True, exist_ok=True)
        _ensure_registry_entry(project_dir)            # seed if unknown
        entry = find_by_dir(project_dir)
        if entry is None:
            raise SystemExit(f"could not register project at {project_dir}")
        return project_dir, entry.slug, entry

    # Neither slug nor dir → last-active.
    slug = get_last_active()
    entry = get_project(slug) if slug else None
    if entry is None:
        raise SystemExit(
            "no --project-dir/--slug and no last-active project — "
            "pass --project-dir <folder>.")
    project_dir = Path(entry.db_dir or entry.project_dir).resolve()
    return project_dir, slug, entry


def _static_index() -> Path:
    """Absolute path to the per-project Manager's ``index.html``."""
    return Path(__file__).resolve().parents[1] / "ui_project" / "static" / "index.html"


def _selftest(bridge) -> int:
    """No-socket smoke: drive a few in-process ASGI requests and print results."""
    import base64

    def _hit(method: str, path: str) -> None:
        r = bridge.request(method, path, {}, None)
        body = base64.b64decode(r["body_b64"])
        logger.info("  %-4s %-22s → %s  (%d bytes)", method, path, r["status"], len(body))

    logger.info("ASGI bridge self-test (no window, no socket):")
    _hit("GET", "/health")
    _hit("GET", "/")
    _hit("GET", "/api/domains")
    logger.info("self-test done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[mweft-app] %(message)s",
    )

    project_dir, slug, entry = _resolve_entry(args)
    logger.info("Opening project '%s' at %s", slug, project_dir)

    # Activate the project IN MEMORY — no os.environ for project config.
    from k2g.desktop import switch
    switch.activate_entry(entry)

    # Single standalone process: no per-project server spawning, so disable the
    # single-active sweep that the per-project launcher uses. Set process-level
    # identity (not project config) for any "current project" lookups.
    os.environ["K2G_UI_NO_SINGLETON"] = "1"
    os.environ.setdefault("K2G_USE_HUB", "auto")
    os.environ["K2G_PROJECT_DIR"] = str(project_dir)
    os.environ["K2G_PROJECT_SLUG"] = slug

    # No real origin — the activate route soft-refreshes in place (spawned:false).
    switch.set_self_url("/")

    # Register the in-process switch hook so /projects/activate rebinds here
    # instead of spawning. (Internal variable, not env.)
    from k2g.web import deps
    deps.desktop_rebind = switch.rebind_project

    # Build the existing per-project app, standalone (no hub register / sweep),
    # and run it in-process via the ASGI bridge — no socket.
    from k2g.ui_project.cli import _build_app
    from k2g.desktop.bridge import AsgiBridge

    app = _build_app(project_dir, hub_info=None, project_port=None)
    bridge = AsgiBridge(app)
    try:
        bridge.start()                       # boots the loop + runs lifespan startup
    except Exception as exc:  # noqa: BLE001
        logger.error("ASGI bridge failed to start: %s", exc)
        return 4

    try:
        if args.no_window:
            return _selftest(bridge)

        try:
            import webview
        except ImportError:
            logger.error(
                "pywebview not installed — install with: pip install 'mweft[manager]'.",
            )
            return 3

        index_url = _static_index().as_uri()
        logger.info("Opening native window (file://, no socket) → %s", index_url)
        try:
            webview.create_window(
                WINDOW_TITLE, url=index_url, js_api=bridge,
                width=args.width, height=args.height,
            )
            webview.start(http_server=False)  # never start pywebview's bottle server
        except Exception as exc:  # noqa: BLE001 — surface WebView2 / backend issues
            logger.error(
                "native window failed (%s). On Windows the Manager UI needs the "
                "WebView2 runtime: https://developer.microsoft.com/microsoft-edge/webview2/",
                exc,
            )
            return 4
    finally:
        # Window closed / self-test done → run lifespan shutdown (DB close).
        bridge.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
