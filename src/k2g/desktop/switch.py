"""In-process project switch for the desktop app.

The browser Manager UI switches projects by *spawning* a fresh per-project
server (``subprocess.Popen("mweft-ui-project --slug …")``) and redirecting the
browser to its new port — the old server self-shuts-down. That hand-off leaks
zombie processes and makes the post-switch page look frozen while the new server
warms up.

The desktop app never spawns. Switching is a same-process swap of the **active
``Settings``** (built in memory from the registry entry, no ``os.environ``):

    deps.shutdown()                 # close the old project's DB
    set_active_settings(new)        # swap config in memory
    # → next data request rebuilds stores/embedding against the new project

See ``core.config.set_active_settings`` / ``Settings.backend_mode`` for why the
in-memory Settings is authoritative (the only config read that bypassed Settings
— ``db_store.factory._resolve_backend_mode`` — now honours ``backend_mode``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from k2g.core.config import Settings
    from k2g.ui.project_registry import ProjectEntry

logger = logging.getLogger(__name__)

# Returned to the frontend on a switch. The no-HTTP desktop app has no origin,
# so the launcher sets this to "/"; the activate route returns ``spawned:false``
# and the frontend soft-refreshes in place (the URL is unused, never navigated).
_self_url = "/"


def set_self_url(url: str) -> None:
    global _self_url
    _self_url = url


def settings_from_entry(entry: "ProjectEntry") -> "Settings":
    """Build an in-memory ``Settings`` from a registry entry — no env.

    Mirrors ``ui.project_registry.entry_env_dict`` (the proven env mapping) but
    targets ``Settings`` *field names* so nothing flows through ``os.environ``.
    """
    import os

    from k2g.core.config import Settings, search_targets_to_csv
    from k2g.ui.project_config import resolve_embedding_dim

    emb = entry.embedding or {}
    # Provider: an explicit entry value wins; otherwise fall back to the
    # deployment env (the portable bundle sets EMBEDDING_PROVIDER=onnx), then
    # "local". This lets an onnx-only bundle (no torch) serve projects whose
    # entry left embedding unset, instead of defaulting to the torch backend.
    provider = str(emb.get("provider") or os.environ.get("EMBEDDING_PROVIDER") or "local")
    model = str(emb.get("model") or "BAAI/bge-m3")
    dim = emb.get("dim")
    if dim in (None, ""):
        dim = resolve_embedding_dim(model)

    is_pg = (entry.backend == "postgres") or bool(entry.postgres_dsn)

    kw: dict = {
        "data_dir": entry.db_dir or entry.project_dir,
        "embedding_provider": provider,
        "embedding_model": model,
        "embedding_dim": int(dim),
        # Authoritative backend selection (factory reads this before any env).
        "backend_mode": "postgres" if is_pg else "sqlite",
    }
    # The ONNX model path is deployment-location-specific (the portable launcher
    # points it at the bundled model inside the unzip folder), so it comes from
    # the env, not the registry entry, which must stay portable across machines.
    if provider == "onnx":
        onnx_path = os.environ.get("EMBEDDING_ONNX_PATH")
        if onnx_path:
            kw["embedding_onnx_path"] = onnx_path
    if entry.domain:
        kw["user_memory_save_domain"] = entry.domain
    if entry.group:
        kw["user_memory_save_group"] = entry.group
    if entry.save_tags:
        kw["user_memory_save_tags"] = list(entry.save_tags)
    if entry.search_targets:
        kw["user_search_targets"] = search_targets_to_csv(entry.search_targets)
    if is_pg and entry.postgres_dsn:
        kw["postgres_dsn"] = entry.postgres_dsn

    return Settings(**kw)


def activate_entry(entry: "ProjectEntry") -> None:
    """Make ``entry`` the active project (first boot or switch).

    Tears down any current stores, swaps the active Settings, and clears the
    per-process dep caches so the next data request rebuilds against the new DB.
    Idempotent and safe to call before the first ``startup()``.
    """
    from k2g.core.config import set_active_settings
    from k2g.web import deps

    new_settings = settings_from_entry(entry)
    with deps._init_lock:
        try:
            deps.shutdown()              # close old DB (no-op if not yet built)
        except Exception as exc:  # noqa: BLE001
            logger.warning("desktop: store shutdown during switch failed: %s", exc)
        deps._stores = None
        deps._initialized = False
        set_active_settings(new_settings)
        # The BP-75 internal-write dispatcher caches a Deps built from the old
        # stores/settings — drop it so writes also hit the new project.
        try:
            from k2g.ui_project import internal_routes
            internal_routes._reset_caches_for_testing()
        except Exception as exc:  # noqa: BLE001
            logger.debug("desktop: internal_routes cache reset skipped: %s", exc)


def rebind_project(slug: str) -> str:
    """Switch the running app to project ``slug`` in-process. Returns self URL.

    Registered as ``deps.desktop_rebind`` by the launcher; called by the
    ``/projects/activate`` route when the desktop app is in charge.
    """
    from k2g.ui.project_registry import get_project, set_last_active

    entry = get_project(slug)
    if entry is None:
        raise ValueError(f"unknown project slug: {slug!r}")
    activate_entry(entry)
    # Update the process-level UI-identity markers that ``/projects/current``
    # reads (K2G_PROJECT_DIR / K2G_PROJECT_SLUG). These are *not* project DB
    # config (that lives in the in-memory Settings) — they only tell the shared
    # route which project the UI is showing, so the badge stays consistent.
    import os
    os.environ["K2G_PROJECT_DIR"] = str(entry.db_dir or entry.project_dir)
    os.environ["K2G_PROJECT_SLUG"] = slug
    try:
        set_last_active(slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("desktop: set_last_active(%s) failed: %s", slug, exc)
    logger.info(
        "desktop: switched to project %r (%s)",
        slug, entry.db_dir or entry.project_dir,
    )
    return _self_url
